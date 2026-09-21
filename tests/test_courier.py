from pathlib import Path
from decimal import Decimal
import shutil

import pymupdf
import pytest

from invoice_print_layout.courier import inspect_courier_pdf, process_courier_batch
from invoice_print_layout.bot import BatchKind, BotController, BotError, FeishuBotSettings
from invoice_print_layout.mail163 import MailImportError, MailImportSummary
from invoice_print_layout.storage import ensure_workspace, read_history, write_person_name
from tests.test_bot import FakeGateway, message

NUMBER = "26000000000000000001"


def make_courier_pair(folder: Path, kind: str, number: str = NUMBER, pages: int = 1) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    invoice, detail = folder / 'invoice.pdf', folder / 'detail.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=397)
        lines = ['电子发票', '顺丰速运有限公司' if kind == '顺丰' else '顺丰同城物流有限公司',
                 '发票号码：' + number, '开票日期：2026年09月18日', '价税合计']
        for i, line in enumerate(lines):
            page.insert_text((20, 25 + i * 25), line, fontname='china-s', fontsize=10)
        page.insert_text((300, 250), '（小写）', fontname='china-s', fontsize=10)
        # Currency visually overlaps the closing full-width bracket's glyph box,
        # as in the SF source PDF; locate the words, not the bracket edge.
        page.insert_text((336, 250), '¥43.00', fontsize=10)
        # Tax extracted LAST must never be mistaken for invoice total.
        page.insert_text((480, 190), '¥2.43', fontsize=10)
        page.add_freetext_annot(pymupdf.Rect(500, 290, 590, 312), 'Download:1', fontsize=9)
        doc.save(invoice)
    with pymupdf.open() as doc:
        for i in range(pages):
            page = doc.new_page(width=595, height=842)
            lines = (['顺丰电子发票 — 运单明细', '发票号码：' + number, '发票总金额(元)43.00']
                     if kind == '顺丰' else ['运单起止日期：2026-09-01至2026-09-01', '共1笔运单，合计43.00元', '订单号 发件信息 收件信息'])
            lines += [f'运单记录第{i + 1}页', f'SF123456789{i} 43.00']
            for j, line in enumerate(lines):
                page.insert_text((20, 45 + j * 28), line, fontname='china-s', fontsize=10)
            page.insert_text((500, 815), f'{i+1}/{pages}', fontsize=10)
        doc.save(detail)
    return [invoice, detail]


@pytest.mark.parametrize('kind', ['顺丰', '同城'])
def test_amount_pair_archive_and_duplicate(tmp_path: Path, kind: str) -> None:
    originals = make_courier_pair(tmp_path / 'original', kind)
    copies = tmp_path / 'copies'
    copies.mkdir()
    files = [Path(shutil.copy2(p, copies / p.name)) for p in originals]
    assert inspect_courier_pdf(files[0], kind).amount == Decimal('43.00')
    assert not inspect_courier_pdf(files[1], kind).is_invoice
    paths, result = process_courier_batch(tmp_path / 'workspace', files, 'Test', kind)
    assert result.success_count == 1, result
    history = read_history(paths.history)
    assert history[0]['category'] == ('快递' if kind == '顺丰' else '同城配送')
    assert history[0]['amount'] == '43.00'
    with pymupdf.open(history[0]['output']) as pdf:
        assert len(pdf) == 1
        assert abs(pdf[0].rect.height - 841.89) < 1
        assert 'SF1234567890' in pdf[0].get_text()
        assert 'Download:1' in pdf[0].get_text()
    assert len(list(Path(history[0]['archive']).glob('*.pdf'))) == 2
    _, repeated = process_courier_batch(paths.root, originals, 'Test', kind)
    assert repeated.duplicate_count == 1
    assert len(read_history(paths.history)) == 1


def test_three_detail_pages_pair_last_with_invoice(tmp_path: Path) -> None:
    files = make_courier_pair(tmp_path / 'source', '顺丰', pages=3)
    paths, result = process_courier_batch(tmp_path / 'workspace', files, 'Test', '顺丰')
    assert result.success_count == 1, result
    with pymupdf.open(read_history(paths.history)[0]['output']) as pdf:
        assert len(pdf) == 2
        assert 'SF1234567890' in pdf[0].get_text() and 'SF1234567891' in pdf[0].get_text()
        assert 'SF1234567892' in pdf[1].get_text() and NUMBER in pdf[1].get_text()


def test_express_mismatched_invoice_number_needs_review(tmp_path: Path) -> None:
    a = make_courier_pair(tmp_path / 'a', '顺丰')
    b = make_courier_pair(tmp_path / 'b', '顺丰', '26000000000000000002')
    paths, result = process_courier_batch(tmp_path / 'workspace', [a[0], b[1]], 'Test', '顺丰')
    assert result.success_count == 0 and result.review_count == 1
    assert read_history(paths.history) == []


def test_city_ambiguous_amount_needs_review(tmp_path: Path) -> None:
    a = make_courier_pair(tmp_path / 'a', '同城')
    b = make_courier_pair(tmp_path / 'b', '同城', '26000000000000000002')
    _, result = process_courier_batch(tmp_path / 'workspace', a + b, 'Test', '同城')
    assert result.success_count == 0 and result.review_count == 1


def test_wrong_kind_is_rejected(tmp_path: Path) -> None:
    files = make_courier_pair(tmp_path / 'source', '同城')
    for file in files:
        with pytest.raises(ValueError, match='类型'):
            inspect_courier_pdf(file, '顺丰')


@pytest.mark.parametrize('command,kind', [('顺丰', '顺丰'), ('同城', '顺丰'), ('顺丰同城', '顺丰')])
def test_bot_courier_mail_to_pdf_and_md(tmp_path: Path, command: str, kind: str) -> None:
    paths = ensure_workspace(tmp_path / 'workspace')
    write_person_name(paths.config, 'Test')
    gateway = FakeGateway()
    calls = []
    def importer(destination: Path, selected: str) -> MailImportSummary:
        calls.append(selected)
        make_courier_pair(destination, selected)
        return MailImportSummary(downloaded=2)
    controller = BotController(paths.root, paths.root / 'bot.toml',
        FeishuBotSettings('cli_abcdefgh1234', 'ou_owner'), None, gateway, courier_mail_importer=importer)
    incoming = message('courier', text=command)
    controller.handle(incoming)
    controller.handle(incoming)
    assert calls == [kind]
    assert controller.inbox.current() is None
    assert [file.suffix for _, file in gateway.sent_files] == ['.pdf', '.md']
    assert len(read_history(paths.history)) == 1
    assert '43.00' in gateway.sent_files[1][1].read_text(encoding='utf-8')


def test_courier_partial_download_retry(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / 'workspace')
    write_person_name(paths.config, 'Test')
    original = make_courier_pair(tmp_path / 'source', '顺丰')
    calls = 0
    def importer(destination: Path, kind: str) -> MailImportSummary:
        nonlocal calls
        calls += 1
        shutil.copy2(original[calls - 1], destination)
        if calls == 1:
            raise MailImportError('temporary')
        return MailImportSummary(downloaded=1)
    gateway = FakeGateway()
    controller = BotController(paths.root, paths.root / 'bot.toml',
        FeishuBotSettings('cli_abcdefgh1234', 'ou_owner'), None, gateway, courier_mail_importer=importer)
    with pytest.raises(BotError):
        controller.handle(message('courier', text='顺丰'))
    controller.handle(message('continue', text='处理'))
    assert calls == 2 and len(read_history(paths.history)) == 1
    assert len(gateway.sent_files) == 2


def test_manual_courier_and_batch_isolation(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / 'workspace')
    write_person_name(paths.config, 'Test')
    gateway = FakeGateway()
    controller = BotController(paths.root, paths.root / 'bot.toml',
        FeishuBotSettings('cli_abcdefgh1234', 'ou_owner'), None, gateway)
    controller.handle(message('manual', text='同城'))
    batch = controller.inbox.current()
    assert batch and batch.kind == BatchKind.EXPRESS
    with pytest.raises(BotError):
        controller.handle(message('wrong', text='顺丰'))
    assert controller.inbox.current() == batch
    make_courier_pair(batch.directory / 'files', '同城')
    controller.handle(message('process', text='处理'))
    assert len(gateway.sent_files) == 2


def test_unified_courier_returns_one_bundle_without_cross_pairing(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / 'workspace')
    write_person_name(paths.config, 'Test')
    gateway = FakeGateway()
    def importer(destination: Path, selected: str) -> MailImportSummary:
        assert selected == '顺丰'
        for kind, number in [('顺丰', NUMBER), ('同城', '26000000000000000002')]:
            for file in make_courier_pair(tmp_path / kind, kind, number):
                shutil.copy2(file, destination / (kind + file.name))
        return MailImportSummary(downloaded=4)
    controller = BotController(paths.root, paths.root / 'bot.toml',
        FeishuBotSettings('cli_abcdefgh1234', 'ou_owner'), None, gateway, courier_mail_importer=importer)
    controller.handle(message('both', text='顺丰'))
    records = read_history(paths.history)
    assert {r['category'] for r in records} == {'快递', '同城配送'}
    assert len(records) == 2
    assert [p.suffix for _, p in gateway.sent_files] == ['.pdf', '.md']
    with pymupdf.open(gateway.sent_files[0][1]) as pdf:
        assert len(pdf) == 2
    markdown = gateway.sent_files[1][1].read_text(encoding='utf-8')
    assert '86.00' in markdown and '快递' in markdown and '同城配送' in markdown


def test_unified_courier_does_not_pair_cross_provider_evidence(tmp_path: Path) -> None:
    express = make_courier_pair(tmp_path / 'express', '顺丰')
    city = make_courier_pair(tmp_path / 'city', '同城', '26000000000000000002')
    paths, result = process_courier_batch(tmp_path / 'workspace', [express[0], city[1]], 'Test')
    assert result.success_count == 0 and result.review_count == 1
    assert read_history(paths.history) == []
