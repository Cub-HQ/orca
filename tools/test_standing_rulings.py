"""Execute the actual intake producer with isolated authority and history inputs."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/df-pipeline.yml'
STEP = next(s for s in yaml.safe_load(WORKFLOW.read_text())['jobs']['intake']['steps']
            if s.get('name') == 'Fetch standing rulings (once per run)')
SOURCE = STEP['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]


class StandingRulingsTests(unittest.TestCase):
    def produce(self, authority, decisions='', failure=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            config = root / '.config/hindsight-client/config.json'
            config.parent.mkdir(parents=True)
            key = root / 'key'
            key.write_text('offline')
            config.write_text(json.dumps({'api_url': 'https://offline.invalid', 'compartments':
                {'mac-brain': {'bank': 'test', 'key_file': str(key)}}}))

            def fetch(args, **kwargs):
                endpoint = args[-1]
                if '/contents/' in endpoint:
                    if failure:
                        raise failure
                    self.assertTrue(endpoint.startswith('repos/Cub-HQ/omp-config-backup/contents/'))
                    self.assertTrue(endpoint.endswith('?ref=main'))
                    return authority if '/agent/AGENTS.md?' in endpoint else decisions
                return json.dumps({'title': 'Issue', 'body': 'Complete mandatory issue body'})

            with patch.dict(os.environ, HOME=directory, GITHUB_OUTPUT=str(output), R='Cub-HQ/orca', ISSUE='7'), \
                 patch('subprocess.check_output', side_effect=fetch), \
                 patch('urllib.request.urlopen', return_value=io.StringIO(json.dumps(
                     {'results': [{'text': '記憶🛡️' * 30000}]}))):
                exec(compile(SOURCE, str(WORKFLOW), 'exec'), {})
            return output.read_text().split('\n', 1)[1].rsplit('\nrulings_', 1)[0][:-1]

    def test_unbounded_authority_and_utf8_history_budget(self):
        authorities = ["## Josh’s laws — mandatory\n1. " + '守則🛡️ ' * 20000 + '\n\n',
                       "## Josh's laws\n1. Short law.\n"]
        if os.environ.get('CANONICAL_AGENTS'):
            import re
            canonical = Path(os.environ['CANONICAL_AGENTS']).read_text()
            authorities.append(re.search(r"^## Josh['’]s laws[^\n]*\n.*?(?=^## |\Z)", canonical, re.M | re.S)[0])
        for laws in authorities:
            with self.subTest(bytes=len(laws.encode())):
                text = self.produce(laws + '## Other\nNot authority.\n',
                    '## Decisions\n- 2026-09-24 Newest complete decision.\n- 2020-01-01 ' + '古' * 30000)
                header = '# Standing rulings (obey absolutely)\n\n' + laws + '\n\n'
                self.assertTrue(text.startswith(header))
                self.assertIn('Newest complete decision.', text)
                self.assertNotIn('2020-01-01', text)
                self.assertIn('Relevant past lessons', text)
                self.assertLessEqual(len(text.encode()) - len(header.encode()), 65536)
                self.assertNotIn('\ufffd', text)

    def test_invalid_or_unavailable_authority_fails_closed(self):
        for authority in ('## No laws\n', "## Josh's laws\n", "## Josh's laws\nNot numbered.\n",
                          "## Josh's laws\n1. One\n## Josh's laws\n1. Duplicate\n"):
            with self.subTest(authority=authority), self.assertRaisesRegex(ValueError, 'missing, empty, or malformed'):
                self.produce(authority)
        for failure in (OSError('offline'), subprocess.CalledProcessError(1, 'gh')):
            with self.assertRaisesRegex(RuntimeError, 'Standing rulings unavailable'):
                self.produce('', failure=failure)


if __name__ == '__main__':
    unittest.main()
