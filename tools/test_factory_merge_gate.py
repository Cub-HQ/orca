import subprocess
import tempfile
from pathlib import Path
import unittest

from factory_merge_gate import validate_workflow_yaml, validate_workflows


class WorkflowYamlTests(unittest.TestCase):
    def test_duplicate_keys_refuse_at_each_mapping_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workflow.yml'
            for body in ('permissions:\n  checks: read\n  checks: read\n',
                         'jobs:\n  build:\n    env:\n      TOKEN: one\n      TOKEN: two\n'):
                path.write_text(body)
                with self.assertRaisesRegex(ValueError, 'duplicate YAML mapping key'):
                    validate_workflow_yaml(path)
            path.write_text('jobs:\n  build: &build\n    runs-on: ubuntu-latest\n  test: *build\n')
            self.assertEqual(validate_workflow_yaml(path)['jobs']['test']['runs-on'], 'ubuntu-latest')

    def test_actionlint_rejects_job_runner_context_but_accepts_step_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workflow.yml'
            prefix = 'on: push\nconcurrency:\n  group: regression\n  queue: max\njobs:\n  check:\n    runs-on: [self-hosted, hetzner, fast]\n'
            path.write_text(prefix + '    env:\n      CACHE: ${{ runner.temp }}\n    steps:\n      - run: echo ok\n')
            with self.assertRaises(subprocess.CalledProcessError):
                validate_workflows([path])
            path.write_text(prefix + '    steps:\n      - run: echo ok\n        env:\n          CACHE: ${{ runner.temp }}\n')
            validate_workflows([path])
            path.write_text(path.read_text().replace('queue: max', 'queue: typo'))
            with self.assertRaisesRegex(ValueError, 'unsupported concurrency.queue'):
                validate_workflows([path])


if __name__ == '__main__':
    unittest.main()
