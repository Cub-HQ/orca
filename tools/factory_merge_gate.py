#!/usr/bin/env python3
"""Neutral candidate-checkout gate. Run with the selected project's interpreter.

No checkout, install, publish, or exception-to-success conversion occurs here.
--discover is the single unittest discovery runner; --self-test is isolated proof.
"""
import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


def command(argv):
    print('GATE_COMMAND=' + json.dumps(argv), flush=True)
    subprocess.run(argv, check=True)


def skips(path):
    rows = json.loads(Path(path).read_text())
    if not isinstance(rows, list):
        raise ValueError('skip list must be an array')
    seen = set()
    for row in rows:
        if set(row) != {'kind', 'name', 'reason'} or row['kind'] not in ('module', 'test'):
            raise ValueError('each skip needs kind=module|test, exact name and reason')
        if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', row['name']) or not row['reason'].strip():
            raise ValueError('skip names must be exact identifiers, with a reason')
        key = (row['kind'], row['name'])
        if key in seen:
            raise ValueError('duplicate skip: ' + row['name'])
        seen.add(key)
    return rows


def discover(start, rows, stream=None, pattern='test*.py'):
    # Omit enumerated modules BEFORE importing: missing optional imports cannot
    # become _FailedTest placeholders and accidentally hide a different module.
    modules = {r['name']: r['reason'] for r in rows if r['kind'] == 'module'}
    tests = {r['name']: r['reason'] for r in rows if r['kind'] == 'test'}

    class Loader(unittest.TestLoader):
        def _get_module_from_name(self, name):
            if name in modules:
                print('SKIP ' + name + ': ' + modules[name], flush=True)
                raise unittest.SkipTest(modules[name])
            return super()._get_module_from_name(name)

    def filter_suite(suite):
        result = unittest.TestSuite()
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                result.addTest(filter_suite(test))
            elif test.id() in tests:
                name, reason = test.id(), tests[test.id()]
                print('SKIP ' + name + ': ' + reason, flush=True)
                def skip(reason=reason):
                    raise unittest.SkipTest(reason)
                result.addTest(unittest.FunctionTestCase(skip, description=name))
            else:
                result.addTest(test)
        return result

    suite = filter_suite(Loader().discover(start, pattern=pattern))
    if suite.countTestCases() == 0:
        raise ValueError('discovery found no tests')
    result = unittest.TextTestRunner(stream=stream or sys.stderr, verbosity=2).run(suite)
    return result.wasSuccessful()


def omp_gate(base, smoke_path, yaml_command):
    changed = subprocess.check_output(['git', 'diff', '--name-only', '--diff-filter=ACMR', '-z', base, 'HEAD']).decode().split('\0')
    smoke = json.loads(Path(smoke_path).read_text()) if smoke_path else {}
    for name in filter(None, changed):
        path = Path(name)
        if not path.is_file():
            continue
        data = path.read_bytes()
        first = data.split(b'\n', 1)[0]
        is_python = path.suffix == '.py' or (first.startswith(b'#!') and b'python' in first)
        is_shell = path.suffix in ('.sh', '.bash', '.zsh') or (first.startswith(b'#!') and any(x in first for x in (b'bash', b'zsh', b'/sh')))
        if is_python:
            compile(data, name, 'exec')
            print('SYNTAX_PASS=' + name)
        elif is_shell:
            command(['zsh' if path.suffix == '.zsh' or b'zsh' in first else 'bash', '-n', name])
        elif path.suffix in ('.js', '.mjs', '.cjs'):
            command(['node', '--check', name])
        if path.suffix in ('.yaml', '.yml'):
            if not yaml_command:
                raise ValueError('changed YAML requires --yaml-command JSON argv (file appended)')
            command(yaml_command + [name])
        if name.startswith('tools/') and (is_python or is_shell or os.access(path, os.X_OK) or path.suffix in ('.js', '.mjs', '.cjs', '.ts')):
            commands = smoke.get(name)
            if not commands:
                raise ValueError('real tool smoke missing for ' + name)
            for argv in commands:
                if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                    raise ValueError('smoke entry must be a nonempty argv array')
                command(argv)


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'test_env.py').write_text("raise RuntimeError('must never import skipped module')\n")
        (root / 'test_behavior.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_env(self): self.fail("known")\n def test_other(self): self.assertTrue(False)\n')
        rows = [{'kind': 'module', 'name': 'test_env', 'reason': 'enumerated missing host fixture'}, {'kind': 'test', 'name': 'test_behavior.T.test_env', 'reason': 'enumerated environment case'}]
        assert not discover(tmp, rows, io.StringIO()), 'unlisted failure was swallowed'
        sys.modules.pop('test_behavior', None)
        (root / 'test_behavior.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_env(self): self.fail("known")\n def test_other(self): self.assertTrue(True)\n')
        # Disable bytecode reuse after changing this synthetic module.
        import shutil
        shutil.rmtree(root / '__pycache__', ignore_errors=True)
        assert discover(tmp, rows, io.StringIO())
    print('PASS: exact module/test skips report reasons; unrelated failure fails; repaired control passes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=os.environ.get('R', ''))
    parser.add_argument('--base-sha')
    parser.add_argument('--candidate-sha')
    parser.add_argument('--skip-list')
    parser.add_argument('--smoke-json', default='tools/merge-smokes.json')
    parser.add_argument('--yaml-command', type=json.loads)
    parser.add_argument('--discover', metavar='TEST_DIRECTORY')
    parser.add_argument('--pattern', default='test*.py', help='Focused runner: exact test_MODULE.py; neutral full gate ignores this')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.skip_list:
        parser.error('--skip-list is required (an empty explicit list is valid)')
    rows = skips(args.skip_list)
    if args.discover:
        sys.path.insert(0, str(Path.cwd()))
        if not discover(args.discover, rows, pattern=args.pattern):
            raise SystemExit(1)
        return
    if not args.candidate_sha or not args.base_sha:
        parser.error('--candidate-sha and --base-sha are required')
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    if actual != args.candidate_sha or not re.fullmatch(r'[0-9a-f]{40}', actual):
        raise ValueError('checkout HEAD must equal the full candidate SHA')
    subprocess.run(['git', 'merge-base', '--is-ancestor', args.base_sha, actual], check=True)
    repo = args.repo.removeprefix('Cubatica/')
    if repo == 'fitness-coach':
        os.chdir('runtime')
        sys.path.insert(0, str(Path.cwd()))
        if not discover('tests', rows):
            raise SystemExit(1)
    elif repo in ('orca', 'df-fixture'):
        if rows:
            raise ValueError('pnpm exclusions require runner-native exact-ID support; none currently proven')
        command(['pnpm', 'run', 'typecheck'])
        command(['pnpm', 'test'])
    elif repo == 'omp-config-backup':
        if rows:
            raise ValueError('no known environment exclusions for script gates')
        omp_gate(args.base_sha, args.smoke_json, args.yaml_command)
    else:
        raise ValueError('unknown repository: ' + args.repo)
    print('MERGE_GATE=pass\nCANDIDATE_SHA=' + actual)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('result=pass\ncandidate_sha=' + actual + '\n')


if __name__ == '__main__':
    main()
