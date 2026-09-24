"""Replay review workflow shell with an offline GitHub contents server."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


def script(job, name):
    workflow = yaml.safe_load((Path(__file__).parent.parent / '.github/workflows/df-pipeline.yml').read_text())
    return next(step['run'] for step in workflow['jobs'][job]['steps'] if step.get('name') == name)


class TrustedReviewFetchTests(unittest.TestCase):
    job = 'review1'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sha = 'a' * 40
        self.env = dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH'],
                        RUNNER_TEMP=str(self.root), GITHUB_ENV=str(self.root / 'env'),
                        GITHUB_RUN_ID='99', GITHUB_RUN_ATTEMPT='1', GITHUB_SHA='b' * 40,
                        GITHUB_REF='refs/heads/untrusted', GITHUB_REPOSITORY='Cub-HQ/orca',
                        R='Cub-HQ/orca', READ_TOKEN='offline', CALLS=str(self.root / 'calls'),
                        MARKER=str(self.root / 'executions'), TRUSTED_SHA=self.sha,
                        TIER='b', MODEL='test', EFFORT='low', MINUTES='1')
        gh = self.root / 'gh'
        gh.write_text('#!' + sys.executable + '\n' + '''import json, os, pathlib, sys
endpoint = next(a for a in sys.argv if a.startswith('repos/'))
with open(os.environ['CALLS'], 'a') as stream: stream.write(endpoint + '\\n')
if endpoint == 'repos/Cub-HQ/orca': print('main')
elif endpoint == 'repos/Cub-HQ/orca/commits/main': print(os.environ['TRUSTED_SHA'])
elif '/contents/' in endpoint:
    name = endpoint.split('/contents/')[1].split('?')[0]
    if name.endswith('factory_state_bootstrap.py'):
        print('import pathlib, sys; pathlib.Path(sys.argv[1]).write_text("pass\\\\n")')
    elif name.endswith('review_run.py'):
        print('import os; open(os.environ["MARKER"], "a").write(os.environ.get("RUNNER_VERSION", "mutable") + "\\\\n")')
    else: print('# trusted contents')
else: raise SystemExit('Unexpected endpoint: ' + endpoint)
''')
        gh.chmod(0o755)

    def shell(self, name):
        if name == 'Independent review' and self.job == 'review2':
            name = 'Scoped re-review'
        text = script(self.job, name)
        text = '\n'.join(line for line in text.splitlines() if not line.strip().startswith('export PATH='))
        return subprocess.run(['bash', '-eo', 'pipefail', '-c', text], env=self.env,
                              cwd=self.root, capture_output=True, text=True)

    def load(self):
        result = self.shell('Load receipt and lease guard')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.env.update(line.split('=', 1) for line in (self.root / 'env').read_text().splitlines())
        return result

    def tearDown(self):
        for path in self.root.rglob('*'):
            if path.is_dir(): path.chmod(0o700)
            else: path.chmod(0o600)

    def test_dispatch_fetches_default_branch_snapshot(self):
        result = self.load()
        calls = (self.root / 'calls').read_text().splitlines()
        fetched = [call for call in calls if '/contents/' in call]
        self.assertTrue(all(call.endswith('?ref=' + self.sha) for call in fetched), calls)
        self.assertEqual(len(fetched), 4)
        self.assertEqual(calls.count('repos/Cub-HQ/orca'), 1)
        self.assertEqual(calls.count('repos/Cub-HQ/orca/commits/main'), 1)
        self.assertIn('untrusted', result.stdout)
        manifest = Path(self.env['REVIEW_MANIFEST'])
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertEqual(self.env['RUNNER_VERSION'], self.sha + ':' + digest)
        for path in [manifest.parent, *manifest.parent.iterdir()]:
            self.assertEqual(path.stat().st_mode & 0o222, 0, str(path))
        run = self.shell('Independent review')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual((self.root / 'executions').read_text().strip(), self.sha + ':' + digest)

    def test_same_snapshot_has_same_version_across_jobs(self):
        self.load()
        version = self.env['RUNNER_VERSION']
        first = self.env['REVIEW_RUN']
        self.load()
        self.assertNotEqual(first, self.env['REVIEW_RUN'])
        self.assertEqual(version, self.env['RUNNER_VERSION'])

    def test_tampered_bundle_refuses_retry(self):
        self.load()
        state = Path(self.env['REVIEW_STATE'])
        state.mkdir(parents=True, exist_ok=True)
        (state / 'result.json').write_text(json.dumps({'retryable': True, 'reason': 'budget-exceeded'}))
        result = self.shell('Independent review')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = (self.root / 'executions').read_text()
        runner = Path(self.env['REVIEW_RUN'])
        for path in sorted(runner.parent.iterdir()):
            if not path.is_file(): continue
            original = path.read_bytes()
            path.chmod(0o600)
            path.write_bytes(original + b'\n# tampered\n')
            with self.subTest(file=path.name):
                retry = self.shell('Retry budget-exceeded review once')
                self.assertNotEqual(retry.returncode, 0, retry.stdout + retry.stderr)
                self.assertEqual((self.root / 'executions').read_text(), marker)
            path.write_bytes(original)
            path.chmod(0o444)
        retry = self.shell('Retry budget-exceeded review once')
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual((self.root / 'executions').read_text(), marker * 2)


class TrustedReReviewFetchTests(TrustedReviewFetchTests):
    job = 'review2'


if __name__ == '__main__':
    unittest.main()
