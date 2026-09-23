"""Focused REST-consumer smoke; no network, credentials or project dependencies."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    helper = Path(__file__).with_name("closure_epitaph.py")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gh = root / "gh"
        gh.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p=pathlib.Path(os.environ['FAKE_STATE']); s=json.loads(p.read_text()); a=sys.argv
route=a[2]
if '--input' in a:
    s['comments'].append(json.load(sys.stdin)); p.write_text(json.dumps(s)); result={}
elif '/timeline?' in route: result=[s['events'][:1],s['events'][1:]]
elif '/comments?' in route: result=[s['comments'][:1],s['comments'][1:]]
elif '/commits/' in route: result=[[s['pr']]]
elif '/pulls/' in route: result=s['pr']
else: result=s['issue']
print(json.dumps(result))
''')
        gh.chmod(0o755)
        statefile = root / "state.json"
        env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], FAKE_STATE=str(statefile))
        def run(events, expected, state="closed", pr=None):
            data = {'issue': {'state': state, 'closed_by': {'login': 'Cubatica'}}, 'events': events,
                    'comments': [{'body': 'For Josh: not shipped'}], 'pr': pr}
            statefile.write_text(json.dumps(data))
            command = [sys.executable, str(helper), '--repo', 'Cubatica/test', '--issue', '12']
            subprocess.run(command, env=env, check=True)
            result = json.loads(statefile.read_text())
            if state == 'open':
                assert result == data
                return
            body = result['comments'][-1]['body']
            assert expected in body, body
            assert body.startswith('## Where this ended up') and body.endswith('Nothing is waiting on Josh here.')
            subprocess.run(command, env=env, check=True)
            assert json.loads(statefile.read_text()) == result, 'duplicate receipt appended'
            result['comments'].append({'body': 'For Josh: stale later bot tail'})
            statefile.write_text(json.dumps(result))
            subprocess.run(command, env=env, check=True)
            corrected = json.loads(statefile.read_text())
            assert len(corrected['comments']) == len(result['comments']) + 1
            assert corrected['comments'][-1]['body'] == body
        closed = {'event': 'closed'}
        duplicate = {'event': 'commented', 'author_association': 'OWNER', 'body': 'Duplicate of #137 - scope folded in.'}
        run([duplicate, closed], 'duplicate of #137')
        run([{'event':'marked_as_duplicate','canonical':{'html_url':'https://github.com/Cubatica/test/issues/137'}},closed], 'duplicate of https://github.com/Cubatica/test/issues/137')
        run([closed], 'closed by Josh; factory stopped')
        run([duplicate, {'event': 'reopened'}, closed], 'closed by Josh; factory stopped')
        run([duplicate, {'event': 'unmarked_as_duplicate'}, closed], 'closed by Josh; factory stopped')
        run([{'event':'commented','author_association':'NONE','body':'Duplicate of #137'},closed], 'closed by Josh; factory stopped')
        run([{'event':'commented','author_association':'OWNER','body':'Work moved to #137'},closed], 'https://github.com/Cubatica/test/issues/137')
        pr = {'number': 42, 'merged_at':'2026-09-23', 'merge_commit_sha':'abc', 'html_url':'https://github.com/Cubatica/test/pull/42'}
        cross = {'event':'cross-referenced','source':{'issue':{'url':'https://api.github.com/repos/Cubatica/test/issues/42','pull_request':{'url':'unused'}}}}
        run([cross,closed], 'closed by Josh; factory stopped', pr=pr)
        run([dict(cross,will_close_target=True),closed], 'shipped via PR #42', pr=pr)
        run([dict(closed,commit_id='abc',commit_url='https://api.github.com/repos/Cubatica/test/commits/abc')], 'shipped via PR #42', pr=pr)
        run([dict(cross,will_close_target=True),closed], 'closed by Josh; factory stopped', pr=dict(pr,merged_at=None))
        run([duplicate,closed], '', state='open')
    print('PASS: 12 cases; duplicate/shipped/generic/moved/open, unrelated and unmerged PR rejection, reopen/unmark, identical-tail idempotence, stale-tail correction, paginated evidence')


if __name__ == '__main__':
    main()
