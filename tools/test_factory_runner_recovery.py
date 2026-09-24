#!/usr/bin/env python3
"""Offline real-CLI proof; fake gh persists API writes, including lost responses."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("factory_runner_watch.py")
STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p = Path(os.environ['FAKE_STATE'])
s = json.loads(p.read_text())
args = sys.argv
path = args[2]
method = args[args.index('-X') + 1]
if method == 'GET':
    assert path == 'orgs/Cub-HQ/actions/runners?per_page=100', path
    with open(os.environ['FAKE_READS'], 'a') as log:
        log.write('GET\n')
    if s.get('fail_readback') and s['calls']:
        del s['fail_readback']
        p.write_text(json.dumps(s))
        sys.exit(1)
    assert '--paginate' in args and '--slurp' in args, args
    print(json.dumps([{'runners': s['runners']}]))
    sys.exit(0)
assert path.startswith('orgs/Cub-HQ/actions/runners/'), path
assert method in ('POST', 'DELETE'), method
rid = int(path.split('/')[4])
label = json.load(sys.stdin)['labels'][0] if method == 'POST' else path.split('/')[-1]
s['calls'].append([rid, method, label])
fail = s.get('fail') == [rid, method, label]
if not fail or s.get('apply_failure'):
    r = next(r for r in s['runners'] if r['id'] == rid)
    r['labels'] = [v for v in r['labels'] if v['name'] != label]
    if method == 'POST':
        r['labels'].append({'name': label, 'type': 'custom'})
    if s.get('drop_target') and label == 'fast' and method == 'POST':
        r['status'] = 'offline'
if fail:
    del s['fail']
p.write_text(json.dumps(s))
if fail:
    print('simulated lost response or rejected write', file=sys.stderr)
    sys.exit(1)
print('{}')
'''


def state():
    runners = []
    for index, name in enumerate(f'df-runner-{h}-{s}' for h in range(1, 5) for s in (1, 2)):
        runners.append({'id': 101 + index, 'name': name, 'busy': True,
                        'status': 'offline' if index == 0 else 'online',
                        'labels': [{'name': v} for v in ['self-hosted', 'hetzner', 'Linux', 'X64',
                                                       'keep-me', 'fast' if index == 0 else 'heavy']]})
    runners.append({'id': 999, 'name': 'unrelated', 'status': 'online', 'busy': False,
                    'labels': [{'name': 'fast'}, {'name': 'm4'}]})
    return {'runners': runners, 'calls': []}


class RecoveryCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stub = self.root / 'gh'
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.file = self.root / 'state.json'
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                        FAKE_STATE=str(self.file), FAKE_READS=str(self.root / 'reads'))

    def run_cli(self, data, success=True, repo='Cub-HQ/fitness-coach'):
        self.file.write_text(json.dumps(data))
        (self.root / 'reads').write_text('')
        result = subprocess.run([sys.executable, str(SCRIPT), '--repo', repo, '--recover-fast'],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return json.loads(self.file.read_text())

    def assert_split(self, data, target=102):
        self.assertEqual(len(data['runners']), 9)
        for runner in data['runners'][:8]:
            labels = {v['name'] for v in runner['labels']}
            self.assertEqual('fast' in labels, runner['id'] == target)
            self.assertEqual('heavy' in labels, runner['id'] != target)
            self.assertTrue({'self-hosted', 'hetzner', 'Linux', 'X64', 'keep-me'} <= labels)
        self.assertEqual(data['runners'][-1], state()['runners'][-1])

    def test_busy_failover_and_replay(self):
        after = self.run_cli(state())
        self.assert_split(after)
        replay = self.run_cli(after)
        self.assertEqual(replay, after)
        after['runners'][0]['status'] = 'online'
        self.assertEqual(self.run_cli(after), after)  # recovered former fast stays heavy

    def test_online_busy_fast_untouched(self):
        data = state()
        data['runners'][0]['status'] = 'online'
        self.assertEqual(self.run_cli(data), data)
        self.assertEqual((self.root / 'reads').read_text(), 'GET\n')

    def test_failures_readback_and_next_run(self):
        for operation in ([102, 'POST', 'fast'], [101, 'POST', 'heavy'],
                          [101, 'DELETE', 'fast'], [102, 'DELETE', 'heavy']):
            for applied in (False, True):
                with self.subTest(operation=operation, applied=applied):
                    data = state()
                    data.update(fail=operation, apply_failure=applied)
                    after = self.run_cli(data, success=applied)
                    self.assertTrue(any(any(v['name'] == 'fast' for v in r['labels'])
                                        for r in after['runners'][:8]))
                    self.assert_split(self.run_cli(after))

    def test_no_fast_after_reregistration(self):
        data = state()
        for runner in data['runners'][:8]:
            runner['labels'] = [v for v in runner['labels'] if v['name'] not in ('fast', 'heavy')]
            runner['status'] = 'online'
        self.assert_split(self.run_cli(data), target=101)

    def test_refusals_do_not_mutate(self):
        for kind in ('offline', 'missing', 'wrong-label', 'wrong-repo', 'duplicate-name'):
            with self.subTest(kind=kind):
                data = state()
                if kind == 'offline':
                    for r in data['runners'][:8]:
                        r['status'] = 'offline'
                elif kind == 'missing':
                    data['runners'].pop(2)
                elif kind == 'wrong-label':
                    data['runners'][2]['labels'] = []
                elif kind == 'duplicate-name':
                    data['runners'][2]['name'] = data['runners'][1]['name']
                repo = 'Cub-HQ/other' if kind == 'wrong-repo' else 'Cub-HQ/fitness-coach'
                self.assertEqual(self.run_cli(data, success=False, repo=repo), data)

    def test_readback_outage_and_overlap_reconcile(self):
        data = state()
        data['fail_readback'] = True
        after = self.run_cli(data, success=False)
        self.assertEqual(after['calls'], [[102, 'POST', 'fast']])
        after['runners'][0]['status'] = 'online'
        self.assert_split(self.run_cli(after), target=101)

    def test_promoted_runner_goes_offline(self):
        data = state()
        data['drop_target'] = True
        after = self.run_cli(data, success=False)
        self.assertEqual(after['calls'], [[102, 'POST', 'fast']])
        del after['drop_target']
        self.assert_split(self.run_cli(after), target=103)


if __name__ == '__main__':
    unittest.main()
