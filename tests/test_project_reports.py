from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from typing import Any
import zipfile

import pymupdf
import pytest

from invoice_print_layout import project_reports, reimbursement
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.project_events import project_snapshot
from invoice_print_layout.workbench import ExpenseStore
from tests.test_material_matching import proof


@pytest.fixture
def package_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ExpenseStore, str]:
    store = ExpenseStore(tmp_path / 'workspace')
    project = ExpenseProjects(store).create('Synthetic package project')['id']
    (store.root / reimbursement.TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(reimbursement, 'validate_template', lambda _: None)
    monkeypatch.setattr(reimbursement, 'spreadsheet_runtime', lambda: (Path('node'), Path('modules')))
    monkeypatch.setattr(project_reports, 'validate_template', lambda _: None)
    monkeypatch.setattr(project_reports, 'spreadsheet_runtime', lambda: (Path('node'), Path('modules')))

    def excel(template: Path, items: list[dict[str, Any]], options: dict[str, str], output: Path,
              *, summary_pdf: Path | None = None) -> None:
        assert summary_pdf is not None
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('xl/workbook.xml', '<workbook/>')
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((30, 30), f"SYNTHETIC TABLE {sum(x['amount_cents'] for x in items) / 100:.2f}")
            document.save(summary_pdf)

    monkeypatch.setattr(reimbursement, 'create_excel', excel)
    return store, project


def ready(store: ExpenseStore, project: str, title: str = 'Synthetic hotel', amount: str = '12.34') -> dict[str, Any]:
    item = store.create({'title': title, 'category': '酒店', 'project_id': project, 'amount': amount,
                         'expense_date': '2026-01-02'})
    store.add_attachment(item['id'], title + '.pdf', proof(title), 'invoice')
    return store.transition(item['id'], 'verify')


def version(store: ExpenseStore, project: str) -> str:
    with store.connect() as db:
        return str(project_snapshot(store, db, project)['version'])


def test_project_package_files_and_persistent_idempotency(package_store: tuple[ExpenseStore, str]) -> None:
    store, project = package_store
    first = ready(store, project)
    second = ready(store, project, 'Another hotel', '7.66')
    store.create({'title': 'Pending', 'category': '酒店', 'project_id': project, 'amount': '10'})
    foreign_project = ExpenseProjects(store).create('Other synthetic project')['id']
    foreign = ready(store, foreign_project)
    result = project_reports.create_project_report(store, project, 'package-001', version(store, project))
    assert result['ok'] and not result['reused']
    assert set(result['expense_ids']) == {first['id'], second['id']}
    assert result['expense_count'] == 2 and result['total_cents'] == 2000
    folder = store.root / '导出'
    before = {name: (folder / name).read_bytes() for name in result['files'].values()}
    with pymupdf.open(folder / result['files']['pdf']) as document:
        assert len(document) >= 2 and '20.00' in document[0].get_text()
    with zipfile.ZipFile(folder / result['files']['xlsx']) as archive:
        assert 'xl/workbook.xml' in archive.namelist()
    assert '20.00' in (folder / result['files']['md']).read_text(encoding='utf-8')
    repeated = project_reports.create_project_report(ExpenseStore(store.workspace), project, 'package-001', 'old-version')
    assert repeated == {**result, 'reused': True}
    assert before == {name: (folder / name).read_bytes() for name in result['files'].values()}
    assert len(list(folder.iterdir())) == 3
    assert store.get(foreign['id'])['stage'] == 'draft'
    assert store.get(first['id'])['stage'] == store.get(second['id'])['stage'] == 'submitted'


@pytest.mark.parametrize(('project', 'request_id', 'expected'), [('', 'ok', None), ('p', '../bad', None),
                                                              ('p', '', None), ('p', 'ok', 12)])
def test_invalid_project_package_request(package_store: tuple[ExpenseStore, str], project: str,
                                         request_id: str, expected: Any) -> None:
    result = project_reports.create_project_report(package_store[0], project, request_id, expected)
    assert result['error']['code'] == 'invalid_request' and not result['error']['retryable']


def test_empty_batch_stale_version_and_request_project_conflict(package_store: tuple[ExpenseStore, str]) -> None:
    store, project = package_store
    assert project_reports.create_project_report(store, project, 'empty')['error']['code'] == 'nothing_to_package'
    assert project_reports.create_project_report(store, 'missing', 'missing')['error']['code'] == 'project_not_found'
    old = version(store, project)
    ready(store, project)
    assert project_reports.create_project_report(store, project, 'stale', old)['error']['code'] == 'version_changed'
    assert project_reports.create_project_report(store, project, 'one')['ok']
    other = ExpenseProjects(store).create('Synthetic other')['id']
    result = project_reports.create_project_report(store, other, 'one')
    assert result['error']['code'] == 'request_id_conflict' and not result['error']['retryable']


@pytest.mark.parametrize(('kind', 'code', 'retryable'), [('missing', 'template_missing', True),
    ('template', 'template_invalid', True), ('runtime', 'runtime_unavailable', True),
    ('excel', 'excel_error', True), ('storage', 'storage_error', True), ('materials', 'report_error', False)])
def test_package_failure_codes_keep_drafts(package_store: tuple[ExpenseStore, str], monkeypatch: pytest.MonkeyPatch,
                                           kind: str, code: str, retryable: bool) -> None:
    store, project = package_store
    item = ready(store, project)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise (OSError('disk unavailable') if kind == 'storage' else
               ValueError({'template': '模板损坏', 'runtime': '运行环境缺失', 'excel': 'Excel制作失败',
                           'materials': '原始凭证不可用'}.get(kind, 'failed')))

    if kind == 'missing':
        (store.root / reimbursement.TEMPLATE_NAME).unlink()
    elif kind in {'template', 'runtime'}:
        monkeypatch.setattr(project_reports, 'validate_template' if kind == 'template' else 'spreadsheet_runtime', fail)
    else:
        monkeypatch.setattr(reimbursement, 'create_excel' if kind in {'excel', 'storage'} else 'compose_matter', fail)
    result = project_reports.create_project_report(store, project, 'failure')
    assert result['error']['code'] == code and result['error']['retryable'] == retryable
    assert store.get(item['id'])['stage'] == 'draft'
    assert not list((store.root / '导出').glob('*'))


def test_retryable_failure_can_resume_same_request(package_store: tuple[ExpenseStore, str]) -> None:
    store, project = package_store
    ready(store, project)
    template = store.root / reimbursement.TEMPLATE_NAME
    template.unlink()
    assert project_reports.create_project_report(store, project, 'retry')['error']['code'] == 'template_missing'
    template.write_bytes(b'synthetic template')
    assert project_reports.create_project_report(store, project, 'retry')['ok']


def test_existing_package_size_limit_has_explicit_code(package_store: tuple[ExpenseStore, str],
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import project_events
    store, project = package_store
    monkeypatch.setattr(project_events, 'project_snapshot', lambda *_: {'version': 'v',
                         'expense_ids': [str(index) for index in range(201)]})
    result = project_reports.create_project_report(store, project, 'large')
    assert result['error']['code'] == 'too_many_expenses' and not result['error']['retryable']


def test_project_change_during_rendering_rejects_receipt_and_files(package_store: tuple[ExpenseStore, str],
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = package_store
    first = ready(store, project)
    original = reimbursement.create_excel

    def changed(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        ready(store, project, 'Arrived during rendering')

    monkeypatch.setattr(reimbursement, 'create_excel', changed)
    result = project_reports.create_project_report(store, project, 'changed')
    assert result['error']['code'] == 'version_changed'
    assert store.get(first['id'])['stage'] == 'draft'
    assert not list((store.root / '导出').glob('*'))
    with store.connect() as db:
        assert db.execute('SELECT status FROM project_report_requests').fetchone()[0] == 'failed'


def test_receipt_rolls_back_with_existing_submit_validation(package_store: tuple[ExpenseStore, str],
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = package_store
    first = ready(store, project)
    original = reimbursement.create_excel

    def changed(template: Path, items: list[dict[str, Any]], *args: Any, **kwargs: Any) -> None:
        original(template, items, *args, **kwargs)
        items[0]['merchant'] = 'Unstored render mutation'

    monkeypatch.setattr(reimbursement, 'create_excel', changed)
    result = project_reports.create_project_report(store, project, 'rollback')
    assert result['error']['code'] == 'version_changed'
    assert store.get(first['id'])['stage'] == 'draft'
    with store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM project_report_requests').fetchone()[0])
    assert 'result' not in data


def test_concurrent_requests_do_not_render_twice(package_store: tuple[ExpenseStore, str], monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = package_store
    ready(store, project)
    entered, finish = threading.Event(), threading.Event()
    original = reimbursement.create_report

    def slow(*args: Any, **kwargs: Any) -> dict[str, str]:
        entered.set()
        assert finish.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(reimbursement, 'create_report', slow)
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(project_reports.create_project_report, store, project, 'concurrent')
        try:
            assert entered.wait(10)
            blocked = project_reports.create_project_report(store, project, 'concurrent')
            assert blocked['error']['code'] == 'in_progress' and blocked['error']['retryable']
        finally:
            finish.set()
        result = running.result(timeout=20)
    assert result['ok']
    assert project_reports.create_project_report(store, project, 'concurrent')['reused']
    assert len(list((store.root / '导出').iterdir())) == 3


@pytest.mark.parametrize('after_submit', [False, True])
def test_interrupted_process_request_recovers_without_second_package(package_store: tuple[ExpenseStore, str],
                                                                     monkeypatch: pytest.MonkeyPatch, after_submit: bool) -> None:
    store, project = package_store
    ready(store, project)
    original = reimbursement.create_report

    def interrupted(*args: Any, **kwargs: Any) -> dict[str, str]:
        if after_submit:
            original(*args, **kwargs)
        raise SystemExit('synthetic process exit')

    monkeypatch.setattr(reimbursement, 'create_report', interrupted)
    with pytest.raises(SystemExit):
        project_reports.create_project_report(store, project, 'interrupted')
    monkeypatch.setattr(reimbursement, 'create_report', original)
    result = project_reports.create_project_report(ExpenseStore(store.workspace), project, 'interrupted')
    assert result['ok'] and result['reused'] is after_submit
    assert len(list((store.root / '导出').iterdir())) == 3


def test_process_exit_after_publication_reuses_the_same_filenames(package_store: tuple[ExpenseStore, str],
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = package_store
    ready(store, project)
    original = store.submit_report

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise SystemExit('synthetic exit between file publication and DB submission')

    monkeypatch.setattr(store, 'submit_report', interrupted)
    with pytest.raises(SystemExit):
        project_reports.create_project_report(store, project, 'published-then-exit')
    filenames = {path.name for path in (store.root / '导出').iterdir()}
    assert len(filenames) == 3
    monkeypatch.setattr(store, 'submit_report', original)
    result = project_reports.create_project_report(store, project, 'published-then-exit')
    assert result['ok'] and not result['reused']
    assert set(result['files'].values()) == filenames
    assert {path.name for path in (store.root / '导出').iterdir()} == filenames
