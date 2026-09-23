#!/usr/bin/env python3
"""Loud runner stalls; scheduled serially by factory-sweep, never reroute live jobs."""
import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys

TRACKER = "repos/Cub-HQ/omp-config-backup/issues/123/comments"
PREFIX = "<!-- factory-runner-watch "
SAFE = {"Admission", "Merge", "Merge (not shipped)", "Verdict"}


def gh(path, method="GET", data=None, pages=False):
    cmd = ["gh", "api", path, "-X", method]
    if pages:
        cmd += ["--paginate", "--slurp"]
    if data is not None:
        cmd += ["--input", "-"]
    result = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{method} {path}: {result.stderr[:500]}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def trusted(comment):
    return (comment.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"}
            or comment.get("user", {}).get("login") == "github-actions[bot]")


def records(comments):
    for comment in comments:
        if not trusted(comment):
            continue
        for line in comment.get("body", "").splitlines():
            if line.startswith(PREFIX) and line.endswith(" -->"):
                try:
                    record = json.loads(line[len(PREFIX):-4])
                    if isinstance(record, dict) and record.get("key"):
                        yield comment, record
                except ValueError:
                    pass


def stalled(job, runners, now):
    # GitHub pending/waiting jobs (needs/concurrency/environment) are not runner queues.
    # Unresolved jobs also lack concrete labels. Never age them from run.created_at.
    labels = job.get("labels") or []
    if job.get("status") != "queued" or not labels or job.get("runner_id"):
        return None
    if any("${{" in label for label in labels) or not job.get("created_at"):
        return None
    minutes = (now - datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))).total_seconds() / 60
    if minutes <= 10:
        return None
    required = {label.lower() for label in labels}
    if "self-hosted" in required:
        matching = [r for r in runners if required <= {x["name"].lower() for x in r.get("labels", [])}]
        online = [r for r in matching if r.get("status") == "online"]
        idle = [r for r in online if r.get("busy") is False]
        if idle:
            return None
        counts = f"online={len(online)}, idle={len(idle)}, registered={len(matching)}"
        kind = "self-hosted"
    elif len(labels) == 1 and re.fullmatch(r"(?:ubuntu|windows|macos)-(?:latest|[\d.]+)(?:-arm)?", labels[0]):
        # Hosted fleet counts are not exposed by the repository runners API.
        counts = "online=unknown, idle=unknown (hosted counts unavailable); observed pick-ups=0"
        kind = "cloud"
    else:
        return None
    return kind, f"required={','.join(labels)}, queued-for={minutes:.1f} minutes, {counts}"


def block_board(repo, issue, url, reason):
    import board_sync
    for project in board_sync.projects_for(repo):
        try:
            board_sync.sync_project(project, issue, "Blocked", url)
            fields = board_sync.project_fields(project)
            why = fields.get("Why Awaiting Human")
            if why:
                root = f"users/{board_sync.OWNER}/projectsV2/{project}"
                item = next(i for i in board_sync.pages(root + "/items?per_page=100")
                            if (i.get("content") or {}).get("url") == issue["url"])
                board_sync.api(f"{root}/items/{item['id']}", "PATCH",
                               {"fields": [{"id": why["id"], "value": reason}]})
        except Exception as exc:
            print(f"runner-watch board update unavailable: {exc}", file=sys.stderr)


def watch(repo, api=gh, board=block_board, now=None):
    now = now or datetime.now(timezone.utc)
    root = f"repos/{repo}"
    def listing(path, key=None):
        return [v for page in api(path, pages=True) for v in (page[key] if key else page)]
    def comments(number):
        return listing(f"{root}/issues/{number}/comments?per_page=100")
    def save(number, comment, record):
        body = record["reason"] + "\n\n" + record["explanation"] + "\n" + PREFIX + json.dumps(record, sort_keys=True) + " -->"
        if comment:
            return api(f"{root}/issues/comments/{comment['id']}", "PATCH", {"body": body})
        return api(f"{root}/issues/{number}/comments", "POST", {"body": body})

    runners = None
    work = {}
    # Resume durable side effects even after cancellation removes the run from active lists.
    for issue in listing(root + "/issues?state=open&labels=factory%3Ablocked&per_page=100"):
        for comment, record in records(comments(issue["number"])):
            if record.get("repo") == repo and record.get("phase") != "done":
                work[record["key"]] = (issue, comment, record)
    for status in ("queued", "in_progress"):
        runs = listing(root + f"/actions/workflows/df-pipeline.yml/runs?status={status}&per_page=100", "workflow_runs")
        for run in runs:
            match = re.match(r"#(\d+)\s", run.get("display_title", ""))
            if not match:
                continue
            number = int(match[1])
            existing = {r["key"]: (c, r) for c, r in records(comments(number))
                        if r.get("repo") == repo and "phase" in r}
            jobs = listing(root + f"/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", "jobs")
            for job in jobs:
                key = f"{repo}:{run['id']}:{job['id']}"
                if key in existing:
                    comment, record = existing[key]
                    if record.get("phase") != "done":
                        issue = api(f"{root}/issues/{number}")
                        work[key] = (issue, comment, record)
                    continue
                if "self-hosted" in {x.lower() for x in job.get("labels", [])} and runners is None:
                    runners = listing(root + "/actions/runners?per_page=100", "runners")
                evidence = stalled(job, runners or [], now)
                if not evidence:
                    continue
                issue = api(f"{root}/issues/{number}")
                if issue.get("state") != "open":
                    continue
                kind, detail = evidence
                record = {"key": key, "repo": repo, "issue": number, "run": run["id"],
                          "job": job["id"], "phase": "alert", "workflow": run["workflow_id"],
                          "ref": run["head_branch"], "url": run["html_url"],
                          "fallback": kind == "cloud" and job["name"] in SAFE,
                          "reason": f"cloud runner budget cap likely hit (or runner outage): run={run['id']}, job={job['name']}, type={kind}, {detail}",
                          "explanation": "Blocked reason: no runner has picked up this ready job. The factory is paused; check the Actions cap or runner availability. Hosted capacity is not observable; this is a stall warning, not proof of spending."}
                # Establish recoverable work before cancellation or any redispatch.
                api(f"{root}/issues/{number}/labels", "POST", {"labels": ["factory:blocked"]})
                comment = save(number, None, record)
                work[key] = (issue, comment, record)
    claimed_runs = set()
    for issue, comment, record in work.values():
        number = issue["number"]
        if issue.get("state") != "open":
            continue
        if record["phase"] == "alert":
            api(f"{root}/issues/{number}/labels", "POST", {"labels": ["factory:blocked"]})
            board(repo, issue, record["url"], record["reason"])
            tracker_records = records(listing(TRACKER + "?per_page=100"))
            if not any(r["key"] == record["key"] for _, r in tracker_records):
                api(TRACKER, "POST", {"body": record["reason"] + f"\nAffected: {repo}#{number}; {record['url']}\n" + PREFIX + json.dumps({"key": record["key"]}) + " -->"})
            record["phase"] = "cancel" if record["fallback"] else "done"
            comment = save(number, comment, record)
        if record["phase"] == "cancel":
            run = api(f"{root}/actions/runs/{record['run']}")
            if run["status"] != "completed":
                api(f"{root}/actions/runs/{record['run']}/cancel", "POST")
                run = api(f"{root}/actions/runs/{record['run']}")
            if run["status"] != "completed":
                continue  # next scheduled sweep resumes; cancellation is asynchronous
            if run.get("conclusion") != "cancelled":
                record["phase"] = "done"  # completed naturally: do not duplicate its work
                save(number, comment, record)
                continue
            # One workflow redispatch even if several safe jobs queued in the same run.
            siblings = [r for _, r in records(comments(number)) if r.get("run") == record["run"]]
            if record["run"] in claimed_runs or any(r.get("phase") in {"dispatch-claimed", "dispatched"} for r in siblings):
                record["phase"] = "done"
                save(number, comment, record)
                continue
            record["phase"] = "dispatch-claimed"
            record["explanation"] += " Self-hosted fallback claimed; if dispatch fails or is interrupted, inspect this receipt before a manual retry (never automatic duplicate dispatch)."
            comment = save(number, comment, record)
            claimed_runs.add(record["run"])
            api(f"{root}/actions/workflows/{record['workflow']}/dispatches", "POST",
                {"ref": record["ref"], "inputs": {"issue": str(number), "runner": "self-hosted"}})
            record["phase"] = "dispatched"
            record["explanation"] += " Self-hosted fallback dispatched after confirmed cancellation. Live jobs retain their self-hosted route."
            save(number, comment, record)


def self_test():
    from copy import deepcopy
    now = datetime(2026, 9, 23, 1, tzinfo=timezone.utc)
    job = {"id": 20, "name": "Admission", "status": "queued", "runner_id": None,
           "created_at": "2026-09-23T00:40:00Z", "labels": ["ubuntu-latest"]}
    assert stalled(job, [], now)[0] == "cloud"
    for change in ({"status": "pending"}, {"labels": []}, {"created_at": None},
                   {"created_at": "2026-09-23T00:51:00Z"}, {"runner_id": 9}):
        assert stalled(dict(job, **change), [], now) is None
    local = dict(job, labels=["self-hosted", "m4"])
    idle = {"status": "online", "busy": False, "labels": [{"name": "self-hosted"}, {"name": "m4"}]}
    assert stalled(local, [idle], now) is None
    assert "online=0, idle=0" in stalled(local, [], now)[1]
    assert not list(records([{"body": PREFIX + '{"key":"spoof"} -->', "author_association": "NONE"}]))

    def replay(selected_job, confirm=True, available=(), alert=True):
        issue = {"number": 8, "id": 80, "state": "open", "url": "https://api.github.com/repos/Cub-HQ/demo/issues/8"}
        run = {"id": 10, "workflow_id": 7, "display_title": "#8 demo", "head_branch": "main",
               "html_url": "https://github.com/Cub-HQ/demo/actions/runs/10", "status": "queued"}
        stored, notices, calls, boards = [], [], [], []
        def fake(path, method="GET", data=None, pages=False):
            calls.append((path, method, data))
            if path == TRACKER or path.startswith(TRACKER + "?"):
                if method == "POST":
                    notices.append(dict(data, author_association="OWNER"))
                    return notices[-1]
                return [deepcopy(notices)]
            if "/issues/comments/" in path:
                c = next(c for c in stored if c["id"] == int(path.rsplit("/", 1)[1]))
                c.update(data)
                return deepcopy(c)
            if "/issues/8/comments" in path:
                if method == "POST":
                    c = dict(data, id=len(stored) + 1, author_association="OWNER")
                    stored.append(c)
                    return deepcopy(c)
                return [deepcopy(stored)]
            if "/issues?" in path:
                return [[issue] if stored else []]
            if path.endswith("/issues/8"):
                return issue
            if path.endswith("/labels"):
                return []
            if "/runs?status=" in path:
                return [{"workflow_runs": [deepcopy(run)] if f"status={run['status']}&" in path else []}]
            if "/jobs?" in path:
                return [{"jobs": [selected_job]}]
            if "/actions/runners?" in path:
                return [{"runners": list(available)}]
            if path.endswith("/cancel"):
                if confirm:
                    run.update(status="completed", conclusion="cancelled")
                return None
            if path.endswith("/actions/runs/10"):
                return deepcopy(run)
            if path.endswith("/dispatches"):
                assert run["status"] == "completed" and run["conclusion"] == "cancelled"
                assert data == {"ref": "main", "inputs": {"issue": "8", "runner": "self-hosted"}}
                return None
            raise AssertionError(path)
        for _ in range(2):
            watch("Cub-HQ/demo", fake, lambda *args: boards.append(args), now)
        dispatches = [c for c in calls if c[0].endswith("/dispatches")]
        if not alert:
            assert not stored and not notices and not boards and not dispatches
            assert all(method == "GET" for _, method, _ in calls)
            return calls
        assert len(stored) == len(notices) == len(boards) == 1
        assert stored[0]["body"].startswith("cloud runner budget cap likely hit (or runner outage):")
        assert boards[0][-1].startswith("cloud runner budget cap likely hit (or runner outage):")
        assert len(dispatches) == int(confirm and selected_job["labels"] == ["ubuntu-latest"] and selected_job["name"] in SAFE)
        return calls
    replay(job)
    replay(job, False)
    local_calls = replay(local)
    assert not any(c[0].endswith("/cancel") for c in local_calls)
    replay(dict(job, name="Deploy"))
    replay(local, available=[idle], alert=False)
    replay(dict(job, created_at="2026-09-23T00:51:00Z"), alert=False)
    replay(dict(job, status="pending"), alert=False)
    replay(dict(job, labels=[]), alert=False)
    # Execute the actual board adapter, including its reason field write.
    import types
    board_calls = []
    stub = types.SimpleNamespace(
        OWNER="Cubatica", projects_for=lambda repo: (4,),
        sync_project=lambda *args: board_calls.append(args),
        project_fields=lambda project: {"Why Awaiting Human": {"id": 42}},
        pages=lambda path: [{"id": 90, "content": {"url": "issue-url"}}],
        api=lambda *args: board_calls.append(args))
    previous = sys.modules.get("board_sync")
    sys.modules["board_sync"] = stub
    try:
        block_board("Cub-HQ/demo", {"url": "issue-url"}, "run-url", "zero runners")
    finally:
        if previous is None:
            del sys.modules["board_sync"]
        else:
            sys.modules["board_sync"] = previous
    assert board_calls[0][2] == "Blocked"
    assert board_calls[1] == ("users/Cubatica/projectsV2/4/items/90", "PATCH",
                              {"fields": [{"id": 42, "value": "zero runners"}]})
    print("runner-watch self-test: PASS (hosted/local stall, idle/young/pending exclusion, trusted markers, board+tracker, cancel fence, duplicate replay, live-job no fallback)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        if not args.repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
            parser.error("--repo OWNER/REPO required")
        watch(args.repo)


if __name__ == "__main__":
    main()
