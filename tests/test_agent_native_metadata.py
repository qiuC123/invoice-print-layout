"""Native footer values need positioned labels, never a name-shaped tail."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from invoice_print_layout.agent_evidence import _Word, _native_metadata, material_facts


def word(text: str, x: float, y: float = 100) -> _Word:
    return _Word(text, x, y, x + len(text) * 8, y + 9)


@pytest.mark.parametrize(('label', 'value', 'kind'), [
    ('价税合计（大写）', '贰拾玖圆陆角', 'invoice_total_words'),
    ('开票人：', '合成甲', 'issuer'),
])
def test_footer_requires_unique_same_baseline_right_value(label: str, value: str, kind: str) -> None:
    marker = word(label, 20)
    valid = word(value, marker.right - 1.3, 101.5)
    result = _native_metadata([marker, valid, word('额外安装费', 300, 180)], 2, 'a' * 64)
    assert len(result) == 1
    assert result[0]['kind'] == kind and result[0]['value'] == value
    assert result[0]['page'] == 2 and result[0]['sha256'] == 'a' * 64
    assert result[0]['method'] == 'native_geometry' and value in result[0]['quote']
    assert not _native_metadata([valid], 2, 'a' * 64)
    assert not _native_metadata([marker, valid, word(label, 500)], 2, 'a' * 64)
    assert not _native_metadata([marker, word(value, marker.right, 140)], 2, 'a' * 64)
    assert not _native_metadata([marker, word(value, 0)], 2, 'a' * 64)


def test_two_possible_issuers_are_not_discarded_as_metadata() -> None:
    assert not _native_metadata([word('开票人：', 20), word('合成甲', 70), word('合成乙', 110)], 1, 'a' * 64)


def test_native_pdf_metadata_bound_to_source_without_hiding_unlabelled_tail(tmp_path: Path) -> None:
    path = tmp_path / 'synthetic.pdf'
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=400)
        # The stream deliberately places labels before unrelated table text.
        for xy, text in [((40, 280), '价税合计（大写）'), ((40, 370), '开票人：'),
                         ((20, 140), '*软饮料*饮用水'), ((20, 160), '额外安装费'),
                         ((180, 278.2), '贰拾玖圆陆角'), ((72, 370.5), '合成甲')]:
            page.insert_text(xy, text, fontname='china-s', fontsize=8)
        document.save(path)
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    facts = material_facts({'id': 'case', 'digest': digest, 'revision': 0,
                            'path': str(path), 'role': 'invoice', 'text': '', 'fields': {}})
    assert {(row['kind'], row['value']) for row in facts['native_metadata']} == {
        ('invoice_total_words', '贰拾玖圆陆角'), ('issuer', '合成甲')}
    assert all(row['sha256'] == digest for row in facts['native_metadata'])
    assert '额外安装费' in facts['source_text']
    assert not any('额外安装费' in row['quote'] for row in facts['native_metadata'])
    assert path.read_bytes() == original
