"""Board derivation contract; run with python -m unittest discover -s tools -p test_factory_sweep.py."""
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
        for stage in ("In review", "QA", "Deploying", "Live test"):
            cases.extend([("open", [], True, stage, stage),
                          ("open", [], False, stage, "Queued")])
        for state, labels, running, current, expected in cases:
            with self.subTest(state=state, labels=labels, running=running, current=current):
                self.assertEqual(derive_stage(state, labels, running, current), expected)

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
        with patch.object(factory_sweep.board_sync, "PROJECTS", (3,)), \
             patch.object(factory_sweep.board_sync, "project_fields", return_value={"Running For": {"id": 1}}), \
             patch.object(factory_sweep, "board_items", return_value=items), \
             patch.object(factory_sweep.board_sync, "update_item") as update, \
             patch("builtins.print"):
            self.assertEqual(factory_sweep.reconcile_board(issues, running), 30)
        self.assertEqual(sum("running_for" in call.kwargs for call in update.call_args_list), 100)
        self.assertEqual(sum("clear_why" in call.kwargs for call in update.call_args_list), 30)


if __name__ == "__main__":
    unittest.main()
