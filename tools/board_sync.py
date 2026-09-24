#!/usr/bin/env python3
"""Set Stage + Run on the personal and organization factory boards for one issue."""
import argparse, functools, json, re, subprocess, sys, time
from datetime import datetime, timezone

ORG_PROJECTS = ("orgs/Cub-HQ/projectsV2/1", "orgs/Cub-HQ/projectsV2/2")


def projects_for(repo):
    return (3, ORG_PROJECTS[0], 4, ORG_PROJECTS[1]) if repo == "Cub-HQ/fitness-coach" else (4, ORG_PROJECTS[1])

def project_path(project):
    return project if isinstance(project, str) else f"users/Cubatica/projectsV2/{project}"


# Pipeline job milestones, not board columns; mirror this repo's df-pipeline.yml.
PIPELINE_STAGES = ("Admission", "Intake", "Build", "Review", "Rework", "Re-review",
                   "QA", "Rebase", "Merge", "Verdict")
TOTAL = len(PIPELINE_STAGES)
STEPS_DONE = {"Queued": 0, "Shipped": TOTAL}
STEPS_DONE.update({stage: PIPELINE_STAGES.index(job) + 1 for stage, job in {
    "Triage": "Intake", "Building": "Build", "In review": "Review", "QA": "QA",
    "Deploying": "Merge"}.items()})



def api(path, method="GET", body=None):
    cmd = ["gh", "api", "--include", "-X", method,
           "-H", "X-GitHub-Api-Version: 2026-03-10", path]
    if body is not None:
        cmd += ["--input", "-"]
    for attempt in range(2):
        r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                           capture_output=True, text=True)
        headers, _, payload = r.stdout.partition("\n\n")
        status = re.match(r"HTTP/\S+ (\d{3})", headers)
        code = int(status[1]) if status else 0
        if r.returncode == 0 and 200 <= code < 300:
            link = re.search(r'^link: (.*)$', headers, re.I | re.M)
            next_page = re.search(r'<([^>]+)>; rel="next"', link[1]) if link else None
            return json.loads(payload), next_page[1] if next_page else None
        if attempt == 0 and (code in (403, 429) or 500 <= code < 600):
            time.sleep(10)
            continue
        raise RuntimeError(f"{method} {path}: {r.stderr[:500] or payload[:500]}")

def graphql(query, variables):
    r = subprocess.run(["gh", "api", "graphql", "--input", "-"],
                       input=json.dumps({"query": query, "variables": variables}),
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"GraphQL: {r.stderr[:500] or r.stdout[:500]}")
    result = json.loads(r.stdout)
    if result.get("errors"):
        raise RuntimeError(f"GraphQL: {result['errors']}")
    return result["data"]


@functools.cache
def project_id(project):
    value, _ = api(project_path(project))
    return value["node_id"]


def add_item(project, issue):
    content_id = issue.get("node_id")
    if not content_id:
        raise RuntimeError(f"#{issue.get('number')} has no GraphQL node id")
    data = graphql("""mutation($project: ID!, $content: ID!) {
      addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { databaseId } }
    }""", {"project": project_id(project), "content": content_id})
    return {"id": data["addProjectV2ItemById"]["item"]["databaseId"], "fields": []}


def pages(path):
    while path:
        values, path = api(path)
        yield from values


@functools.cache
def project_fields(project):
    return {f["name"]: f for f in pages(f"{project_path(project)}/fields?per_page=30")}


def update_item(project, item_id, stage_name=None, run_url="", clear_why=False, running_for=None, shipped_at=None):
    """Update an existing item without querying or adding any board items."""
    path = project_path(project)
    fields = project_fields(project)
    stage = fields.get("Workflow Stage")
    opt = next((o["id"] for o in (stage or {}).get("options", [])
                if o["name"]["raw"] == stage_name), None)
    if stage_name is not None and opt is None:
        raise RuntimeError(f"unknown Workflow Stage: {stage_name}")
    updates = [{"id": stage["id"], "value": opt}] if stage_name is not None else []
    if run_url and "Workflow URL" in fields:
        updates.append({"id": fields["Workflow URL"]["id"], "value": run_url})
    if stage_name in STEPS_DONE and "Workflow Progress" in fields:
        n = STEPS_DONE[stage_name]
        bar = "▓" * n + "░" * (TOTAL - n) + f" {n}/{TOTAL}"
        updates.append({"id": fields["Workflow Progress"]["id"], "value": bar})
    if clear_why and "Why Awaiting Human" in fields:
        updates.append({"id": fields["Why Awaiting Human"]["id"], "value": None})
    if running_for is not None:
        updates.append({"id": fields["Running For"]["id"], "value": running_for or None})
    if shipped_at is not None and "Shipped At" in fields:
        updates.append({"id": fields["Shipped At"]["id"], "value": shipped_at})
    if updates:
        api(f"{path}/items/{item_id}", "PATCH", {"fields": updates})


def first_ship_date(issue, existing=None):
    """Keep the original ship date, including when an issue is reopened."""
    if existing:
        return None
    closed = issue.get("closed_at")
    if not closed and issue.get("state", "").lower() == "closed":
        path = issue["url"].split("api.github.com/", 1)[-1]
        issue, _ = api(path)
        closed = issue.get("closed_at")
        if not closed:
            merged = []
            timeline = list(pages(f"{path}/timeline?per_page=100"))
            closing_commits = {event["commit_id"] for event in timeline
                               if event.get("event") == "closed" and event.get("commit_id")}
            for event in timeline:
                source = (event.get("source") or {}).get("issue") or {}
                pull = source.get("pull_request") or {}
                if pull.get("url"):
                    pr, _ = api(pull["url"])
                    if pr.get("merged_at") and (event.get("will_close_target") is True
                                               or pr.get("merge_commit_sha") in closing_commits):
                        merged.append(pr["merged_at"])
            closed = min(merged) if merged else None
    return (closed or datetime.now(timezone.utc).isoformat())[:10]


def sync_project(project, issue, stage_name, run_url, known_missing=False):
    path = project_path(project)
    date_field = project_fields(project).get("Shipped At")
    selected = f"&fields={date_field['id']}" if date_field else ""
    item = None if known_missing else next((
        item for item in pages(f"{path}/items?per_page=100{selected}")
        if (item.get("content") or {}).get("url") == issue["url"]), None)
    if item is None:
        item = add_item(project, issue)
    existing = next((field.get("value") for field in item.get("fields", [])
                     if field["name"] == "Shipped At"), None)
    date = first_ship_date(issue, existing) if stage_name == "Shipped" and date_field else None
    update_item(project, item["id"], stage_name, run_url, shipped_at=date)


def self_test():
    calls = []
    original_project_id, original_graphql = project_id, graphql
    try:
        globals()["project_id"] = lambda project: "PVT_project"
        globals()["graphql"] = lambda query, variables: (
            calls.append((query, variables)) or
            {"addProjectV2ItemById": {"item": {"databaseId": 90}}})
        item = add_item(4, {"number": 45, "node_id": "I_issue"})
        assert item["id"] == 90
        assert calls[0][1] == {"project": "PVT_project", "content": "I_issue"}
        try:
            add_item(4, {"number": 46})
        except RuntimeError as exc:
            assert str(exc) == "#46 has no GraphQL node id"
        else:
            raise AssertionError("missing node id did not fail")
        assert projects_for("Cub-HQ/fitness-coach") == (
            3, "orgs/Cub-HQ/projectsV2/1", 4, "orgs/Cub-HQ/projectsV2/2")
        assert projects_for("Cub-HQ/orca") == (4, "orgs/Cub-HQ/projectsV2/2")
    finally:
        globals()["project_id"], globals()["graphql"] = original_project_id, original_graphql
    print("board-sync self-test: PASS (node ID add, missing ID failure, four-board routing)")


def main():
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        return
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--issue", required=True, type=int)
    p.add_argument("--stage", required=True)
    p.add_argument("--run-url", default="")
    a = p.parse_args()

    issue, _ = api(f"repos/{a.repo}/issues/{a.issue}")
    if issue["state"] == "closed" and a.stage != "Shipped":
        # a closed issue can never regress to an active/blocked column
        print(f"board: #{a.issue} closed; ignoring stage {a.stage}")
        return

    for path in projects_for(a.repo):
        sync_project(path, issue, a.stage, a.run_url)
        print(f"board: {path} #{a.issue} -> {a.stage}")


if __name__ == "__main__":
    main()
