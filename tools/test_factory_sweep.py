"""Board derivation contract; run with python -m unittest discover -s tools -p test_factory_sweep.py."""
import json
import unittest
from unittest.mock import patch

import factory_sweep

from factory_sweep import derive_stage, format_elapsed, running_for


class DeriveStageTest(unittest.TestCase):
    def test_contract(self):
        cases = [
            ("closed", ["factory:needs-you", "factory:blocked"], True, "QA", "Shipped"),
            ("open", ["factory:needs-you", "factory:blocked"], True, "QA", "Human Review Needed"),
            ("open", ["actions:needs-info"], False, None, "Human Review Needed"),
            ("open", ["factory:needs-info"], True, "Building", "Human Review Needed"),
            ("open", ["factory:blocked"], True, "Live test", "Blocked"),
            ("open", [], True, None, "Building"),
            ("open", [], True, "Human Review Needed", "Building"),
            ("open", ["actions:go"], False, "Building", "Triage"),
            ("open", [], False, None, "Queued"),
            ("open", [], False, "Human Review Needed", "Queued"),
        ]
        for label in ("factory:needs-you", "actions:needs-info", "factory:needs-info",
                      "actions:parked", "factory:awaiting-review", "factory:awaiting-merge",
                      "factory:awaiting-user-review", "factory:needs-plan"):
            cases.extend([("open", [label], False, "Queued", "Human Review Needed"),
                          ("open", [label, "factory:blocked"], True, "QA", "Human Review Needed"),
                          ("closed", [label], True, "QA", "Shipped")])
            cases.append(("open", [label, "factory:orch-action"], True, "QA", "Blocked"))
        cases.extend([("open", ["factory:orch-action"], False, None, "Blocked"),
                      ("closed", ["factory:orch-action"], True, "Blocked", "Shipped")])
        for job, expected in (("Admission", "Triage"), ("Intake", "Triage"), ("Build", "Building"),
                              ("Rework", "Building"), ("Review", "In review"),
                              ("Re-review", "In review"), ("Source QA (read-only)", "QA"),
                              ("Rebase", "Deploying"), ("Merge PR", "Deploying"),
                              ("Deploy", "Deploying"), ("Live Slack desktop acceptance", "Live test")):
            with self.subTest(job=job):
                self.assertEqual(derive_stage("open", [], True, "Building", job), expected)
                self.assertEqual(derive_stage("open", [], False, "Building", job), "Queued")
                self.assertEqual(derive_stage("open", ["factory:awaiting-review"], True,
                                              "Building", job), "Human Review Needed")
        self.assertEqual(derive_stage("open", [], True, "Live test", "Verdict"), "Live test")
        self.assertEqual(derive_stage("open", [], True, "Building", "unknown"), "Building")
        for stage in ("In review", "QA", "Deploying", "Live test"):
            cases.extend([("open", [], True, stage, stage),
                          ("open", [], False, stage, "Queued")])
        for state, labels, running, current, expected in cases:
            with self.subTest(state=state, labels=labels, running=running, current=current):
                self.assertEqual(derive_stage(state, labels, running, current), expected)

    def test_orchestrator_direct_lifecycle(self):
        label = "factory:orch-direct"
        self.assertEqual(derive_stage("open", [label], False), "Building")
        self.assertEqual(derive_stage("closed", [label], False), "Shipped")
        self.assertEqual(derive_stage("open", [label, "factory:needs-you"], False), "Human Review Needed")
        self.assertEqual(derive_stage("open", [label], False, blocked_by=[7]), "Blocked")
        item = {"databaseId": 45, "content": {"number": 45, "state": "OPEN",
                "repository": {"nameWithOwner": factory_sweep.R}},
                "stage": {"name": "Queued"}, "why": {"text": ""},
                "running_for": {"text": "?"}}
        fields = {"Running For": {"id": 1}, "Why Awaiting Human": {"id": 2}}
        with patch.object(factory_sweep.board_sync, "PROJECTS", (4,)), \
             patch.object(factory_sweep.board_sync, "project_fields", return_value=fields), \
             patch.object(factory_sweep, "board_items", return_value=[item]), \
             patch.object(factory_sweep.board_sync, "update_item") as update, \
             patch.object(factory_sweep.board_sync, "api") as api, patch("builtins.print"):
            issues = [{"n": 45, "labs": [label, "actions:go", "factory:building"]}]
            self.assertEqual(factory_sweep.reconcile_board(issues, {}), 1)
            self.assertEqual(update.call_args.args[2], "Building")
            self.assertEqual(api.call_args.args[2]["fields"], [{"id": 2, "value": "orchestrator direct"}])
            item["stage"]["name"] = "Building"
            item["why"]["text"] = "orchestrator direct"
            self.assertEqual(factory_sweep.reconcile_board(issues, {}), 0)
            with patch.object(factory_sweep, "snapshot", return_value=(issues, set(), {}, {})), \
                 patch.object(factory_sweep, "gh", side_effect=AssertionError("must not requeue direct work")):
                factory_sweep.main()

    def test_board_routing(self):
        board = factory_sweep.board_sync
        for repo, expected in (("fitness-coach", [3, 4]), ("omp-config-backup", [4]),
                               ("df-fixture", [4]), ("orca", [4])):
            with self.subTest(repo=repo), \
                 patch.object(board.sys, "argv", ["board_sync.py", "--repo", f"Cub-HQ/{repo}",
                                                   "--issue", "45", "--stage", "Human Review Needed"]), \
                 patch.object(board, "api", return_value=({"state": "open"}, None)), \
                 patch.object(board, "sync_project") as sync, patch("builtins.print"):
                board.main()
                self.assertEqual([call.args[0] for call in sync.call_args_list], expected)

    def test_foreign_item_never_updated_on_fitness_board(self):
        item = {"databaseId": 45, "content": {"number": 45, "state": "OPEN",
                "repository": {"nameWithOwner": "Cub-HQ/omp-config-backup"}},
                "stage": {"name": "Queued"}}
        with patch.object(factory_sweep, "R", "Cub-HQ/omp-config-backup"), \
             patch.object(factory_sweep.board_sync, "PROJECTS", (3, 4)), \
             patch.object(factory_sweep.board_sync, "project_fields", return_value={"Running For": {"id": 1}}), \
             patch.object(factory_sweep, "board_items", return_value=[item]), \
             patch.object(factory_sweep.board_sync, "update_item") as update, patch("builtins.print"):
            self.assertEqual(factory_sweep.reconcile_board(
                [{"n": 45, "labs": ["factory:awaiting-review"]}], {}), 1)
        self.assertEqual([call.args[0] for call in update.call_args_list], [4])

    def test_orchestrator_action_repairs_stage_and_why(self):
        reason = "orchestrator handling - not Josh"
        fields = {"Running For": {"id": 1}, "Why Awaiting Human": {"id": 2},
                  "Workflow Stage": {"id": 3, "options": [
                      {"id": "blocked", "name": {"raw": "Blocked"}}]}}
        for current, why in (("Human Review Needed", "needs Josh"),
                             ("Blocked", "needs Josh"), ("Blocked", reason)):
            item = {"databaseId": 45, "content": {"number": 45, "state": "OPEN",
                    "repository": {"nameWithOwner": factory_sweep.R}},
                    "stage": {"name": current}, "why": {"text": why}}
            with self.subTest(current=current, why=why), \
                 patch.object(factory_sweep.board_sync, "PROJECTS", (4,)), \
                 patch.object(factory_sweep.board_sync, "project_fields", return_value=fields), \
                 patch.object(factory_sweep, "board_items", return_value=[item]), \
                 patch.object(factory_sweep.board_sync, "api") as api, patch("builtins.print"):
                self.assertEqual(factory_sweep.reconcile_board(
                    [{"n": 45, "labs": ["factory:orch-action", "factory:needs-you"]}],
                    {45: "2026-09-23T00:00:00Z"}, {45: "Review"}), int(why != reason))
            updates = [field for call in api.call_args_list for field in call.args[2]["fields"]]
            self.assertEqual(updates, ([{"id": 3, "value": "blocked"}]
                                      if current != "Blocked" else []) +
                             ([{"id": 2, "value": reason}] if why != reason else []))

    def test_dependency_snapshot_and_board_lifecycle(self):
        blocker = {"state": "open", "html_url": "https://github.com/Cub-HQ/orca/issues/7",
                   "title": "Repair release"}
        fields = {"Running For": {"id": 1}, "Why Awaiting Human": {"id": 2},
                  "Workflow Stage": {"id": 3, "options": [
                      {"id": stage, "name": {"raw": stage}}
                      for stage in ("Blocked", "Queued", "Human Review Needed")]}}
        for dependencies, labels, expected in (([[blocker]], ["factory:needs-you"], "Blocked"),
                ([[dict(blocker, state="closed")]], [], "Queued"), ([[]], [], "Queued"),
                ([[]], ["factory:needs-you"], "Human Review Needed")):
            with self.subTest(dependencies=dependencies, labels=labels):
                replies = [json.dumps([[{"number": 45, "labels": [{"name": x} for x in labels],
                                        "updated_at": "2026-09-23T00:00:00Z"}]]),
                           json.dumps(dependencies), json.dumps([{"workflow_runs": []}])]
                with patch.object(factory_sweep, "gh", side_effect=replies):
                    issues, _, running, jobs = factory_sweep.snapshot()
                item = {"databaseId": 45, "content": {"number": 45, "state": "OPEN",
                        "repository": {"nameWithOwner": factory_sweep.R}},
                        "stage": {"name": "Human Review Needed" if expected == "Blocked" else "Blocked"},
                        "why": {"text": "Blocked by: old dependency"}}
                with patch.object(factory_sweep.board_sync, "PROJECTS", (4,)), \
                     patch.object(factory_sweep.board_sync, "project_fields", return_value=fields), \
                     patch.object(factory_sweep, "board_items", return_value=[item]), \
                     patch.object(factory_sweep.board_sync, "api") as api, patch("builtins.print"):
                    self.assertEqual(factory_sweep.reconcile_board(issues, running, jobs), 1)
                updates = [field for call in api.call_args_list for field in call.args[2]["fields"]]
                self.assertEqual(updates, [{"id": 3, "value": expected},
                    {"id": 2, "value": ("Blocked by: " + blocker["html_url"] + " — " + blocker["title"])
                     if expected == "Blocked" else None}])

    def test_triage_snapshot_reconciles_real_evidence(self):
        started = "2026-09-23T00:00:00Z"
        cases = [
            ("new", [], [], [], [], "Triage"),
            ("queued timestamp is not started", ["queued"], [], [], [], "Triage"),
            ("pending timestamp is not started", ["pending"], [], [], [], "Triage"),
            ("skipped run", ["completed"], [("Intake", "completed", "skipped")], [], [], "Triage"),
            ("intake", ["in_progress"], [("Intake", "in_progress", None)], [], [], "Triage"),
            ("build", ["in_progress"], [("Build", "in_progress", None)], [], [], "Building"),
            ("waiting for runner", ["in_progress"], [("Build", "queued", None)], ["## DF_Intake\nINTAKE=go"], [], "Queued"),
            ("old run then requeued", ["queued", "completed"], [("Intake", "completed", "success")], [], [], "Queued"),
            ("old receipt", [], [], ["## DF_Intake\nINTAKE=go"], [], "Queued"),
            ("receipt mention is not receipt", [], [], ["Waiting for ## DF_Intake"], [], "Triage"),
            ("human precedence", ["in_progress"], [("Intake", "in_progress", None)], [], ["factory:needs-you"], "Human Review Needed"),
            ("blocked precedence", [], [], [], ["factory:blocked"], "Blocked"),
            ("direct precedence", [], [], [], ["factory:orch-direct"], "Building"),
        ]
        for name, statuses, job_rows, comments, labels, expected in cases:
            with self.subTest(name=name):
                def github(path, *args):
                    self.assertEqual(args, ("--paginate", "--slurp"))
                    if "/dependencies/blocked_by?" in path:
                        return json.dumps([[]])
                    if "/comments?" in path:
                        return json.dumps([[{"body": "unrelated"}], [{"body": body} for body in comments]])
                    if "/jobs?" in path:
                        return json.dumps([{"jobs": []}, {"jobs": [
                            {"name": job, "status": status, "conclusion": conclusion,
                             "started_at": None if status == "queued" else started}
                            for job, status, conclusion in job_rows]}])
                    if "/actions/runs?" in path:
                        return json.dumps([{"workflow_runs": []}] + [{"workflow_runs": [
                            {"id": index, "display_title": "#45 issue", "status": status,
                             "run_started_at": started}]} for index, status in enumerate(statuses)])
                    self.assertIn("/issues?state=open", path)
                    return json.dumps([[{"number": 45, "updated_at": started,
                                         "labels": [{"name": label} for label in ["actions:go"] + labels]}]])

                item = {"databaseId": 45, "content": {"number": 45, "state": "OPEN",
                        "repository": {"nameWithOwner": factory_sweep.R}}, "stage": {"name": "(unset)"}}
                with patch.object(factory_sweep, "gh", side_effect=github), \
                     patch.object(factory_sweep.board_sync, "PROJECTS", (4,)), \
                     patch.object(factory_sweep.board_sync, "project_fields", return_value={
                         "Running For": {"id": 1}, "Why Awaiting Human": {"id": 2}}), \
                     patch.object(factory_sweep, "board_items", return_value=[item]), \
                     patch.object(factory_sweep.board_sync, "update_item") as update, \
                     patch.object(factory_sweep.board_sync, "api"), patch("builtins.print"):
                    self.assertEqual(factory_sweep.reconcile_board(), 1)
                self.assertEqual(update.call_args_list[0].args[2], expected)

    def test_cancelled_worker_and_skipped_successors_clear_building(self):
        for conclusion, queued in (("cancelled", False), ("failure", False), ("cancelled", True)):
            with self.subTest(conclusion=conclusion, queued=queued):
                started = "2026-09-23T00:00:00Z"
                def github(path, *args):
                    if "/dependencies/" in path:
                        return json.dumps([[]])
                    if "/actions/runs?" in path:
                        return json.dumps([{"workflow_runs": ([
                            {"id": 3, "display_title": "#31 manual", "status": "queued",
                             "run_started_at": "2026-09-23T02:00:00Z"}] if queued else []) + [
                            {"id": 2, "display_title": "#31 manual", "status": "completed",
                             "conclusion": "skipped", "run_started_at": "2026-09-23T01:00:00Z"}]},
                            {"workflow_runs": [{"id": 1, "display_title": "#31 manual",
                             "status": "completed", "conclusion": conclusion, "run_started_at": started}]}])
                    if "/jobs?" in path:
                        return json.dumps([{"jobs": [{"name": "Build" if "/1/" in path else "Intake",
                            "status": "completed", "started_at": started,
                            "conclusion": conclusion if "/1/" in path else "skipped"}]}])
                    self.assertIn("/issues?state=open", path)
                    return json.dumps([[{"number": 31, "updated_at": started,
                        "labels": [{"name": "actions:go"}, {"name": "factory:building"}]}]])

                item = {"databaseId": 31, "content": {"number": 31, "state": "OPEN",
                        "repository": {"nameWithOwner": factory_sweep.R}},
                        "stage": {"name": "Building"}, "running_for": {"text": "00:50:00"}}
                with patch.object(factory_sweep, "gh", side_effect=github):
                    issues, active, running, jobs = factory_sweep.snapshot()
                self.assertEqual(active, {31} if queued else set())
                self.assertEqual(running, {})
                with patch.object(factory_sweep.board_sync, "PROJECTS", (4,)), \
                     patch.object(factory_sweep.board_sync, "project_fields", return_value={"Running For": {"id": 1}}), \
                     patch.object(factory_sweep, "board_items", return_value=[item]), \
                     patch.object(factory_sweep.board_sync, "update_item") as update, patch("builtins.print"):
                    self.assertEqual(factory_sweep.reconcile_board(issues, running, jobs), 1)
                self.assertEqual(update.call_args_list[0].args[2], "Queued")
                self.assertEqual(update.call_args_list[1].kwargs, {"running_for": ""})

    def test_board_progress_tracks_pipeline_milestones(self):
        board = factory_sweep.board_sync
        milestones = {"Queued": 0, "Triage": 2, "Building": 3, "In review": 4,
                      "QA": 7, "Deploying": 9, "Shipped": 10}
        fields = {"Workflow Stage": {"id": 1, "options": [
            {"id": name, "name": {"raw": name}} for name in milestones]}, "Workflow Progress": {"id": 2}}
        for name, count in milestones.items():
            with self.subTest(stage=name), patch.object(board, "project_fields", return_value=fields), \
                 patch.object(board, "api") as api:
                board.update_item(4, 45, name)
            self.assertEqual(api.call_args.args[2]["fields"], [
                {"id": 1, "value": name}, {"id": 2, "value": "▓" * count + "░" * (10 - count) + f" {count}/10"}])

    def test_ship_date_transition_backfill_and_reopen(self):
        board = factory_sweep.board_sync
        fields = {"Running For": {"id": 1}, "Shipped At": {"id": 2},
                  "Workflow Stage": {"id": 3, "options": [
                      {"id": stage, "name": {"raw": stage}} for stage in ("Shipped", "Queued")]}}
        issue = {"number": 45, "state": "closed", "closed_at": "2026-09-20T12:00:00Z",
                 "url": f"https://api.github.com/repos/{factory_sweep.R}/issues/45",
                 "repository_url": f"https://api.github.com/repos/{factory_sweep.R}"}
        for current in ("Building", "Shipped"):
            with self.subTest(current=current):
                row = {"id": 45, "content_type": "Issue", "content": dict(issue), "fields": [
                    {"name": "Workflow Stage", "value": {"name": {"raw": current}}}]}
                def persist(path, method, body):
                    for update in body["fields"]:
                        if update["id"] == 2:
                            row["fields"].append({"name": "Shipped At", "value": update["value"] + "T00:00:00+00:00"})
                        if update["id"] == 3:
                            row["fields"][0]["value"]["name"]["raw"] = update["value"]
                with patch.object(board, "PROJECTS", (4,)), \
                     patch.object(board, "project_fields", return_value=fields), \
                     patch.object(board, "pages", return_value=[row]) as pages, \
                     patch.object(board, "api", side_effect=persist) as api, patch("builtins.print"):
                    self.assertEqual(factory_sweep.reconcile_board([], {}), 1)
                    self.assertIn({"id": 2, "value": "2026-09-20"}, api.call_args.args[2]["fields"])
                    self.assertIn("fields=3,1,2", pages.call_args.args[0])
                    api.reset_mock()
                    self.assertEqual(factory_sweep.reconcile_board([], {}), 0)
                    api.assert_not_called()
                    row["content"]["state"] = "open"
                    factory_sweep.reconcile_board([{"n": 45, "labs": []}], {})
                    self.assertNotIn(2, [f["id"] for f in api.call_args.args[2]["fields"]])
                    api.reset_mock()
                    row["fields"] = row["fields"][:1]
                    factory_sweep.reconcile_board([{"n": 45, "labs": []}], {})
                    api.assert_not_called()

        for stage, existing, expected in (("Shipped", None, "2026-09-20"),
                ("Shipped", "2026-09-19", None), ("Queued", None, None), ("Queued", "2026-09-19", None)):
            row = {"id": 45, "content": issue, "fields": [
                {"name": "Shipped At", "value": existing + "T00:00:00+00:00" if existing else None}]}
            with self.subTest(stage=stage, existing=existing), \
                 patch.object(board, "project_fields", return_value=fields), \
                 patch.object(board, "pages", return_value=[row]), \
                 patch.object(board, "api") as api:
                board.sync_project(4, issue, stage, "")
            dates = [f["value"] for f in api.call_args.args[2]["fields"] if f["id"] == 2]
            self.assertEqual(dates, [expected] if expected else [])

    def test_ship_date_missing_close_requires_closing_pull_evidence(self):
        board = factory_sweep.board_sync
        issue = {"state": "closed", "url": "https://api.github.com/repos/Cub-HQ/orca/issues/45"}
        for will_close, closing_commit, expected in ((True, None, "2026-09-18"),
                (False, "merge", "2026-09-18"), (False, "other", "2026-09-23"),
                (False, None, "2026-09-23")):
            timeline = [{"will_close_target": will_close, "source": {"issue": {
                "pull_request": {"url": "https://api.github.com/repos/Cub-HQ/orca/pulls/46"}}}},
                {"event": "closed", "commit_id": closing_commit}]
            with self.subTest(will_close=will_close, closing_commit=closing_commit), \
                 patch.object(board, "pages", return_value=timeline), \
                 patch.object(board, "datetime") as clock, \
                 patch.object(board, "api", side_effect=[(issue, None),
                     ({"merged_at": "2026-09-18T13:00:00Z", "merge_commit_sha": "merge"}, None)]):
                clock.now.return_value.isoformat.return_value = "2026-09-23T00:00:00Z"
                self.assertEqual(board.first_ship_date(issue), expected)

    def test_running_for(self):
        self.assertEqual(format_elapsed(25 * 3600), "25:00:00")
        self.assertEqual(format_elapsed(3661.9), "01:01:01")
        self.assertEqual(format_elapsed(-1), "00:00:00")
        for stage in ("Building", "In review", "QA", "Deploying", "Live test"):
            with self.subTest(stage=stage):
                self.assertEqual(running_for(stage, "2026-09-23T00:00:00Z", 1790125261), "01:01:01")
                self.assertEqual(running_for(stage, None, 1790125261), "?")
                self.assertEqual(running_for(stage, None, 1790125261,
                                             "2026-09-23T00:00:00Z"), "01:01:01")
        for stage in ("Queued", "Shipped", "Human Review Needed", "Feedback given", "Blocked"):
            with self.subTest(stage=stage):
                self.assertEqual(running_for(stage, "2026-09-23T00:00:00Z", 1790125261), "")

    def test_durations_continue_after_correction_cap(self):
        items = [{"databaseId": n, "content": {"number": n, "state": "OPEN",
                  "repository": {"nameWithOwner": factory_sweep.R}},
                  "stage": {"name": "Queued"}} for n in range(101)]
        issues = [{"n": n, "labs": []} for n in range(101)]
        running = {n: "2026-09-23T00:00:00Z" for n in range(101)}
        with patch.object(factory_sweep.board_sync, "PROJECTS", (4,)), \
             patch.object(factory_sweep.board_sync, "project_fields", return_value={"Running For": {"id": 1}}), \
             patch.object(factory_sweep, "board_items", return_value=items), \
             patch.object(factory_sweep.board_sync, "update_item") as update, \
             patch("builtins.print"):
            self.assertEqual(factory_sweep.reconcile_board(issues, running), 30)
        self.assertEqual(sum("running_for" in call.kwargs for call in update.call_args_list), 100)
        self.assertEqual(sum("clear_why" in call.kwargs for call in update.call_args_list), 30)


if __name__ == "__main__":
    unittest.main()
