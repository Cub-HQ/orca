"""Self-healing staleness sweep. Runs on a schedule; needs no human.

Fixes, without asking:
- board stage contradicting reality (labels + live runs are ground truth)
- closed issues still on active board columns
- issues with actions:go but no live/queued run for >20 min -> dispatch the existing issue
Leaves alone: needs-plan, umbrella PRDs, and true waiting labels.
"""
import io
import json
import os
import re
import subprocess
import sys
import board_sync
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

R = os.environ.get("GITHUB_REPOSITORY", "Cub-HQ/omp-config-backup")
WAIT = {"actions:needs-info", "factory:needs-info", "actions:parked",
        "factory:needs-you", "factory:awaiting-review", "factory:awaiting-merge",
        "factory:awaiting-user-review"}
PARKED_OK = {"factory:needs-plan"}


def gh(*args):
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[:300])
    return r.stdout


def derive_stage(state, labels, running, current=None, job=None, blocked_by=(), triaged=False):
    """Human gates beat live jobs; only a live run may preserve a finer stage."""
    if state.lower() == "closed":
        return "Shipped"
    if blocked_by:
        return "Blocked"
    if "factory:orch-action" in labels:
        return "Blocked"
    if set(labels) & (WAIT | PARKED_OK):
        return "Human Review Needed"
    if "factory:blocked" in labels:
        return "Blocked"
    if "factory:orch-direct" in labels:
        return "Building"
    if running:
        if job in {"Admission", "Intake"}:
            return "Triage"
        if job in {"Build", "Rework"}:
            return "Building"
        if job in {"Review", "Re-review"}:
            return "In review"
        if job and job.startswith("Source QA"):
            return "QA"
        if job == "Deploy" or (job and job.startswith(("Rebase", "Merge"))):
            return "Deploying"
        if job and job.startswith("Live Slack") and "acceptance" in job.lower():
            return "Live test"
        if job == "Verdict" and current:
            return current
        return current if current in {"In review", "QA", "Deploying", "Live test"} else "Building"
    if "actions:go" in labels and not triaged:
        return "Triage"
    return "Queued"


def format_elapsed(seconds):
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def running_for(stage, run_started_at, now, stage_updated_at=None):
    if stage not in {"Building", "In review", "QA", "Deploying", "Live test"}:
        return ""
    started = run_started_at or stage_updated_at
    return format_elapsed(now - datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()) if started else "?"


def snapshot():
    pages = json.loads(gh(f"repos/{R}/issues?state=open&per_page=100", "--paginate", "--slurp"))
    rows = [i for page in pages for i in page if "pull_request" not in i]
    issues = [{"n": i["number"], "labs": [l["name"] for l in i["labels"]],
               "updated": i["updated_at"], "issue": i} for i in rows]
    for issue in issues:
        pages = json.loads(gh(
            f"repos/{R}/issues/{issue['n']}/dependencies/blocked_by?per_page=100",
            "--paginate", "--slurp"))
        issue["blocked_by"] = [blocker for page in pages for blocker in page
                               if blocker["state"].lower() == "open"]
    pages = json.loads(gh(f"repos/{R}/actions/runs?per_page=100", "--paginate", "--slurp"))
    runs = [run for page in pages for run in page["workflow_runs"]]
    by_number = {issue["n"]: issue for issue in issues}
    active, running, jobs = set(), {}, {}
    for run in sorted(runs, key=lambda r: r.get("run_started_at") or "", reverse=True):
        match = re.search(r"#(\d+) ", run.get("display_title") or "")
        if not match or int(match[1]) not in by_number:
            continue
        n = int(match[1])
        issue = by_number[n]
        if run["status"] in {"queued", "in_progress", "pending", "waiting", "requested"}:
            active.add(n)
        if run["status"] not in {"in_progress", "completed"}:
            continue  # queued runs can have run_started_at without ever starting a job
        if issue.get("triaged") and (run["status"] != "in_progress" or n in running):
            continue
        pages = json.loads(gh(f"repos/{R}/actions/runs/{run['id']}/jobs?filter=all&per_page=100",
                              "--paginate", "--slurp"))
        run_jobs = [job for page in pages for job in page["jobs"]]
        if any(job.get("started_at") and job["status"] != "queued"
               and job.get("conclusion") != "skipped" for job in run_jobs):
            issue["triaged"] = True
        if run["status"] == "in_progress" and n not in running:
            job = next((job for job in run_jobs if job["status"] == "in_progress"), None)
            if job:
                running[n] = job.get("started_at") or run.get("run_started_at")
                jobs[n] = job["name"]
    for issue in issues:
        if "actions:go" in issue["labs"] and not issue.get("triaged"):
            pages = json.loads(gh(f"repos/{R}/issues/{issue['n']}/comments?per_page=100",
                                  "--paginate", "--slurp"))
            issue["triaged"] = any((comment.get("body") or "").startswith("## DF_Intake")
                                   for page in pages for comment in page)
    return issues, active, running, jobs


def board_items(project):
    fields = board_sync.project_fields(project)
    ids = ",".join(str(fields[name]["id"]) for name in
                   ("Workflow Stage", "Why Awaiting Human", "Running For", "Shipped At") if name in fields)
    for item in board_sync.pages(f"{board_sync.project_path(project)}/items?per_page=100&fields={ids}"):
        issue = item.get("content") or {}
        if item.get("content_type") != "Issue":
            continue
        values = {field["name"]: field.get("value") or {} for field in item.get("fields", [])}
        # REST has no field-specific timestamp; item.updated_at changes on our own duration writes.
        yield {"databaseId": item["id"],
               "stage": {"name": values.get("Workflow Stage", {}).get("name", {}).get("raw")},
               "why": {"text": values.get("Why Awaiting Human", {}).get("raw")},
               "running_for": {"text": values.get("Running For", {}).get("raw")},
               "shipped_at": values.get("Shipped At"),
               "content": {"number": issue["number"], "state": issue["state"].upper(),
                           "closed_at": issue.get("closed_at"),
                           "url": issue.get("url") or f"{issue['repository_url']}/issues/{issue['number']}",
                           "repository": {"nameWithOwner": issue["repository_url"].split("/repos/", 1)[-1]}}}


def reconcile_board(issues=None, running=None, jobs=None):
    if issues is None:
        issues, _, running, jobs = snapshot()
    labels = {i["n"]: i["labs"] for i in issues}
    blockers = {i["n"]: i.get("blocked_by", []) for i in issues}
    triaged = {i["n"]: i.get("triaged", False) for i in issues}
    additions = corrections = refreshes = 0
    now = time.time()
    for project in board_sync.projects_for(R):
        fields = board_sync.project_fields(project)
        if "Running For" not in fields:
            fields["Running For"], _ = board_sync.api(
                f"{board_sync.project_path(project)}/fields", "POST", {"name": "Running For", "data_type": "text"})
        items = list(board_items(project))
        present = {item["content"]["number"] for item in items
                   if item.get("content", {}).get("repository", {}).get("nameWithOwner") == R}
        for row in issues:
            if "issue" not in row or row["n"] in present:
                continue
            issue = row["issue"]
            stage = derive_stage(issue["state"], row["labs"], row["n"] in running,
                                 job=(jobs or {}).get(row["n"]), blocked_by=blockers.get(row["n"], []),
                                 triaged=triaged.get(row["n"], False))
            board_sync.sync_project(project, issue, stage, "", known_missing=True)
            additions += 1
            print(f"board-missing: added #{row['n']} as {stage} ({project})")
        for item in items:
            issue = item.get("content") or {}
            if issue.get("repository", {}).get("nameWithOwner") != R:
                continue
            n = issue["number"]
            if issue["state"] == "OPEN" and n not in labels:
                continue  # opened after the REST snapshot; reconcile next sweep
            current = (item.get("stage") or {}).get("name")
            stage = derive_stage(issue["state"], labels.get(n, []), n in running, current,
                                 (jobs or {}).get(n), blockers.get(n, []), triaged.get(n, False))
            current_why = (item.get("why") or {}).get("text") or ""
            desired_why = None
            if stage == "Blocked":
                if blockers.get(n):
                    desired_why = "Blocked by: " + "; ".join(
                        f"{b['html_url']} — {b['title']}" for b in blockers[n])
                elif "factory:orch-action" in labels.get(n, []):
                    desired_why = "orchestrator handling - not Josh"
            if stage == "Building" and "factory:orch-direct" in labels.get(n, []):
                desired_why = "orchestrator direct"
            clear_why = bool(current_why) and desired_why is None and (stage not in {"Human Review Needed", "Blocked"}
                         or (current_why.startswith("Blocked by: ") and desired_why is None))
            set_why = desired_why is not None and current_why != desired_why
            shipped_at = None
            if stage == "Shipped" and "Shipped At" in fields and not item.get("shipped_at") and corrections < 30:
                shipped_at = board_sync.first_ship_date(issue)
            if (stage != current or clear_why or set_why or shipped_at) and corrections < 30:
                board_sync.update_item(project, item["databaseId"],
                                       stage if stage != current else None, clear_why=clear_why,
                                       shipped_at=shipped_at)
                if set_why:
                    board_sync.api(
                        f"{board_sync.project_path(project)}/items/{item['databaseId']}", "PATCH",
                        {"fields": [{"id": fields["Why Awaiting Human"]["id"], "value": desired_why}]})
                corrections += 1
                print(f"board-drift: #{n} was {current or '(unset)'}, derived {stage}"
                      f" (project {project}" + ("; cleared Why" if clear_why else "") + ")")
                if corrections == 30:
                    print("board-drift: correction cap 30 reached; remaining drift deferred")
            duration = running_for(stage, running.get(n), now,
                                   (item.get("stage") or {}).get("updatedAt") if stage == current else None)
            if duration != ((item.get("running_for") or {}).get("text") or "") and refreshes < 100:
                board_sync.update_item(project, item["databaseId"], running_for=duration)
                refreshes += 1
                print(f"board-duration: #{n} -> {duration or '(empty)'} (project {project})")
                if refreshes == 100:
                    print("board-duration: refresh cap 100 reached; remaining durations deferred")
    print(f"board-missing: {additions} additions")
    print(f"board-drift: {corrections} corrections")
    print(f"board-duration: {refreshes} refreshes")
    return additions + corrections


def recent_closed():
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    pages = json.loads(gh(f"repos/{R}/issues?state=closed&sort=updated&direction=desc&since={since}&per_page=100",
                          "--paginate", "--slurp"))
    return [{"n": issue["number"], "labs": [label["name"] for label in issue["labels"]],
             "updated": issue["updated_at"], "issue": issue}
            for page in pages for issue in page if "pull_request" not in issue]

def self_test():
    fields = {"Running For": {"id": 1}}
    missing = [{"n": n, "labs": [], "issue": {
        "number": n, "node_id": f"I_{n}", "state": "open",
        "url": f"https://api.github.com/repos/{R}/issues/{n}"}}
        for n in range(35)]
    drift = {"n": 100, "labs": []}
    item = {"databaseId": 100, "content": {"number": 100, "state": "OPEN",
            "repository": {"nameWithOwner": R}}, "stage": {"name": "Building"}}
    additions, updates = [], []
    originals = (board_sync.projects_for, board_sync.project_fields, board_sync.sync_project,
                 board_sync.update_item, globals()["board_items"])
    try:
        board_sync.projects_for = lambda repo: (4,)
        board_sync.project_fields = lambda project: fields
        board_sync.sync_project = lambda *args, **kwargs: additions.append((args, kwargs))
        board_sync.update_item = lambda *args, **kwargs: updates.append((args, kwargs))
        globals()["board_items"] = lambda project: [item]
        with redirect_stdout(io.StringIO()):
            changed = reconcile_board(missing + [drift], {})
        assert changed == 36 and len(additions) == 35 and len(updates) == 1
        assert all(kwargs == {"known_missing": True} for _, kwargs in additions)
        board_sync.sync_project = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("forced add failure"))
        globals()["board_items"] = lambda project: []
        try:
            with redirect_stdout(io.StringIO()):
                reconcile_board(missing[:1], {})
        except RuntimeError as exc:
            assert str(exc) == "forced add failure"
        else:
            raise AssertionError("forced add failure was swallowed")
    finally:
        (board_sync.projects_for, board_sync.project_fields, board_sync.sync_project,
         board_sync.update_item, globals()["board_items"]) = originals
    print("factory-sweep self-test: PASS (35-item backfill keeps stage repair; forced add failure exits)")


def main():
    issues, active, running, jobs = snapshot()

    for i in issues:
        n, labs = i["n"], set(i["labs"])
        if n in active or i.get("blocked_by"):
            continue  # do not re-fire dispatch while a run is queued or active
        if labs & (WAIT | {"factory:blocked", "factory:orch-direct"}):
            continue
        elif "actions:go" in labs:
            # labeled go but nothing running: event was lost -> re-fire.
            # Staleness = time since the go LABEL was applied, not issue updated_at
            # (comments keep updated_at fresh forever and hid lost dispatches).
            ev = gh(f"repos/{R}/issues/{n}/events?per_page=100",
                    "--jq", '[.[] | select(.event=="labeled" and .label.name=="actions:go")] | last | .created_at // empty').strip()
            ref = ev or i["updated"]
            age = time.time() - time.mktime(time.strptime(ref, "%Y-%m-%dT%H:%M:%SZ"))
            if age > 1200:
                gh("-X", "POST", f"repos/{R}/actions/workflows/df-pipeline.yml/dispatches",
                   "-f", "ref=main", "-f", f"inputs[issue]={n}")
                print(f"#{n}: dispatched lost actions:go (label age {int(age)}s)")
        elif labs & PARKED_OK or not labs & {"actions:go"}:
            # deliberately parked (needs-plan/umbrella) or legacy leftover state labels
            stray = labs & {"actions:building", "factory:building", "factory:pr-open", "factory:reviewed"}
            for s in stray:
                gh("-X", "DELETE", f"repos/{R}/issues/{n}/labels/{s.replace(':', '%3A')}")
            if stray:
                gh("-X", "POST", f"repos/{R}/issues/{n}/labels", "-f", "labels[]=actions:go")
                print(f"#{n}: stray {stray} with no run -> requeued")

    reconcile_board(issues + recent_closed(), running, jobs)
    print("sweep done")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        main()
