#!/usr/bin/env python3
"""Neutral candidate-checkout gate. Run with the selected project's interpreter.

No checkout, publish, or exception-to-success conversion occurs here.
--discover is the single unittest discovery runner; --self-test is isolated proof.
"""
import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from urllib.request import urlopen


ACTIONLINT_VERSION = '1.7.12'
# Official v1.7.12 checksums.txt, also matched against GitHub release asset digests.
ACTIONLINT_SHA256 = {
    'darwin_arm64': 'aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f',
    'darwin_amd64': '5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644',
    'linux_amd64': '8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8',
    'linux_arm64': '325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6',
}


@contextmanager
def pinned_actionlint():
    installed = shutil.which('actionlint')
    if installed:
        version = subprocess.run([installed, '-version'], capture_output=True, text=True)
        if version.returncode == 0 and version.stdout.splitlines()[:1] == [ACTIONLINT_VERSION]:
            yield installed
            return
    machine = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine(), platform.machine())
    target = platform.system().lower() + '_' + machine
    digest = ACTIONLINT_SHA256[target]
    name = f'actionlint_{ACTIONLINT_VERSION}_{target}.tar.gz'
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'factory-merge-gate'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / name
    if archive.exists():
        data = archive.read_bytes()
    else:
        url = f'https://github.com/rhysd/actionlint/releases/download/v{ACTIONLINT_VERSION}/{name}'
        with urlopen(url, timeout=60) as response:
            data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('actionlint archive checksum mismatch: ' + str(archive))
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as download:
            temporary = Path(download.name)
            try:
                download.write(data)
                download.close()
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
    # Re-extract only the verified regular binary; never execute an unchecked cache file.
    with tempfile.TemporaryDirectory(prefix='factory-actionlint-') as tmp:
        binary = Path(tmp) / 'actionlint'
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            member = tar.getmember('actionlint')
            if not member.isfile():
                raise ValueError('actionlint archive binary must be a regular file')
            with tar.extractfile(member) as source:
                binary.write_bytes(source.read())
        binary.chmod(0o700)
        print(f'ACTIONLINT_VERSION={ACTIONLINT_VERSION} SHA256={digest} ARCHIVE={archive}', flush=True)
        yield str(binary)


def validate_workflows(paths):
    paths = list(paths)
    if not paths:
        return
    for path in paths:
        workflow = validate_workflow_yaml(path)
        # v1.7.12 predates GitHub's queue key. Validate its literal contract here;
        # never suppress expression-context diagnostics or accept unchecked expressions.
        for owner in [workflow, *workflow.get('jobs', {}).values()]:
            concurrency = owner.get('concurrency')
            if isinstance(concurrency, dict) and 'queue' in concurrency:
                if concurrency['queue'] not in ('single', 'max'):
                    raise ValueError(f'unsupported concurrency.queue in {path}: expected single or max')
    with pinned_actionlint() as binary, tempfile.TemporaryDirectory(prefix='factory-actionlint-config-') as tmp:
        config = Path(tmp) / 'actionlint.yaml'
        config.write_text('self-hosted-runner:\n  labels: [m4, hetzner, fast, heavy, blacksmith-6vcpu-macos-15]\n')
        command([binary, '-shellcheck=', '-pyflakes=', '-config-file', str(config),
                 '-ignore', '^unexpected key "queue" for "concurrency" section\\. expected one of "cancel-in-progress", "group"$',
                 '-ignore', '^anchor "[^\"]+" is defined but not used$',
                 *map(str, paths)])


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


def validate_workflow_yaml(path):
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            keys = [key.value for key, _ in node.value]
            if len(keys) != len(set(keys)):
                raise ValueError(f'duplicate YAML mapping key in {path}')
            return super().construct_mapping(node, deep=deep)

    return yaml.load(Path(path).read_text(), Loader=UniqueLoader)


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
    repo = args.repo.removeprefix('Cub-HQ/')
    validate_workflows(sorted(path for path in Path(".github/workflows").iterdir()
                              if path.suffix in (".yml", ".yaml") and path.is_file()))
    if repo == 'fitness-coach':
        os.chdir('runtime')
        sys.path.insert(0, str(Path.cwd()))
        if not discover('tests', rows):
            raise SystemExit(1)
    elif repo in ('orca', 'df-fixture'):
        if rows:
            raise ValueError('package test exclusions require runner-native exact-ID support; none currently proven')
        manager = 'npm' if repo == 'df-fixture' else 'pnpm'
        command([manager, 'run', 'typecheck'])
        command([manager, 'test'])
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
