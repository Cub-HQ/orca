#!/usr/bin/env python3
"""Leave a factual final receipt for a closed factory issue (REST only)."""
import argparse
import json
import re
import subprocess


def api(path, payload=None, pages=False):
    command = ["gh", "api", path]
    if pages:
        command += ["--paginate", "--slurp"]
    if payload is not None:
        command += ["--input", "-"]
    result = json.loads(subprocess.check_output(
        command, input=json.dumps(payload) if payload is not None else None, text=True))
    return [item for page in result for item in page] if pages else result


def epitaph(repo, number, issue, events):
    # Only the current closure cycle supplies a reason; old closures can be superseded.
    reopened = max((i for i, e in enumerate(events) if e.get("event") == "reopened"), default=-1)
    events = events[reopened + 1:]
    closure = next((e for e in reversed(events) if e.get("event") == "closed"), {})
    target = None
    moved = None
    reference = r"(https://github\.com/[\w.-]+/[\w.-]+/issues/\d+|[\w.-]+/[\w.-]+#\d+|#\d+)"
    for event in events:
        if event.get("event") == "marked_as_duplicate":
            canonical = event.get("canonical") or event.get("duplicate_of") or {}
            if isinstance(canonical, dict) and canonical.get("html_url"):
                target = canonical["html_url"]
        if event.get("event") == "unmarked_as_duplicate":
            target = None
        if event.get("event") != "commented":
            continue
        # A random mention is not a duplicate, nor is our own previous receipt evidence.
        body = event.get("body") or ""
        if body.startswith("## Where this ended up"):
            continue
        trusted = event.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"} or event.get("user", {}).get("login") == "cub-orchestrator[bot]"
        if not trusted:
            continue
        match = re.search(r"(?im)^\s*duplicate of\s+" + reference, body)
        move = re.search(r"(?im)^\s*(?:work moved to|moved to|superseded by)\s+" + reference, body)
        if match:
            target = match.group(1)
        if move:
            moved = move.group(1)

    def link(ref):
        if ref.startswith("https://"):
            return ref
        owner, n = ref.rsplit("#", 1)
        return f"https://github.com/{owner or repo}/issues/{n}"

    if target:
        ending = f"This issue is closed because it's a duplicate of {target}."
        destination = f"**Where to look now:** {link(target)} — the work moved there."
    else:
        # Cross-references alone prove neither shipment nor duplication. GitHub must
        # say this PR closes this issue, or the closing commit must be its merge.
        shipped = None
        if closure.get("commit_url") and closure.get("commit_id"):
            commit_path = closure["commit_url"].removeprefix("https://api.github.com/")
            for pr in api(commit_path + "/pulls?per_page=100", pages=True):
                if pr.get("merged_at") and pr.get("merge_commit_sha") == closure["commit_id"]:
                    shipped = pr
                    break
        for event in reversed(events) if shipped is None else []:
            source = event.get("source", {}).get("issue", {})
            pull = source.get("pull_request", {})
            if not pull or not source.get("url", "").startswith("https://api.github.com/repos/"):
                continue
            pr = api(source["url"].replace("https://api.github.com/", "").replace("/issues/", "/pulls/"))
            if pr.get("merged_at") and (event.get("will_close_target") is True or
                    (closure.get("commit_id") and closure["commit_id"] == pr.get("merge_commit_sha"))):
                shipped = pr
                break
        if shipped:
            ending = f"This issue is closed: shipped via PR #{shipped['number']} (merged into the shared code)."
            destination = f"**Where to look now:** {shipped['html_url']}. This confirms the merge, not a separate live deployment."
        else:
            actor = (issue.get("closed_by") or {}).get("login") or (closure.get("actor") or {}).get("login")
            who = "Josh" if actor == "Cubatica" else (actor or "the issue owner")
            ending = f"This issue was closed by {who}; factory stopped."
            destination = f"**Where to look now:** {link(moved)} — the work moved there." if moved else "No replacement issue or shipped fix is confirmed by the closure evidence."
    return ("## Where this ended up (plain English)\n\n**" + ending + "**\n\n"
            "What happened, in order:\n1. This issue was closed.\n"
            "2. This factory run stops here. Earlier ‘not shipped’ or ‘For Josh’ comments describe an older point in the run, not a current request.\n\n"
            + destination + "\n\nNothing is waiting on Josh here.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    args = parser.parse_args()
    path = f"repos/{args.repo}/issues/{args.issue}"
    issue = api(path)
    if issue["state"] != "closed":
        return
    events = api(path + "/timeline?per_page=100", pages=True)
    body = epitaph(args.repo, args.issue, issue, events)
    comments = api(path + "/comments?per_page=100", pages=True)
    # Check the actual tail, not whether this receipt appeared somewhere before.
    if comments and comments[-1].get("body") == body:
        return
    if api(path)["state"] == "closed":
        api(path + "/comments", {"body": body})


if __name__ == "__main__":
    main()
