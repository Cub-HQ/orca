#!/usr/bin/env python3
"""Set Stage + Run on the Cubatica project boards for one issue."""
import argparse, functools, json, re, subprocess, sys, time
from datetime import datetime, timezone

PROJECTS = (3, 4)  # users/Cubatica projects: Fitness Coach Factory, All Projects
OWNER = "Cubatica"


def projects_for(repo):
    return PROJECTS if repo == "Cubatica/fitness-coach" else (4,)


# Pipeline milestones, not board columns (12 jobs including Admission).
# admission intake build review rework re-review qa rebase merge deploy live-test verdict
STEPS_DONE = {"Queued": 0, "Triage": 2, "Building": 3, "In review": 4, "QA": 7, "Deploying": 10, "Live test": 11, "Shipped": 12}
TOTAL = 12


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


def pages(path):
    while path:
        values, path = api(path)
        yield from values


@functools.cache
def project_fields(number):
    return {f["name"]: f for f in pages(
        f"users/{OWNER}/projectsV2/{number}/fields?per_page=30")}


def update_item(number, item_id, stage_name=None, run_url="", clear_why=False, running_for=None, shipped_at=None):
    """Update an existing item without querying or adding any board items."""
    path = f"users/{OWNER}/projectsV2/{number}"
    fields = project_fields(number)
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


def sync_project(number, issue, stage_name, run_url):
    path = f"users/{OWNER}/projectsV2/{number}"
    date_field = project_fields(number).get("Shipped At")
    selected = f"&fields={date_field['id']}" if date_field else ""
    item = next((i for i in pages(f"{path}/items?per_page=100{selected}")
                 if (i.get("content") or {}).get("url") == issue["url"]), None)
    if item is None:
        item, _ = api(f"{path}/items", "POST", {"type": "Issue", "id": issue["id"]})
    existing = next(((field.get("value") or {}).get("raw") for field in item.get("fields", [])
                     if field["name"] == "Shipped At"), None)
    date = first_ship_date(issue, existing) if stage_name == "Shipped" and date_field else None
    update_item(number, item["id"], stage_name, run_url, shipped_at=date)


def main():
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

    for number in projects_for(a.repo):
        try:
            sync_project(number, issue, a.stage, a.run_url)
            print(f"board: project {number} #{a.issue} -> {a.stage}")
        except Exception as exc:  # one board must not prevent the other from syncing
            print(f"board sync skipped: project {number}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # board sync must never fail the pipeline
        print(f"board sync skipped: {exc}", file=sys.stderr)
