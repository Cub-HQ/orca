"""Self-healing staleness sweep. Runs on a schedule; needs no human.

Fixes, without asking:
- board stage contradicting reality (labels + live runs are ground truth)
- closed issues still on active board columns
- issues with actions:go but no live/queued run for >20 min -> re-fire the label
Leaves alone: needs-plan, umbrella PRDs, and true waiting labels.
"""
import json
import re
import subprocess
import board_sync
import time
from datetime import datetime

R = "Cubatica/orca"
WAIT = {"actions:needs-info", "factory:needs-info", "actions:parked",
        "factory:needs-you", "factory:awaiting-review", "factory:awaiting-merge",
        "factory:awaiting-user-review"}
PARKED_OK = {"factory:needs-plan"}


def gh(*args):
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[:300])
    return r.stdout


def derive_stage(state, labels, running, current=None, job=None):
    """Human gates beat live jobs; only a live run may preserve a finer stage."""
    if state.lower() == "closed":
        return "Shipped"
    if "factory:orch-action" in labels:
        return "Blocked"
    if set(labels) & (WAIT | PARKED_OK):
        return "Human Review Needed"
    if "factory:blocked" in labels:
        return "Blocked"
    if running:
        if job in {"Intake", "Build", "Rework"}:
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
    issues = json.loads(gh(f"repos/{R}/issues?state=open&per_page=100", "--paginate", "--slurp"))
    issues = [{"n": i["number"], "labs": [l["name"] for l in i["labels"]],
               "updated": i["updated_at"]} for page in issues for i in page if "pull_request" not in i]
    runs = json.loads(gh(f"repos/{R}/actions/runs?per_page=100"))["workflow_runs"]
    active, running, jobs = set(), {}, {}
    for run in sorted(runs, key=lambda r: r.get("run_started_at") or "", reverse=True):
        if run["status"] in {"queued", "in_progress", "pending"}:
            match = re.search(r"#(\d+) ", run.get("display_title") or "")
            if match:
                n = int(match[1])
                active.add(n)
                if run["status"] == "in_progress" and n not in running:
                    running[n] = run.get("run_started_at")
                    pages = json.loads(gh(f"repos/{R}/actions/runs/{run['id']}/jobs?per_page=100",
                                          "--paginate", "--slurp"))
                    jobs[n] = next((job["name"] for page in pages for job in page["jobs"]
                                    if job["status"] == "in_progress"), None)
    return issues, active, running, jobs


def board_items(number):
    fields = board_sync.project_fields(number)
    ids = ",".join(str(fields[name]["id"]) for name in
                   ("Workflow Stage", "Why Awaiting Human", "Running For") if name in fields)
    for item in board_sync.pages(
            f"users/{board_sync.OWNER}/projectsV2/{number}/items?per_page=100&fields={ids}"):
        issue = item.get("content") or {}
        if item.get("content_type") != "Issue":
            continue
        values = {field["name"]: field.get("value") or {} for field in item.get("fields", [])}
        # REST has no field-specific timestamp; item.updated_at changes on our own duration writes.
        yield {"databaseId": item["id"],
               "stage": {"name": values.get("Workflow Stage", {}).get("name", {}).get("raw")},
               "why": {"text": values.get("Why Awaiting Human", {}).get("raw")},
               "running_for": {"text": values.get("Running For", {}).get("raw")},
               "content": {"number": issue["number"], "state": issue["state"].upper(),
                           "repository": {"nameWithOwner": issue["repository_url"].split("/repos/", 1)[-1]}}}


def reconcile_board(issues=None, running=None, jobs=None):
    if issues is None:
        issues, _, running, jobs = snapshot()
    labels = {i["n"]: i["labs"] for i in issues}
    corrections = refreshes = 0
    now = time.time()
    for project in board_sync.PROJECTS:
        fields = board_sync.project_fields(project)
        if "Running For" not in fields:
            fields["Running For"], _ = board_sync.api(
                f"users/{board_sync.OWNER}/projectsV2/{project}/fields", "POST",
                {"name": "Running For", "data_type": "text"})
        for item in board_items(project):
            issue = item.get("content") or {}
            repo = issue.get("repository", {}).get("nameWithOwner")
            if project == 3 and repo != "Cubatica/fitness-coach":
                print(f"board-drift: foreign item {repo} #{issue.get('number')} on fitness-only project 3; skipped")
                continue
            if issue.get("repository", {}).get("nameWithOwner") != R:
                continue
            n = issue["number"]
            if issue["state"] == "OPEN" and n not in labels:
                continue  # opened after the REST snapshot; reconcile next sweep
            current = (item.get("stage") or {}).get("name")
            stage = derive_stage(issue["state"], labels.get(n, []), n in running, current,
                                 (jobs or {}).get(n))
            clear_why = bool((item.get("why") or {}).get("text")) and stage not in {
                "Human Review Needed", "Blocked"}
            orch_why = "orchestrator handling - not Josh"
            set_why = (stage == "Blocked" and "factory:orch-action" in labels.get(n, [])
                       and (item.get("why") or {}).get("text") != orch_why)
            if (stage != current or clear_why or set_why) and corrections < 30:
                board_sync.update_item(project, item["databaseId"],
                                       stage if stage != current else None, clear_why=clear_why)
                if set_why:
                    board_sync.api(
                        f"users/{board_sync.OWNER}/projectsV2/{project}/items/{item['databaseId']}",
                        "PATCH", {"fields": [{"id": fields["Why Awaiting Human"]["id"],
                                              "value": orch_why}]})
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
    print(f"board-drift: {corrections} corrections")
    print(f"board-duration: {refreshes} refreshes")
    return corrections


def main():
    issues, active, running, jobs = snapshot()

    for i in issues:
        n, labs = i["n"], set(i["labs"])
        if n in active:
            continue  # do not re-fire dispatch while a run is queued or active
        if labs & (WAIT | {"factory:blocked"}):
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
                gh("-X", "DELETE", f"repos/{R}/issues/{n}/labels/actions%3Ago")
                gh("-X", "POST", f"repos/{R}/issues/{n}/labels", "-f", "labels[]=actions:go")
                print(f"#{n}: re-fired lost actions:go (label age {int(age)}s)")
        elif labs & PARKED_OK or not labs & {"actions:go"}:
            # deliberately parked (needs-plan/umbrella) or legacy leftover state labels
            stray = labs & {"actions:building", "factory:building", "factory:pr-open", "factory:reviewed"}
            for s in stray:
                gh("-X", "DELETE", f"repos/{R}/issues/{n}/labels/{s.replace(':', '%3A')}")
            if stray:
                gh("-X", "POST", f"repos/{R}/issues/{n}/labels", "-f", "labels[]=actions:go")
                print(f"#{n}: stray {stray} with no run -> requeued")

    reconcile_board(issues, running, jobs)
    print("sweep done")


if __name__ == "__main__":
    main()
