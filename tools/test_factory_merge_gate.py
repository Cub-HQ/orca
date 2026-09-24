import tempfile
from pathlib import Path
import unittest

from factory_merge_gate import validate_workflow_yaml


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


if __name__ == '__main__':
    unittest.main()
