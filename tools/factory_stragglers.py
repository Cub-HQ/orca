#!/usr/bin/env python3
"""Workflow upgrade/recycler sweep; all callers MUST share a concurrency group.

Triggers: push.paths [.github/workflows/df-pipeline.yml], workflow_run
(workflows: [df-pipeline], types: [completed]), schedule (every five minutes).
Run: python3 tools/factory_stragglers.py sweep --repo "$R" --current-ref main
Permissions: actions:write, contents:read, issues:write. GH_TOKEN is required.
Use a repository-wide `df-straggler-sweep` concurrency group with
cancel-in-progress:false, distinct from the pipeline issue concurrency group.
Only authenticated workflow code may write the marker prefix. Do not run from
untrusted PR checkouts. Dispatch transport ambiguity is retained, never retried;
the receipt explicitly requests reconciliation rather than risking duplicates.
Active Review/Re-review runs defer to GitHub's step/job timeouts, including setup;
the generic inactivity recycler must not shorten their configured deadlines.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from urllib.parse import quote

PREFIX = '<!-- df-straggler:v1 '
ACTIVE = {'queued', 'requested', 'waiting', 'pending', 'in_progress'}
WORK = {'Intake', 'Build', 'Review', 'Rework', 'Re-review', 'Source QA',
        'Source QA (not live acceptance)', 'Source QA (post-merge)'}
HOLD = {'actions:parked', 'actions:needs-info', 'factory:needs-info',
        'factory:needs-you', 'factory:needs-plan', 'factory:awaiting-review',
        'factory:awaiting-user-review', 'factory:awaiting-merge', 'factory:orch-direct'}


def api(path, method='GET', data=None, pages=False):
    command = ['gh', 'api', path, '--method', method]
    if pages:
        command += ['--paginate', '--slurp']
    if data is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else None


def scope(issue):
    # Runtime labels and comments change during normal work; they aren't scope.
    value = [issue.get('title'), issue.get('body'), sorted(
        label['name'] for label in issue['labels']
        if not label['name'].startswith(('factory:', 'actions:')))]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def eligible(issue):
    labels = {label['name'] for label in issue['labels']}
    return issue['state'] == 'open' and not labels.intersection(HOLD)


def issue_number(run):
    match = re.match(r'^#([1-9][0-9]*)(?:\s|$)', run.get('display_title', ''))
    return int(match[1]) if match else None


def marker(comment):
    author = comment.get('user') or {}
    trusted_bot = author.get('login') == 'github-actions[bot]' and author.get('type') == 'Bot'
    trusted_member = comment.get('author_association') in {'OWNER', 'MEMBER', 'COLLABORATOR'}
    if not (trusted_bot or trusted_member):
        return None
    body = comment.get('body') or ''
    if not body.startswith(PREFIX):
        return None
    try:
        return json.loads(body[len(PREFIX):].split(' -->', 1)[0])
    except (ValueError, IndexError):
        return None


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def protected_run(jobs):
    # Whole-run cancellation would also kill a review or live-acceptance sibling.
    return any(job['status'] == 'in_progress' and (
        job['name'] in {'Review', 'Re-review'} or
        (job['name'] not in WORK and 'acceptance' in job['name'].lower())) for job in jobs)


def sweep(repo, current_ref, workflow='df-pipeline.yml', call=api, now=None, dispatch_ref='main'):
    now = now or datetime.now(timezone.utc)
    root = f'repos/{repo}'
    path = f'.github/workflows/{workflow}'
    # Compare immutable content; dispatch REST accepts a branch/tag, not a commit.
    target = call(f'{root}/commits/{quote(current_ref, safe="")}')['sha']
    blob = call(f'{root}/contents/{path}?ref={target}')['sha']
    runs = [run for page in call(
        f'{root}/actions/workflows/{workflow}/runs?per_page=100', pages=True)
        for run in page['workflow_runs']]
    report = []
    issue_cache, comments_cache, blob_cache = {}, {}, {target: blob}

    def save(n, record, comment_id=None):
        body = PREFIX + json.dumps(record, sort_keys=True) + ' -->\n'
        body += f"DF_STRAGGLER run={record['run']} reason={record['reason']} disposition={record['state']}\n"
        body += f"https://github.com/{repo}/actions/runs/{record['run']}\n"
        endpoint = f'{root}/issues/comments/{comment_id}' if comment_id else f'{root}/issues/{n}/comments'
        answer = call(endpoint, 'PATCH' if comment_id else 'POST', {'body': body})
        return answer['id']
    def consolidate(n, run_id, comments):
        found = [(comment, marker(comment)) for comment in comments]
        found = [(comment, record) for comment, record in found
                 if record and record.get('run') == run_id]
        if not found:
            return None, None
        # Never let an earlier pending copy resurrect a dispatch already claimed.
        priority = {'dispatched': 9, 'dispatch-unknown': 8, 'dispatch-claimed': 7,
                    'scope-changed': 6, 'ineligible': 5, 'superseded': 4,
                    'await-completion': 2, 'cancel-requested': 1}
        canonical = min(comment['id'] for comment, _ in found)
        record = dict(max(found, key=lambda pair: priority.get(pair[1]['state'], 0))[1])
        if len(found) > 1:
            # Conflicting snapshots are not authority to restart changed work.
            if priority.get(record['state'], 0) < 4 and len({r['scope'] for _, r in found}) > 1:
                record['state'] = 'scope-changed'
            # Keep aliases readable; all future readers select the same strongest state.
            if next(r for c, r in found if c['id'] == canonical) != record:
                save(n, record, canonical)
        return canonical, record

    for run in runs:
        n = issue_number(run)
        if not n or str(run['id']) == os.environ.get('GITHUB_RUN_ID'):
            continue
        if n not in issue_cache:
            issue_cache[n] = call(f'{root}/issues/{n}')
            comments_cache[n] = [comment for page in call(
                f'{root}/issues/{n}/comments?per_page=100', pages=True) for comment in page]
        issue = issue_cache[n]
        comment_id, record = consolidate(n, run['id'], comments_cache[n])
        if record and record['state'] in {'dispatched', 'dispatch-claimed', 'dispatch-unknown', 'scope-changed', 'ineligible', 'superseded'}:
            if record['state'] in {'dispatch-claimed', 'dispatch-unknown'}:
                report.append(dict(record))
            continue
        if not record:
            if run['status'] not in ACTIVE or not eligible(issue):
                continue
            head = run['head_sha']
            if head not in blob_cache:
                blob_cache[head] = call(f'{root}/contents/{path}?ref={head}')['sha']
            old = blob_cache[head] != blob
            stalled = None
            if run['status'] == 'in_progress':
                jobs = [job for page in call(
                    f"{root}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", pages=True)
                        for job in page['jobs']]
                if not protected_run(jobs):
                    stalled = next((job for job in jobs if job['status'] == 'in_progress'
                                    and job['name'] in WORK and job.get('started_at')
                                    and (now - stamp(job['started_at'])).total_seconds() > 1800), None)
            if not old and not stalled:
                continue
            # Upgrade never cancels an in-progress run, even if also stalled.
            reason = 'workflow-upgrade' if old else 'stalled-work-stage'
            record = {'run': run['id'], 'issue': n, 'scope': scope(issue),
                      'target': target, 'blob': blob, 'reason': reason,
                      'state': 'await-completion' if old and run['status'] == 'in_progress' else 'cancel-requested'}
            if stalled:
                record.update(job=stalled['name'], started_at=stalled['started_at'])
            comment_id = save(n, record)
            comments_cache[n].append({'id': comment_id, 'body': PREFIX + json.dumps(record) + ' -->',
                                      'user': {'login': 'github-actions[bot]', 'type': 'Bot'}})
        # Re-read before any side effect, not just once at the start of the sweep.
        issue = call(f'{root}/issues/{n}')
        if scope(issue) != record['scope'] or not eligible(issue):
            record['state'] = 'scope-changed' if scope(issue) != record['scope'] else 'ineligible'
            save(n, record, comment_id)
            report.append(dict(record))
            continue
        latest = call(f"{root}/actions/runs/{run['id']}")
        if latest['status'] != 'completed':
            if record['state'] == 'cancel-requested':
                # A previously queued upgrade might have started since enumeration.
                if record['reason'] == 'workflow-upgrade' and latest['status'] == 'in_progress':
                    record['state'] = 'await-completion'
                    save(n, record, comment_id)
                else:
                    if record['reason'] == 'stalled-work-stage':
                        current_jobs = [job for page in call(
                            f"{root}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", pages=True)
                                        for job in page['jobs']]
                        same_stall = any(job['status'] == 'in_progress'
                                         and job['name'] == record['job']
                                         and job.get('started_at') == record['started_at']
                                         for job in current_jobs)
                        if not same_stall or protected_run(current_jobs):
                            record['state'] = 'superseded'
                            save(n, record, comment_id)
                            report.append(dict(record))
                            continue
                    call(f"{root}/actions/runs/{run['id']}/cancel", 'POST')
                    latest = call(f"{root}/actions/runs/{run['id']}")
            if latest['status'] != 'completed':
                report.append(dict(record))
                continue  # schedule/completion hook resumes after cancellation actually settles
        # A newer lap already covering this issue obviates the old lap's redispatch.
        other = next((r for r in runs if r['id'] > run['id'] and issue_number(r) == n), None)
        if other:
            record['state'] = 'superseded'
            record['successor'] = other['id']
            save(n, record, comment_id)
        else:
            # Another sweep may have created/claimed a marker after enumeration.
            fresh_comments = [comment for page in call(
                f'{root}/issues/{n}/comments?per_page=100', pages=True) for comment in page]
            comment_id, fresh = consolidate(n, run['id'], fresh_comments)
            if fresh['state'] in {'dispatched', 'dispatch-claimed', 'dispatch-unknown',
                                  'scope-changed', 'ineligible', 'superseded'}:
                report.append(dict(fresh))
                continue
            record = fresh
            record.update(state='dispatch-claimed', target=target, blob=blob)
            save(n, record, comment_id)  # durable claim precedes non-idempotent REST POST
            try:
                call(f'{root}/actions/workflows/{workflow}/dispatches', 'POST',
                     {'ref': dispatch_ref, 'inputs': {'issue': str(n)}})
            except Exception:
                record['state'] = 'dispatch-unknown'
                save(n, record, comment_id)
                raise RuntimeError(f"Run {run['id']}: dispatch outcome unknown; reconcile receipt, never blindly retry")
            record['state'] = 'dispatched'
            save(n, record, comment_id)
        report.append(dict(record))
    return report


def self_test():
    import copy
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    stale = {'name': 'Build', 'status': 'in_progress', 'started_at': '2026-09-23T10:00:00Z'}
    for name in ('Review', 'Re-review'):
        review = dict(stale, name=name)
        for steps in ([], [{'name': 'Prepare runtime', 'status': 'in_progress',
                            'started_at': '2026-09-23T10:30:00Z'}],
                      [{'name': 'Independent review', 'status': 'in_progress',
                        'started_at': '2026-09-23T11:29:59Z'}]):
            review['steps'] = steps
            assert protected_run([stale, review])
        for status in ('queued', 'waiting', 'completed'):
            assert not protected_run([dict(review, status=status), stale])
    issues = {n: {'state': 'open', 'title': f'Issue {n}', 'body': 'scope', 'labels': []}
              for n in range(1, 9)}
    runs = [{'id': n, 'display_title': f'#{n} work', 'head_sha': 'old' if n < 3 else 'unrelated',
             'status': 'queued' if n in (1, 4) else 'in_progress'} for n in issues]
    comments, effects = {n: [] for n in issues}, []
    jobs = {n: [{'name': 'Rework', 'status': 'in_progress', 'started_at': '2026-09-23T11:29:59Z'}]
            for n in issues}
    jobs[4][0]['status'] = 'queued'
    jobs[5][0]['name'] = 'Live Slack acceptance'
    jobs[6][0]['started_at'] = '2026-09-23T11:30:00Z'
    jobs[7][0]['status'] = 'queued'
    jobs[8][0]['status'] = 'waiting'
    jobs[7][0].update(name='Review', status='in_progress')
    jobs[8].append(dict(jobs[3][0], name='Re-review'))
    def fake(url, method='GET', data=None, pages=False):
        route = url.split('/repos/', 1)[-1] if '/repos/' in url else url
        if '/commits/' in route:
            return {'sha': 'new'}
        if '/contents/' in route:
            return {'sha': 'oldblob' if route.endswith('=old') else 'newblob'}
        if '/workflows/' in route and '/runs?' in route:
            assert pages
            return [{'workflow_runs': copy.deepcopy(runs[:4])}, {'workflow_runs': copy.deepcopy(runs[4:])}]
        if route.endswith('/dispatches'):
            effects.append(('dispatch', int(data['inputs']['issue'])))
            return None
        if '/actions/runs/' in route:
            n = int(route.split('/actions/runs/')[1].split('/')[0])
            if '/jobs?' in route:
                assert pages
                return [{'jobs': copy.deepcopy(jobs[n])}]
            if route.endswith('/cancel'):
                effects.append(('cancel', n))
                runs[n - 1]['status'] = 'completed'
            return copy.deepcopy(runs[n - 1])
        if '/issues/comments/' in route:
            cid = int(route.rsplit('/', 1)[1])
            comment = next(c for cs in comments.values() for c in cs if c['id'] == cid)
            comment.update(data)
            return comment
        n = int(route.split('/issues/')[1].split('/')[0])
        if '/comments' in route:
            if method == 'POST':
                comment = {'id': 100 + sum(map(len, comments.values())),
                           'user': {'login': 'github-actions[bot]', 'type': 'Bot'}, **data}
                comments[n].append(comment)
                return comment
            assert pages
            return [copy.deepcopy(comments[n])]
        return copy.deepcopy(issues[n])
    first = sweep('owner/repo', 'main', call=fake, now=now)
    assert effects == [('cancel', 1), ('dispatch', 1), ('cancel', 3), ('dispatch', 3)], effects
    assert next(r for r in first if r['run'] == 2)['state'] == 'await-completion'
    sweep('owner/repo', 'main', call=fake, now=now)
    assert len(effects) == 4
    runs[1]['status'] = 'completed'
    sweep('owner/repo', 'main', call=fake, now=now)
    sweep('owner/repo', 'main', call=fake, now=now)
    assert effects[-1] == ('dispatch', 2) and len(effects) == 5, effects
    # Revalidate old cancellation receipts: active review now protects the run.
    jobs[3].append(dict(jobs[3][0], name='Re-review'))
    runs[2]['status'] = 'in_progress'
    prior = marker(comments[3][0])
    prior['state'] = 'cancel-requested'
    comments[3][0]['body'] = PREFIX + json.dumps(prior) + ' -->'
    sweep('owner/repo', 'main', call=fake, now=now)
    assert len(effects) == 5
    assert marker(comments[3][0])['state'] == 'superseded'
    print('PASS: active Review/Re-review protect setup, mixed jobs and pending cancellation receipts')
    # Deferred upgrade observes closure and scope changes rather than restarting.
    for state in ('closed', 'changed'):
        runs[1]['status'] = 'in_progress'
        comments[2].clear()
        sweep('owner/repo', 'main', call=fake, now=now)
        runs[1]['status'] = 'completed'
        if state == 'closed':
            issues[2]['state'] = 'closed'
        else:
            issues[2]['body'] = 'different scope'
        sweep('owner/repo', 'main', call=fake, now=now)
        assert len(effects) == 5
        issues[2]['state'] = 'open'
    # Cancellation acknowledgment is not completion: no redispatch until observed.
    comments[1].clear()
    runs[0]['status'] = 'queued'
    def delayed(url, method='GET', data=None, pages=False):
        if url.endswith('/runs/1/cancel'):
            effects.append(('cancel-pending', 1))
            return None
        return fake(url, method, data, pages)
    sweep('owner/repo', 'main', call=delayed, now=now)
    assert effects[-1] == ('cancel-pending', 1) and len(effects) == 6
    runs[0]['status'] = 'completed'
    sweep('owner/repo', 'main', call=delayed, now=now)
    assert effects[-1] == ('dispatch', 1) and len(effects) == 7
    # A lost HTTP response after dispatch must never cause a duplicate POST.
    comments[1].clear()
    runs[0]['status'] = 'queued'
    def ambiguous(url, method='GET', data=None, pages=False):
        answer = fake(url, method, data, pages)
        if url.endswith('/dispatches'):
            raise RuntimeError('response lost after server accepted dispatch')
        return answer
    try:
        sweep('owner/repo', 'main', call=ambiguous, now=now)
        raise AssertionError('expected ambiguous dispatch failure')
    except RuntimeError as error:
        assert 'outcome unknown' in str(error)
    count = len(effects)
    sweep('owner/repo', 'main', call=ambiguous, now=now)
    assert len(effects) == count and marker(comments[1][0])['state'] == 'dispatch-unknown'
    forged = dict(comments[1][0], user={'login': 'outsider', 'type': 'User'})
    assert marker(forged) is None
    for association in ('OWNER', 'MEMBER', 'COLLABORATOR'):
        assert marker(dict(forged, author_association=association)) is not None
    assert marker(dict(forged, author_association='CONTRIBUTOR')) is None
    pending = marker(comments[1][0])
    pending['state'] = 'await-completion'
    comments[1] = [dict(comments[1][0], id=1000 + offset,
                        body=PREFIX + json.dumps(pending) + ' -->') for offset in range(2)]
    runs[0]['status'] = 'in_progress'
    sweep('owner/repo', 'main', call=fake, now=now)
    assert len(effects) == count
    runs[0]['status'] = 'completed'
    sweep('owner/repo', 'main', call=fake, now=now)
    sweep('owner/repo', 'main', call=fake, now=now)
    assert len(effects) == count + 1 and effects[-1] == ('dispatch', 1)
    for terminal in ('dispatched', 'dispatch-claimed', 'dispatch-unknown'):
        finished = dict(pending, state=terminal)
        comments[1][0]['body'] = PREFIX + json.dumps(pending) + ' -->'
        comments[1][1]['body'] = PREFIX + json.dumps(finished) + ' -->'
        sweep('owner/repo', 'main', call=fake, now=now)
        assert len(effects) == count + 1
        assert marker(comments[1][0])['state'] == terminal
    print('PASS: duplicate pending aliases merge; completion dispatch once; any dispatched/claimed/unknown copy suppresses replay')
    print('PASS: paginated blob comparison; queued upgrade cancel-before-dispatch; in-progress deferred once; repeated sweep no duplicates; >30m Rework recycled; queued/dependency waits, acceptance, exact 30m excluded; closed/changed scope suppressed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['sweep', 'self-test'])
    parser.add_argument('--repo', default=os.environ.get('R') or os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--current-ref', default='main')
    parser.add_argument('--workflow', default='df-pipeline.yml')
    parser.add_argument('--dispatch-ref', default='main', help='branch/tag for workflow_dispatch')
    args = parser.parse_args()
    if args.command == 'self-test':
        self_test()
        return
    if not args.repo:
        parser.error('--repo or R required')
    report = sweep(args.repo, args.current_ref, args.workflow, dispatch_ref=args.dispatch_ref)
    text = json.dumps(report, sort_keys=True)
    print(text)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('straggler_report=' + text + '\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('## Straggler sweep\n\n```json\n' + json.dumps(report, indent=2) + '\n```\n')


if __name__ == '__main__':
    main()
