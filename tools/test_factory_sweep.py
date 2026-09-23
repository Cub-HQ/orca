"""Board derivation contract; run with python -m unittest discover -s tools -p test_factory_sweep.py."""
import unittest

from factory_sweep import derive_stage


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


if __name__ == "__main__":
    unittest.main()
