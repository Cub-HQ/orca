#!/usr/bin/env python3
"""Serialized df-pipeline lease and receipts. Requires per-issue Actions concurrency.

R, ISSUE, GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT identify the caller.
Commands: admission; guard [--stage ship] [--pr N]; resume STAGE;
record STAGE [--value done]; revoke; selfcheck. All support --pr N.
Outputs are printed and appended to GITHUB_OUTPUT. API errors fail closed.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

PREFIX = 'DF_PIPELINE_V1='
STAGES = ('intake', 'build', 'review1', 'rework', 'review2', 'qa', 'rebase', 'ship', 'deploy', 'acceptance')
TOKENS = {'intake': 'INTAKE', 'build': 'DF_PR', 'review1': 'DF_REVIEW', 'review2': 'DF_REVIEW', 'rework': 'DF_REWORK', 'qa': 'DF_QA'}
VALUES = {'intake': ('go', 'needs-info', 'orch-direct'), 'review1': ('approve', 'block'), 'review2': ('approve', 'block'), 'qa': ('pass', 'fail'), 'rework': ('done',)}
PRODUCER_FIELDS = ('Producer', 'Producer fix', 'Regeneration proof')


def bug_issue(issue):
    return ((issue.get('type') or {}).get('name', '').casefold() == 'bug'
            or any(label['name'].casefold() in ('bug', 'type:bug') for label in issue.get('labels', [])))


def producer_proof(body):
    proof = {}
    for field in PRODUCER_FIELDS:
        values = re.findall(r'^' + re.escape(field) + r':[ \t]*([^\r\n]*)\r?$', body, re.M)
        value = values[0].strip() if len(values) == 1 else ''
        if (not any(c.isalnum() for c in value)
                or re.match(r'^(?:todo|tbd|pending|unknown|none|n/?a|not applicable)(?:\b|$)', value, re.I)
                or re.fullmatch(r'<[^>]*>|\[[^\]]*\]', value)):
            return {}
        proof[field] = value
    return proof


def api(path, method='GET', data=None, pages=False):
    cmd = ['gh', 'api', path, '-X', method]
    if pages:
        cmd += ['--paginate', '--slurp']
    if data is not None:
        cmd += ['--input', '-']
    result = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                            text=True, capture_output=True, check=True)
    value = json.loads(result.stdout) if result.stdout.strip() else None
    return [item for page in value for item in page] if pages else value


def fingerprint(issue):
    labels = sorted(x['name'] for x in issue.get('labels', [])
                    if not x['name'].startswith(('factory:', 'actions:')))
    return hashlib.sha256(json.dumps([issue['title'], issue.get('body') or '', labels],
                                    ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def trusted(comment):
    return comment.get('author_association') in ('OWNER', 'MEMBER', 'COLLABORATOR') or comment.get('user', {}).get('login') == 'github-actions[bot]'


def token(body, name):
    found = re.findall(r'^' + re.escape(name) + r'=([^\r\n]+)\r?$', body, re.M)
    return found[0].strip() if len(found) == 1 else ''


def records(comments):
    result = []
    for c in comments:
        if trusted(c) and c.get('body', '').startswith(PREFIX):
            try:
                row = json.loads(c['body'][len(PREFIX):])
                if isinstance(row, dict):
                    result.append(row)
            except ValueError:
                pass
    return result


class Factory:
    def __init__(self, repo, issue, run, attempt='1', request=api):
        self.root = 'repos/' + repo
        self.issue = str(issue)
        self.run = str(run)
        self.attempt = str(attempt)
        self.owner = self.run + ':' + self.attempt
        self.api = request

    def comments(self, number):
        return self.api(self.root + '/issues/' + str(number) + '/comments?per_page=100', pages=True)

    def state(self):
        issue = self.api(self.root + '/issues/' + self.issue)
        if issue['state'] != 'open':
            raise RuntimeError('issue is closed')
        run = self.api(self.root + '/actions/runs/' + self.run)
        if run['status'] != 'in_progress' or run.get('conclusion') is not None or str(run.get('run_attempt', 1)) != self.attempt:
            raise RuntimeError('run cancelled, completed, or superseded')
        comments = self.comments(self.issue)
        return issue, run, comments, records(comments)

    def post(self, row):
        return self.api(self.root + '/issues/' + self.issue + '/comments', 'POST', {'body': PREFIX + json.dumps(row, sort_keys=True, separators=(',', ':'))})

    def admission(self, source_run=None):
        issue, run, comments, rows = self.state()
        leases = [r for r in rows if r.get('kind') == 'lease']
        if source_run is not None:
            source = self.api(self.root + '/actions/runs/' + str(source_run))
            if source.get('conclusion') != 'success' or source.get('status') != 'completed':
                raise RuntimeError('acceptance source run did not complete successfully')
            if not leases or str(leases[-1].get('run')) != str(source_run) or leases[-1].get('revoked') or leases[-1].get('fingerprint') != fingerprint(issue):
                raise RuntimeError('acceptance source no longer owns the current cycle')
        if leases:
            last = leases[-1]
            if last.get('owner') == self.owner:
                if last.get('revoked') or last.get('fingerprint') != fingerprint(issue):
                    raise RuntimeError('this run lease is revoked or its issue changed')
                return {'go': 'true', 'lease': self.owner}
            old = self.api(self.root + '/actions/runs/' + str(last['run']))
            if old['status'] != 'completed' and not last.get('revoked'):
                raise RuntimeError('another run owns the cycle lease')
            if int(last['run']) > int(self.run):
                raise RuntimeError('newer run has superseded this run')
        self.post({'kind': 'lease', 'owner': self.owner, 'run': self.run,
                   'fingerprint': fingerprint(issue), 'revoked': False})
        self.guard()
        return {'go': 'true', 'lease': self.owner}

    def guard(self):
        issue, run, comments, rows = self.state()
        leases = [r for r in rows if r.get('kind') == 'lease']
        if not leases or leases[-1].get('owner') != self.owner or leases[-1].get('revoked'):
            raise RuntimeError('missing, revoked, or lost cycle lease')
        if leases[-1].get('fingerprint') != fingerprint(issue):
            raise RuntimeError('issue content changed since admission')
        return issue, run, comments, rows

    def revoke(self):
        # Cancellation cleanup may execute after GitHub marks the run completed.
        rows = records(self.comments(self.issue))
        leases = [r for r in rows if r.get('kind') == 'lease']
        if leases and leases[-1].get('owner') == self.owner:
            self.post(dict(leases[-1], revoked=True))
        return {'revoked': 'true'}

    def context(self, pr=None):
        issue, run, comments, rows = self.guard()
        if not pr:
            candidates = [r.get('pr') for r in rows if r.get('kind') == 'stage' and r.get('fingerprint') == fingerprint(issue) and r.get('pr')]
            if candidates:
                pr = candidates[-1]
            else:
                for c in reversed(comments):
                    n = token(c.get('body', ''), 'DF_PR')
                    if trusted(c) and n.isdigit():
                        pr = n
                        break
        pull = self.api(self.root + '/pulls/' + str(pr)) if pr else None
        if pull and pull['state'] != 'open' and not pull.get('merged'):
            raise RuntimeError('PR is closed without merge')
        return issue, run, comments, rows, pull

    def matching(self, stage, issue, rows, pull):
        for row in reversed(rows):
            if row.get('kind') != 'stage' or row.get('stage') != stage or row.get('fingerprint') != fingerprint(issue):
                continue
            if stage in ('build', 'rework') and bug_issue(issue):
                proof = row.get('producer_proof') or {}
                if (not isinstance(proof, dict)
                        or not producer_proof('\n'.join(f'{k}: {v}' for k, v in proof.items()))
                        or not pull or row.get('head_sha') != pull['head']['sha']):
                    continue
            if stage != 'intake':
                if not pull or str(row.get('pr')) != str(pull['number']):
                    continue
                if stage != 'build' and row.get('head_sha') != pull['head']['sha']:
                    continue
                if stage == 'rebase' and row.get('base_sha') != pull['base']['sha']:
                    continue
                if stage in ('ship', 'deploy', 'acceptance') and (not pull.get('merged') or row.get('merge_sha') != pull.get('merge_commit_sha')):
                    continue
            return row
        return None

    def native(self, stage, issue, run, comments, rows, pull, fresh=False):
        if stage not in TOKENS:
            return None
        source = list(comments)
        if pull and stage != 'intake':
            source += self.comments(pull['number'])
        source.sort(key=lambda c: c['id'])
        # Legacy receipts without fingerprint are usable only in an already-bound
        # context, or while recording evidence produced during this run.
        bound = self.matching('intake', issue, rows, pull)
        for c in reversed(source):
            body = c.get('body', '')
            value = token(body, TOKENS[stage])
            if not trusted(c) or not value:
                continue
            if stage in VALUES and value not in VALUES[stage]:
                continue
            if fresh and c.get('created_at', '') < run['run_started_at']:
                continue
            explicit = token(body, 'ISSUE_FINGERPRINT') == fingerprint(issue)
            if not explicit and not fresh and (not bound or c.get('created_at', '') < bound.get('recorded_at', 'z')):
                continue
            if stage == 'intake' and not fresh and not explicit:
                continue
            if stage != 'intake':
                if not pull or token(body, 'HEAD_SHA') != pull['head']['sha']:
                    continue
                if stage == 'build' and value != str(pull['number']):
                    continue
            proof = {}
            if stage in ('build', 'rework') and bug_issue(issue):
                proof = producer_proof(body)
                if not proof:
                    continue
            if stage == 'intake' and value == 'go' and token(body, 'FAST_LANE') == 'orch-direct':
                value = 'orch-direct'
            return {'value': value, 'evidence': c['id'], **({'producer_proof': proof} if proof else {})}
        return None

    def result(self, stage, row, pull):
        result = {'skip': 'true' if row else 'false'}
        if pull:
            result.update(pr=str(pull['number']), head_sha=pull['head']['sha'], merge_sha=pull.get('merge_commit_sha') or '')
            if stage == 'ship':
                result['runtime_changed'] = (row or {}).get('runtime_changed', 'yes')
        if row:
            key = 'go' if stage == 'intake' else 'verdict' if stage in ('review1', 'review2') else 'qa' if stage == 'qa' else 'value'
            result[key] = str(row['value'])
            result['reason'] = 'already done'
        return result

    def resume(self, stage, pr=None):
        issue, run, comments, rows, pull = self.context(pr)
        row = self.native(stage, issue, run, comments, rows, pull) or self.matching(stage, issue, rows, pull)
        if stage == 'build' and pull and not pull.get('merged'):
            qa = self.native('qa', issue, run, comments, rows, pull) or self.matching('qa', issue, rows, pull)
            if qa and qa['value'] == 'fail':
                row = None  # The existing builder owns QA repair on this same PR.
        if stage in ('ship', 'rebase') and pull and pull.get('merged'):
            row = row or {'value': 'done'}
        return self.result(stage, row, pull)

    def release_guard(self, stage, pr=None):
        issue, run, comments, rows, pull = self.context(pr)
        if stage in ('ship', 'deploy', 'acceptance'):
            if not pull:
                raise RuntimeError('release requires a PR')
            if bug_issue(issue) and not any(
                    self.native(s, issue, run, comments, rows, pull) or self.matching(s, issue, rows, pull)
                    for s in ('build', 'rework')):
                raise RuntimeError('current PR SHA lacks Producer:, Producer fix:, Regeneration proof: evidence')
            reviews = [self.matching(s, issue, rows, pull) for s in ('review1', 'review2')]
            reviews = [r for r in reviews if r]
            native = self.native('review1', issue, run, comments, rows, pull)
            review = native or (reviews[-1] if reviews else None)
            qa = self.native('qa', issue, run, comments, rows, pull) or self.matching('qa', issue, rows, pull)
            if not review or review['value'] != 'approve' or not qa or qa['value'] != 'pass':
                raise RuntimeError('current PR SHA lacks review approval and QA pass')
            if stage in ('deploy', 'acceptance') and not pull.get('merged'):
                raise RuntimeError('deployment requires merged PR')
        return self.result(stage or 'build', None, pull)

    def record(self, stage, pr=None, value=None, runtime_changed=None):
        issue, run, comments, rows, pull = self.context(pr)
        if stage in TOKENS:
            evidence = self.native(stage, issue, run, comments, rows, pull, fresh=True)
            if not evidence:
                raise RuntimeError('missing fresh, trusted, current-SHA ' + stage + ' evidence')
            if value is not None and value != evidence['value']:
                raise RuntimeError('provided value differs from evidence')
            value = evidence['value']
        else:
            evidence = {}
            if value != 'done':
                raise RuntimeError('mechanical stages require --value done after successful work')
            if stage in ('ship', 'deploy', 'acceptance'):
                self.release_guard(stage, pr)
                if not pull or not pull.get('merged'):
                    raise RuntimeError('cannot record release before merge')
        if stage != 'intake' and not pull:
            raise RuntimeError('stage requires PR')
        row = {'kind': 'stage', 'stage': stage, 'owner': self.owner, 'fingerprint': fingerprint(issue),
               'value': value, 'recorded_at': run['run_started_at'], **evidence}
        if runtime_changed is not None:
            if stage != 'ship' or runtime_changed not in ('yes', 'no'):
                raise RuntimeError('runtime_changed is a yes/no ship output')
            row['runtime_changed'] = runtime_changed
        if pull:
            row.update(pr=pull['number'], head_sha=pull['head']['sha'], base_sha=pull['base']['sha'], merge_sha=pull.get('merge_commit_sha') or '')
        self.guard()
        self.post(row)
        return self.result(stage, row, pull)


def emit(result):
    text = ''.join(k + '=' + str(v) + '\n' for k, v in result.items())
    print(text, end='')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            f.write(text)


def selfcheck():
    issue = {'state': 'open', 'title': 'fix', 'body': 'contract', 'labels': []}
    runs = {'1': {'status': 'in_progress', 'conclusion': None, 'run_started_at': '2026-09-23T00:00:00Z'},
            '2': {'status': 'in_progress', 'conclusion': None, 'run_started_at': '2026-09-23T01:00:00Z'}}
    pull = {'number': 7, 'state': 'open', 'merged': False, 'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}
    comments = {'9': [], '7': []}
    def comment(n, body):
        c = {'id': sum(map(len, comments.values())) + 1, 'body': body, 'author_association': 'OWNER', 'created_at': '2026-09-23T00:10:00Z'}
        comments[n].append(c)
        return c
    def fake(path, method='GET', data=None, pages=False):
        tail = path.split('repos/o/r/')[1]
        if tail == 'issues/9': return issue
        if tail.startswith('actions/runs/'): return runs[tail.split('/')[-1]]
        if tail == 'pulls/7': return pull
        if tail.startswith('pulls/7/files'): return [{'filename': 'runtime/coach.py'}]
        if '/comments' in tail:
            n = tail.split('/')[1]
            return comment(n, data['body']) if method == 'POST' else comments[n][:]
        raise AssertionError(path)
    def refuses(fn):
        try: fn()
        except RuntimeError: return
        raise AssertionError('expected refusal')
    f = Factory('o/r', 9, 1, request=fake)
    f.admission()
    comment('9', '## DF_Intake\nINTAKE=go')
    f.record('intake')
    comment('9', 'DF_PR=7\nHEAD_SHA=' + 'a' * 40)
    f.record('build')
    comment('7', '## DF_Reviewer\nHEAD_SHA=' + 'a' * 40 + '\nDF_REVIEW=block')
    f.record('review1')
    runs['1'].update(status='completed', conclusion='cancelled')
    refuses(f.guard)
    g = Factory('o/r', 9, 2, request=fake)
    g.admission()
    assert [g.resume(s).get('skip') for s in ('intake', 'build', 'review1', 'rework')] == ['true', 'true', 'true', 'false']
    assert g.resume('review1')['verdict'] == 'block'
    refuses(lambda: g.release_guard('ship'))
    pull['head']['sha'] = 'c' * 40
    assert g.resume('review1')['skip'] == g.resume('qa')['skip'] == 'false'
    assert g.resume('build')['skip'] == 'true'
    comment('7', '## DF_Reviewer\nHEAD_SHA=' + 'c' * 40 + '\nDF_REVIEW=approve')['created_at'] = '2026-09-23T01:10:00Z'
    g.record('review1')
    comment('9', '## DF_QA\nHEAD_SHA=' + 'c' * 40 + '\nDF_QA=fail')['created_at'] = '2026-09-23T01:10:00Z'
    g.record('qa')
    assert g.resume('build')['skip'] == 'false'
    comment('9', '## DF_QA\nHEAD_SHA=' + 'c' * 40 + '\nDF_QA=pass')['created_at'] = '2026-09-23T01:11:00Z'
    g.record('qa')
    g.release_guard('ship')
    g.record('rebase', value='done')
    assert g.resume('rebase')['skip'] == 'true'
    pull.update(merged=True, state='closed', merge_commit_sha='d' * 40)
    pull['base']['sha'] = 'f' * 40
    assert g.resume('rebase')['skip'] == 'true'
    assert g.resume('ship')['runtime_changed'] == 'yes'
    g.record('ship', value='done', runtime_changed='no')
    g.record('deploy', value='done')
    assert g.resume('ship')['runtime_changed'] == 'no'
    assert g.resume('deploy')['skip'] == 'true'
    pull['head']['sha'] = 'e' * 40
    assert g.resume('deploy')['skip'] == 'false'
    refuses(lambda: g.release_guard('deploy'))
    pull['head']['sha'] = 'c' * 40
    issue['body'] = 'changed contract'
    refuses(g.guard)
    issue['body'] = 'contract'
    issue['labels'] = [{'name': 'factory:building'}, {'name': 'actions:go'}]
    g.guard()
    issue['state'] = 'closed'
    refuses(g.guard)
    issue['state'] = 'open'
    refuses(f.guard)
    g.revoke()
    refuses(g.guard)
    refuses(g.admission)
    runs['3'] = dict(runs['2'])
    h = Factory('o/r', 9, 3, request=fake)
    refuses(lambda: h.admission(source_run=2))
    runs['2'].update(status='completed', conclusion='success')
    refuses(lambda: h.admission(source_run=2))  # Explicit revocation survives success.
    h.admission()
    runs['3'].update(status='completed', conclusion='success')
    runs['4'] = dict(runs['1'], status='in_progress', conclusion=None)
    k = Factory('o/r', 9, 4, request=fake)
    k.admission(source_run=3)
    k.release_guard('acceptance')
    print('PASS: cut-after-block resume, changed SHA invalidation, release denial, workflow-label stability, closed/cancelled/lost/revoked leases')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=('admission', 'guard', 'resume', 'record', 'revoke', 'selfcheck'))
    p.add_argument('stage', nargs='?', choices=STAGES)
    p.add_argument('--stage', dest='guard_stage', choices=STAGES)
    p.add_argument('--pr', type=int, default=int(os.environ['PR']) if os.environ.get('PR', '').isdigit() else None)
    p.add_argument('--value')
    p.add_argument('--source-run', type=int)
    p.add_argument('--runtime-changed', choices=('yes', 'no'))
    a = p.parse_args()
    if a.operation == 'selfcheck':
        selfcheck()
        return
    f = Factory(os.environ['R'], int(os.environ['ISSUE']), int(os.environ['GITHUB_RUN_ID']), os.environ.get('GITHUB_RUN_ATTEMPT', '1'))
    if a.operation == 'admission':
        result = f.admission(a.source_run)
    elif a.operation == 'revoke':
        result = f.revoke()
    elif a.operation == 'guard':
        result = f.release_guard(a.guard_stage or a.stage, a.pr)
    else:
        if not a.stage: p.error('stage is required')
        result = f.resume(a.stage, a.pr) if a.operation == 'resume' else f.record(a.stage, a.pr, a.value, a.runtime_changed)
    emit(result)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, KeyError, ValueError, subprocess.SubprocessError) as error:
        print('Factory fence refused: ' + str(error), file=sys.stderr)
        sys.exit(1)
