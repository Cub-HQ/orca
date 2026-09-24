#!/usr/bin/env python3
"""Run directly: python3 tools/test_review_tier.py (isolated real git diffs)."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOL = Path(__file__).with_name('review_tier')


class ReviewTierTest(unittest.TestCase):
    def classify(self, path, before, after, repo='Cub-HQ/project'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
            git('init', '-q')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'user.name', 'Tier test')
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(before)
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            target.write_text(after)
            git('add', '.')
            git('commit', '-qm', 'candidate')
            output = root / 'output'
            result = subprocess.check_output(
                [str(TOOL), '--base', base, '--repo', repo], cwd=root,
                env={**os.environ, 'GITHUB_OUTPUT': str(output)}, text=True)
            data = json.loads(result)
            self.assertEqual(dict(line.split('=', 1) for line in output.read_text().splitlines()),
                             {key: str(value) for key, value in data.items()})
            self.assertEqual(data['timeout_minutes'], data['minutes'] + 1)
            return data

    def test_changed_boundary_not_workflow_name(self):
        cases = [
            ('.github/workflows/df-pipeline.yml', '# token gate\nrun: echo ok\n', '# secret deploy gate\nrun: echo ok\n', 'a'),
            ('worker.py', 'token = load_secret()\ncount = 1\n', 'token = load_secret()\ncount = 2\n', 'b'),
            ('worker.py', 'access_token = old()\n', 'access_token = new()\n', 'c'),
            ('.github/workflows/df-pipeline.yml', 'env:\n  KEY: none\n', 'env:\n  KEY: ${{ secrets.API_KEY }}\n', 'c'),
            ('notes.md', 'old\n', 'authorization = secrets.API_TOKEN\n', 'a'),
            ('worker.py', 'x = 1 # secret\n', 'x = 1  # token\n', 'a'),
            ('worker.py', 'x = 1\n', 'x  =  1\n\n', 'a'),
            ('worker.js', '/*\nold\n*/\nrun();\n', '/*\nsecret token\n*/\nrun();\n', 'a'),
            ('worker.py', 'url = "https://host/#old"\n', 'url = "https://host/#new"\n', 'b'),
            ('authz.py', 'def check():\n    return False\n', 'def check():\n    return True\n', 'c'),
            ('.github/workflows/df-pipeline.yml', 'run: echo old\n', 'run: echo new\n', 'b'),
            ('worker.js', 'accessToken = old();\n', 'accessToken = next();\n', 'c'),
            ('worker.py', 'refund = 1\n', 'refund = 2\n', 'c'),
            ('deploy_gate.py', 'allowed = False\n', 'allowed = True\n', 'c'),
            ('worker.py', 'if ok:\n    run()\n', 'if ok:\n  run()\n', 'b'),
            ('token_cache.py', 'count = 1\n', 'count = 2\n', 'b'),
            ('.github/workflows/df-pipeline.yml', 'permissions:\n  contents: read\n', 'permissions:\n  contents: write\n', 'c'),
            ('.github/workflows/df-pipeline.yml', 'jobs:\n  deploy:\n    if: false\n', 'jobs:\n  deploy:\n    if: true\n', 'c'),
            ('.github/workflows/df-pipeline.yml', 'jobs:\n  deploy:\n    # old\n    if: true\n', 'jobs:\n  deploy:\n    # secret token\n    if: true\n', 'a'),
            ('.github/workflows/df-pipeline.yml', 'jobs:\n  build:\n    if: false\n', 'jobs:\n  build:\n    if: true\n', 'b'),
        ]
        for path, before, after, tier in cases:
            with self.subTest(path=path, tier=tier):
                data = self.classify(path, before, after)
                self.assertEqual(data['tier'], tier)
                self.assertEqual(data['minutes'], {'a': 4, 'b': 5, 'c': 15}[tier])
                self.assertEqual(data['effort'], {'a': 'low', 'b': 'medium', 'c': 'high'}[tier])
                self.assertEqual(data['model'], 'oauth-pool/claude-opus-5' if tier == 'c' else 'oauth-pool/grok-4.6')

    def test_boundary_default_assignment(self):
        for identifier in ('DEFAULT_ROLE', 'defaultPrincipal', 'default_policy'):
            for path, tier in (('authz.py', 'c'), ('display.py', 'b'), ('authz.md', 'a')):
                with self.subTest(identifier=identifier, path=path):
                    data = self.classify(path, f"{identifier} = 'user'\n", f"{identifier} = 'admin'\n")
                    self.assertEqual(data['tier'], tier)

    def test_omp_always_five_minutes(self):
        for path, before, after, tier in [
            ('notes.md', 'old', 'new', 'a'),
            ('worker.py', 'x = 1', 'x = 2', 'b'),
            ('worker.py', 'access_token = old()', 'access_token = new()', 'c'),
        ]:
            with self.subTest(tier=tier):
                data = self.classify(path, before, after, 'Cub-HQ/omp-config-backup')
                self.assertEqual(data['tier'], tier)
                self.assertEqual(data['minutes'], 5)
                self.assertEqual(data['model'], 'oauth-pool/grok-4.6')


if __name__ == '__main__':
    unittest.main()
