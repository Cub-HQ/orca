import unittest

from factory_receipts import Factory, fingerprint


class ProducerReceiptGateTest(unittest.TestCase):
    def setUp(self):
        self.issue = {'state': 'open', 'title': 'Repair generator', 'body': '', 'labels': [{'name': 'bug'}]}
        self.run = {'status': 'in_progress', 'conclusion': None, 'run_started_at': '2026-09-24T00:00:00Z'}
        self.pull = {'number': 7, 'state': 'open', 'merged': False, 'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}
        self.comments = []
        self.files = [{'filename': 'tools/render.py'}]
        self.factory = Factory('o/r', 9, 1, request=self.api)
        self.factory.admission()
        self.body = 'DF_PR=7\nHEAD_SHA=' + self.pull['head']['sha']
        self.proof = {'Producer': 'tools/render.py generates the installed card',
                      'Producer fix': 'tools/render.py now preserves the required rule',
                      'Regeneration proof': 'python3 tools/render.py --temp: regenerated card retains rule'}

    def api(self, path, method='GET', data=None, pages=False):
        if path == 'repos/o/r/issues/9':
            return self.issue
        if path == 'repos/o/r/actions/runs/1':
            return self.run
        if path == 'repos/o/r/pulls/7':
            return self.pull
        if path == 'repos/o/r/pulls/7/files?per_page=100':
            if self.files is None:
                raise RuntimeError('files API unavailable')
            return self.files if pages else self.files[:100]
        if '/comments' in path:
            if method == 'POST':
                return self.comment(data['body'])
            return self.comments if '/issues/9/' in path else []
        raise AssertionError(path)

    def comment(self, body):
        result = {'id': len(self.comments) + 1, 'body': body, 'user': {'login': 'github-actions[bot]'},
                  'created_at': '2026-09-24T00:01:00Z'}
        self.comments.append(result)
        return result

    def receipt(self, proof):
        return self.comment(self.body + '\nISSUE_FINGERPRINT=' + fingerprint(self.issue) +
                            ''.join('\n' + key + ': ' + value for key, value in proof.items()))

    def test_bug_rejects_missing_blank_placeholder_and_duplicate_fields(self):
        for field in self.proof:
            for value in (None, '', '   ', 'TODO', 'TBD', 'N/A', 'none', 'pending', '<producer>', '...', 'TODO: fill this in'):
                with self.subTest(field=field, value=value):
                    proof = dict(self.proof)
                    if value is None:
                        del proof[field]
                    else:
                        proof[field] = value
                    receipt = self.receipt(proof)
                    self.assertEqual(self.factory.resume('build', 7)['skip'], 'false')
                    with self.assertRaises(RuntimeError):
                        self.factory.record('build', 7)
                    self.comments.remove(receipt)
            receipt = self.receipt(self.proof)
            receipt['body'] += '\n' + field + ': duplicate'
            self.assertEqual(self.factory.resume('build', 7)['skip'], 'false')
            self.comments.remove(receipt)

    def test_producer_fix_must_name_actual_changed_path(self):
        for value in ('tools/other.py repaired', 'prefix/tools/render.py repaired',
                      'tools/render.py.bak repaired', 'tools/render.py-extra repaired',
                      'the producer is fixed', 'tools/notrender.py repaired'):
            with self.subTest(value=value):
                receipt = self.receipt(dict(self.proof, **{'Producer fix': value}))
                with self.assertRaises(RuntimeError):
                    self.factory.record('build', 7)
                self.comments.remove(receipt)
        self.files = [{'filename': f'other/{n}.py'} for n in range(100)] + [{'filename': 'tools/render.py'}]
        self.receipt(dict(self.proof, **{'Producer fix': 'Changed `tools/render.py`: rule preserved'}))
        self.factory.record('build', 7)
        for quote in ('`', '"', "'"):
            self.files = [{'filename': 'tools/old generator', 'status': 'removed'}]
            self.receipt(dict(self.proof, **{'Producer fix': f'Removed {quote}tools/old generator{quote}: obsolete'}))
            self.factory.record('build', 7)

    def test_durable_path_binding_and_api_failure_fail_closed(self):
        receipt = self.receipt(self.proof)
        self.factory.record('build', 7)
        self.comments.remove(receipt)
        self.files = [{'filename': 'unrelated.py'}]
        self.assertEqual(self.factory.resume('build', 7)['skip'], 'false')
        self.files = None
        with self.assertRaises(RuntimeError):
            self.factory.resume('build', 7)
        self.receipt(self.proof)
        with self.assertRaises(RuntimeError):
            self.factory.record('build', 7)

    def test_valid_bug_evidence_survives_durable_resume(self):
        receipt = self.receipt(self.proof)
        self.factory.record('build', 7)
        self.comments.remove(receipt)
        self.assertEqual(self.factory.resume('build', 7)['skip'], 'true')

    def test_legacy_durable_build_cannot_bypass_gate_or_release(self):
        self.factory.post({'kind': 'stage', 'stage': 'build', 'owner': '1:1',
                           'fingerprint': fingerprint(self.issue), 'pr': 7, 'value': '7'})
        self.assertEqual(self.factory.resume('build', 7)['skip'], 'false')
        for stage, marker in (('review1', 'DF_REVIEW=approve'), ('qa', 'DF_QA=pass')):
            self.comment(marker + '\nHEAD_SHA=' + self.pull['head']['sha'])
            self.factory.record(stage, 7)
        with self.assertRaises(RuntimeError):
            self.factory.release_guard('ship', 7)
        self.receipt(self.proof)
        self.factory.record('build', 7)
        self.factory.release_guard('ship', 7)

    def test_rework_requires_fresh_producer_proof_for_changed_head(self):
        self.comment('INTAKE=go')
        self.factory.record('intake')
        self.receipt(self.proof)
        self.factory.record('build', 7)
        self.pull['head']['sha'] = 'c' * 40
        self.assertEqual(self.factory.resume('build', 7)['skip'], 'false')
        rework = self.comment('DF_REBASE=done\nHEAD_SHA=' + self.pull['head']['sha'] + '\nDF_REWORK=done')
        with self.assertRaises(RuntimeError):
            self.factory.record('rework', 7)
        for stage, marker in (('review2', 'DF_REVIEW=approve'), ('qa', 'DF_QA=pass')):
            self.comment(marker + '\nHEAD_SHA=' + self.pull['head']['sha'])
            self.factory.record(stage, 7)
        with self.assertRaises(RuntimeError):
            self.factory.release_guard('ship', 7)
        rework['body'] += ''.join('\n' + key + ': ' + value for key, value in self.proof.items())
        self.factory.record('rebase', 7, value='done')
        self.factory.release_guard('ship', 7)  # Native rebase proof needs no durable rework record.
        self.factory.record('rework', 7)
        self.factory.release_guard('ship', 7)

    def test_feature_receipts_remain_valid(self):
        self.files = None  # Feature/intake must not request PR files.
        self.issue['labels'] = [{'name': 'enhancement'}]
        self.comments.clear()
        self.factory.admission()
        self.receipt({})
        self.factory.record('build', 7)
        self.assertEqual(self.factory.resume('build', 7)['skip'], 'true')

    def test_bug_issue_type_also_requires_proof(self):
        for metadata in ({'labels': [{'name': 'type:bug'}]}, {'labels': [], 'type': {'name': 'Bug'}}):
            self.issue.update(metadata)
            self.comments.clear()
            self.factory.admission()
            self.receipt({})
            self.assertEqual(self.factory.resume('build', 7)['skip'], 'false')


if __name__ == '__main__':
    unittest.main()
