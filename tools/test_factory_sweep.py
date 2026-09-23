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
            ("open", ["actions:go"], False, "Building", "Queued"),
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
        for job, expected in (("Intake", "Building"), ("Build", "Building"),
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

    def test_board_routing(self):
        board = factory_sweep.board_sync
        for repo, expected in (("fitness-coach", [3, 4]), ("omp-config-backup", [4]),
                               ("df-fixture", [4]), ("orca", [4])):
            with self.subTest(repo=repo), \
                 patch.object(board.sys, "argv", ["board_sync.py", "--repo", f"Cubatica/{repo}",
                                                   "--issue", "45", "--stage", "Human Review Needed"]), \
                 patch.object(board, "api", return_value=({"state": "open"}, None)), \
                 patch.object(board, "sync_project") as sync, patch("builtins.print"):
                board.main()
                self.assertEqual([call.args[0] for call in sync.call_args_list], expected)

    def test_foreign_item_never_updated_on_fitness_board(self):
        item = {"databaseId": 45, "content": {"number": 45, "state": "OPEN",
                "repository": {"nameWithOwner": "Cubatica/omp-config-backup"}},
                "stage": {"name": "Queued"}}
        with patch.object(factory_sweep, "R", "Cubatica/omp-config-backup"), \
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
        blocker = {"state": "open", "html_url": "https://github.com/Cubatica/orca/issues/7",
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
                           json.dumps(dependencies), json.dumps({"workflow_runs": []})]
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
