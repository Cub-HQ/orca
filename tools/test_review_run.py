#!/usr/bin/env python3
"""Disposable real CLI proof; fake only external GitHub and OMP executables."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).with_name('review_run.py')
GH = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
p = Path(os.environ['FAKE_DATA'])
d = json.loads(p.read_text())
args = sys.argv[1:]; route = args[1]; method = args[args.index('-X')+1]
if d.get('quota'):
    print('API rate limit exceeded', file=sys.stderr); sys.exit(1)
if d.get('checks_fail') and '/commits/' in route:
    print('unexpected check lookup', file=sys.stderr); sys.exit(1)
if method != 'GET':
    body = json.load(sys.stdin)
    with open(os.environ['FAKE_POSTS'], 'a') as f: f.write(json.dumps([method, route, body])+'\\n')
    if method == 'POST': time.sleep(d.get('post_delay', 0))
    print(json.dumps({'id': 99, 'number': 77})); sys.exit()
if '/pulls/' in route: value = {'state':'open','head':{'sha':d['head']},'body':'acceptance'}
elif '/comments?' in route: value = d.get('comments', [])
elif '/check-runs?' in route: value = {'check_runs':[]}
elif route.endswith('/status'): value = {'statuses':[]}
elif '/issues?' in route: value = [{'number':77,'title':'Review budget overruns'}]
else: value = {'title':'bug','body':'acceptance','labels':[],'state':'open'}
print(json.dumps([value] if '--slurp' in args else value))
'''
OMP = '''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
assert not any(k in os.environ for k in ('GH_TOKEN', 'GITHUB_TOKEN', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN', 'BOARD_TOKEN', 'APP_TOKEN', 'GIT_CONFIG_COUNT', 'GIT_ASKPASS'))
if (Path.cwd() / 'sleep-review').exists(): __import__('time').sleep(10)
brief = Path(sys.argv[-1][1:]).read_text()
path = re.search(r'Write your independently produced verdict to (.+) \\(not a GitHub comment\\)', brief)[1]
head = re.search(r'exact HEAD_SHA=([0-9a-f]{40})', brief)[1]
Path(path).write_text('## DF_Reviewer\\nIndependent evidence: inspected changed output.\\nHEAD_SHA='+head+'\\nDF_REVIEW=approve\\n')
print(json.dumps({'type':'message_end'}))
'''


class ReviewCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        helpers = self.root / 'trusted'
        helpers.mkdir()
        for name in ('review_run.py', 'factory_receipts.py'):
            (helpers / name).write_bytes(RUNNER.with_name(name).read_bytes())
        (helpers / 'DF_Reviewer.md').write_bytes((RUNNER.parent.parent / 'agent/agents/DF_Reviewer.md').read_bytes())
        self.runner = helpers / 'review_run.py'
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.checkout / 'value').write_text('before\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'before')
        self.base = self.git('rev-parse', 'HEAD')
        (self.checkout / 'value').write_text('after\n')
        self.git('commit', '-qam', 'after')
        self.head = self.git('rev-parse', 'HEAD')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name, code in [('gh', GH), ('omp', OMP)]:
            path = self.bin / name
            path.write_text(code)
            path.chmod(0o755)
        self.data = self.root / 'data.json'
        self.posts = self.root / 'posts.jsonl'
        self.state = self.root / 'state'
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        FAKE_DATA=str(self.data), FAKE_POSTS=str(self.posts))
        for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN', 'BOARD_TOKEN', 'APP_TOKEN', 'GIT_CONFIG_COUNT', 'GIT_ASKPASS'):
            self.env[key] = 'sensitive-must-not-reach-reviewer'
        self.env['GIT_CONFIG_COUNT'] = '0'
        self.env.pop('GITHUB_OUTPUT', None)
        self.env.pop('GITHUB_STEP_SUMMARY', None)
        self.set_data()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.checkout), *args], text=True).strip()

    def set_data(self, **kwargs):
        self.data.write_text(json.dumps(dict(head=self.head, **kwargs)))

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, str(self.runner), *args, '--state', str(self.state)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def prepare(self, *args, code=0):
        return self.cli('prepare', '--repo', 'owner/repo', '--pr', '3', '--checkout', str(self.checkout), '--base', self.base, *args, code=code)

    def receipt(self, verdict, head=None, **kwargs):
        return dict(id=1, author_association='MEMBER', body=f'## DF_Reviewer\nHEAD_SHA={head or self.head}\nFINDING_ID=value::output::correct OPEN\nDF_REVIEW={verdict}', **kwargs)

    def test_reuse_both_verdicts_and_pipeline_fence(self):
        for verdict in ('approve', 'block'):
            self.set_data(comments=[self.receipt(verdict)])
            self.prepare()
            state = json.loads((self.state / 'state.json').read_text())
            self.assertEqual((state['reuse'], state['verdict']), ('true', verdict))
        self.prepare('--pipeline-stage', 'review1')
        self.assertEqual(json.loads((self.state / 'state.json').read_text())['reuse'], 'false')
        spoof = self.receipt('approve'); spoof['author_association'] = 'NONE'
        self.set_data(comments=[spoof])
        self.prepare()
        self.assertEqual(json.loads((self.state / 'state.json').read_text())['reuse'], 'false')

    def test_delta_and_open_findings(self):
        receipt = self.receipt('block', self.base)
        receipt['body'] += '\nFINDING_ID=value::cleared::done RESOLVED\n'
        self.set_data(comments=[receipt])
        self.prepare()
        brief = (self.state / 'brief.txt').read_text()
        self.assertIn('value::output::correct', brief)
        self.assertNotIn('value::cleared::done', brief)
        self.assertIn('+after', (self.state / 'diff.patch').read_text())
        self.assertEqual(json.loads((self.state / 'state.json').read_text())['diff_base'], self.base)

    def test_actual_run_timing_posting_and_tracking(self):
        self.set_data(post_delay=0.08)
        self.prepare()
        self.cli('run', '--tier', 'A', '--model', 'oauth-pool/test', '--effort', 'low', '--minutes', '1')
        result = json.loads((self.state / 'result.json').read_text())
        self.assertGreaterEqual(result['review_seconds'], 0.08)
        writes = [json.loads(line) for line in self.posts.read_text().splitlines()]
        patched = next(row[2]['body'] for row in writes if row[0] == 'PATCH')
        self.assertIn('REVIEW_SECONDS=', patched)
        self.assertTrue(patched.endswith('DF_REVIEW=approve'))
        self.assertEqual(sum('/issues/77/comments' in row[1] for row in writes), 0)

    def test_quota_is_retryable_without_verdict(self):
        self.set_data(quota=True)
        self.prepare(code=75)
        self.assertTrue(json.loads((self.state / 'result.json').read_text())['retryable'])
        self.assertFalse(self.posts.exists())

    def test_reuse_never_fetches_checks(self):
        self.set_data(comments=[self.receipt('block')], checks_fail=True)
        self.prepare()
        self.assertEqual(json.loads((self.state / 'result.json').read_text())['verdict'], 'block')

    def test_parser_without_optional_feedback_feature(self):
        parser = self.runner.with_name('factory_receipts.py')
        parser.write_text(parser.read_text() + '\nif "FEEDBACK_PREFIX" in globals(): del FEEDBACK_PREFIX\n')
        self.set_data(comments=[self.receipt('approve')])
        self.prepare()
        self.assertEqual(json.loads((self.state / 'result.json').read_text())['verdict'], 'approve')

    def test_deadline_emits_retry_metrics_not_verdict(self):
        self.prepare()
        (self.checkout / 'sleep-review').touch()
        self.cli('run', '--tier', 'A', '--model', 'oauth-pool/test', '--effort', 'low', '--minutes', '0.02', code=75)
        result = json.loads((self.state / 'result.json').read_text())
        self.assertTrue(result['retryable'])
        self.assertGreaterEqual(result['review_seconds'], 0.9)
        writes = [json.loads(line) for line in self.posts.read_text().splitlines()]
        self.assertEqual(len(writes), 1)
        self.assertIn('/issues/77/comments', writes[0][1])
        self.assertNotIn('DF_REVIEW=', writes[0][2]['body'])

    def test_moved_head_is_retryable_without_post(self):
        self.prepare()
        self.set_data(head_override=True)
        data = json.loads(self.data.read_text()); data['head'] = self.base
        self.data.write_text(json.dumps(data))
        self.cli('run', '--tier', 'A', '--model', 'oauth-pool/test', '--effort', 'low', '--minutes', '1', code=75)
        self.assertFalse(self.posts.exists())

    def test_native_metrics_preserve_receipt_admission(self):
        from factory_receipts import Factory, fingerprint
        issue = {'title': 'Acceptance', 'body': 'unchanged', 'labels': []}
        receipt = self.receipt('approve')
        receipt['body'] += '\nISSUE_FINGERPRINT=' + fingerprint(issue)
        base = receipt['body'] + '\nTIER=tier-b\nMODEL=oauth-pool/test\nBUDGET_SECONDS=300\nREVIEW_SECONDS='
        factory = Factory('owner/repo', '2', '1', request=lambda *a, **k: [])
        for value, expected in [('301', True), ('nan', False), ('-1', False), ('inf', False)]:
            receipt['body'] = base + value
            row = factory.native('review1', issue, {}, [receipt], [], {'number': 3, 'head': {'sha': self.head}})
            self.assertEqual(row['value'], 'approve')
            self.assertEqual('review_metrics' in row, expected)
            if expected:
                self.assertEqual(row['review_metrics']['review_seconds'], 301)


if __name__ == '__main__':
    unittest.main()
