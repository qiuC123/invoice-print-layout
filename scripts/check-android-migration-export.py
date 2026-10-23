"""Synthetic-only checks for the read-only desktop-to-phone converter."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('android_export', Path(__file__).with_name('export-android-migration.py'))
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MigrationExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='invoice-android-export-')
        self.output = Path(self.temporary.name) / 'portable.zip'
        self.state = {'projects': [{'id': 'project', 'name': '合成项目', 'reimbursement_status': 'unreimbursed'}],
                      'items': [{'id': 'expense', 'project_id': 'project', 'title': '=literal', 'category': '高铁',
                                 'ready': True, 'stage': 'draft', 'expense_date': '2026-10-17',
                                 'amount_cents': 2345, 'order_number': '10000000100000000001',
                                 'reimbursement_person_id': 'person', 'reimbursement_person_label': '合成人员',
                                 'attachments': [{'id': 'proof', 'name': 'synthetic.pdf', 'role': 'invoice'}]}]}
        self.people = {'project': {'people': [{'id': 'person', 'label': '合成人员'}]}}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_cents_ids_and_text_are_preserved_without_tokens(self) -> None:
        self.state['token'] = 'MUST_NOT_EXPORT'
        before = json.dumps(self.state, sort_keys=True)
        result = module.package(self.state, self.people, [], ['project'], lambda _: b'%PDF-SYNTHETIC', self.output)
        self.assertEqual(1, result['expenses'])
        self.assertEqual(before, json.dumps(self.state, sort_keys=True))
        with zipfile.ZipFile(self.output) as archive:
            raw = archive.read('manifest.json').decode()
            manifest = json.loads(raw)
            data = json.loads(manifest['tables']['expenses'][0]['data'])
            self.assertEqual(2345, data['cents'])
            self.assertEqual('10000000100000000001', data['order_number'])
            self.assertEqual('=literal', data['title'])
            self.assertNotIn('MUST_NOT_EXPORT', raw)
            self.assertTrue(json.loads(manifest['tables']['materials'][0]['fields'])['manual_role'])

    def test_duplicate_project_and_changed_original_stop_export(self) -> None:
        with self.assertRaises(ValueError):
            module.package(self.state, self.people, [], ['project', 'project'], lambda _: b'%PDF-', self.output)
        self.state['items'][0]['attachments'][0]['digest'] = '0' * 64
        with self.assertRaises(ValueError):
            module.package(self.state, self.people, [], ['project'], lambda _: b'%PDF-', self.output)
        self.assertFalse(self.output.exists())

    def test_existing_output_never_overwritten(self) -> None:
        self.output.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            module.package(self.state, self.people, [], ['project'], lambda _: b'%PDF-', self.output)
        self.assertEqual(b'keep', self.output.read_bytes())


if __name__ == '__main__':
    unittest.main()
