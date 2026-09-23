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


def derive_stage(state, labels, running, current=None):
    """Derive board truth; only a live run may preserve a finer pipeline stage."""
    if state.lower() == "closed":
        return "Shipped"
    if set(labels) & {"factory:needs-you", "actions:needs-info", "factory:needs-info"}:
        return "Human Review Needed"
    if "factory:blocked" in labels:
        return "Blocked"
    if running:
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
    active, running = set(), {}
    for run in sorted(runs, key=lambda r: r.get("run_started_at") or "", reverse=True):
        if run["status"] in {"queued", "in_progress", "pending"}:
            match = re.search(r"#(\d+) ", run.get("display_title") or "")
            if match:
                n = int(match[1])
                active.add(n)
                if run["status"] == "in_progress":
                    running.setdefault(n, run.get("run_started_at"))
    return issues, active, running


def board_items(number):
    query = """query($owner:String!, $number:Int!, $cursor:String) {
      user(login:$owner) { projectV2(number:$number) {
        items(first:100, after:$cursor) {
          pageInfo { hasNextPage endCursor }
          nodes { databaseId
            stage:fieldValueByName(name:"Workflow Stage") {
              ... on ProjectV2ItemFieldSingleSelectValue { name updatedAt }
            }
            why:fieldValueByName(name:"Why Awaiting Human") {
              ... on ProjectV2ItemFieldTextValue { text }
            }
            running_for:fieldValueByName(name:"Running For") {
              ... on ProjectV2ItemFieldTextValue { text }
            }
            content { ... on Issue { number state repository { nameWithOwner } } }
          }
        }
      } }
    }"""
    cursor = None
    while True:
        result, _ = board_sync.api("graphql", "POST", {"query": query, "variables": {
            "owner": board_sync.OWNER, "number": number, "cursor": cursor}})
        if result.get("errors"):
            raise RuntimeError(result["errors"])
        items = result["data"]["user"]["projectV2"]["items"]
        yield from items["nodes"]
        if not items["pageInfo"]["hasNextPage"]:
            break
        cursor = items["pageInfo"]["endCursor"]


def reconcile_board(issues=None, running=None):
    if issues is None:
        issues, _, running = snapshot()
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
            if issue.get("repository", {}).get("nameWithOwner") != R:
                continue
            n = issue["number"]
            if issue["state"] == "OPEN" and n not in labels:
                continue  # opened after the REST snapshot; reconcile next sweep
            current = (item.get("stage") or {}).get("name")
            stage = derive_stage(issue["state"], labels.get(n, []), n in running, current)
            clear_why = bool((item.get("why") or {}).get("text")) and stage not in {
                "Human Review Needed", "Blocked"}
            if (stage != current or clear_why) and corrections < 30:
                board_sync.update_item(project, item["databaseId"],
                                       stage if stage != current else None, clear_why=clear_why)
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
    issues, active, running = snapshot()

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

    reconcile_board(issues, running)
    print("sweep done")


if __name__ == "__main__":
    main()
