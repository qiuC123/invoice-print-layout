"""Offline recognition evaluation against an explicitly copied, read-only oracle."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any
import unicodedata
import uuid

FIELDS = ('amount', 'invoice_number', 'merchant', 'category', 'expense_date')
MARKER = '.recognition-eval-copy.json'


def copy_workspace(source: Path, target: Path) -> None:
    source, target = source.resolve(), target.resolve()
    database = source / '工作台' / 'expenses.sqlite3'
    if target == source or target.exists() or not database.is_file():
        raise ValueError('请指定存在的来源工作区和全新的副本目录')
    target_db = target / '工作台' / 'expenses.sqlite3'
    target_db.parent.mkdir(parents=True)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target_db) as dst:
        src.backup(dst)
        for key, name, digest in dst.execute('SELECT id,path,digest FROM attachments').fetchall():
            original = Path(name)
            file = target / 'originals' / (digest + original.suffix.lower())
            file.parent.mkdir(exist_ok=True)
            if original.is_file():
                shutil.copyfile(original, file)
                if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                    raise ValueError('来源材料与台账摘要不符，副本未用于评估')
            dst.execute('UPDATE attachments SET path=? WHERE id=?', (str(file), key))
    (target / MARKER).write_text(json.dumps({'source': str(source), 'offline': True}), encoding='utf-8')


def normal(value: Any) -> str:
    return ''.join(unicodedata.normalize('NFKC', str(value or '')).split()).casefold()


def score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row['type']].append(row)
    result: dict[str, Any] = {}
    for kind, samples in groups.items():
        fields = {}
        for key in FIELDS:
            eligible = [row for row in samples if row['gold'].get(key) not in ('', None)]
            predicted = [row for row in eligible if row['predicted'].get(key) not in ('', None)]
            correct = sum(normal(row['gold'][key]) == normal(row['predicted'][key]) for row in predicted)
            fields[key] = {'eligible': len(eligible), 'extracted': len(predicted), 'correct': correct,
                           'accuracy': correct / len(predicted) if predicted else None,
                           'coverage': len(predicted) / len(eligible) if eligible else None}
        result[kind] = {'samples': len(samples), 'fields': fields}
    return result


def evaluate(workspace: Path) -> dict[str, Any]:
    from invoice_print_layout.workbench import ExpenseStore
    from invoice_print_layout.intake_queue import IntakeQueue
    from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
    from invoice_print_layout.receipt_intake import configure_ocr_cache
    workspace = workspace.resolve()
    marker = workspace / MARKER
    if not marker.is_file() or Path(json.loads(marker.read_text(encoding='utf-8'))['source']).resolve() == workspace:
        raise ValueError('只允许评估由copy_workspace创建的工作区副本')
    database = workspace / '工作台' / 'expenses.sqlite3'
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        expenses = [json.loads(row[0]) for row in db.execute('SELECT data FROM expenses')]
        files = [dict(row) for row in db.execute('SELECT * FROM attachments ORDER BY rowid')]
    counts = Counter(row['item_id'] for row in files)
    digest_counts = Counter(row['digest'] for row in files)
    oracle = {item['id']: item for item in expenses if item.get('verified') and not item.get('deleted')
              and not item.get('amount_pending') and counts[item['id']] == 1}
    shadow = workspace / 'analysis' / uuid.uuid4().hex
    queue = IntakeQueue(ExpenseStore(shadow), ai=LocalOnlyInvoiceAI())
    if (workspace / '工作台' / 'company-profile.json').is_file():
        shutil.copyfile(workspace / '工作台' / 'company-profile.json', queue.store.root / 'company-profile.json')
    configure_ocr_cache(workspace / 'ocr-cache.sqlite3')
    rows = []
    for file in files:
        if file['item_id'] not in oracle or digest_counts[file['digest']] != 1 or file['role'] == 'package':
            continue
        item, path = oracle[file['item_id']], Path(file['path']).resolve()
        if not path.is_relative_to(workspace):
            raise ValueError('副本仍引用外部原件，请重新创建副本')
        numbers = set(item.get('invoice_numbers') or []) | ({item['invoice_number']} if item.get('invoice_number') else set())
        gold = {'amount': f"{item['amount_cents'] / 100:.2f}", 'invoice_number': next(iter(numbers)) if len(numbers) == 1 else '',
                'merchant': item.get('merchant', ''), 'category': item.get('category', ''),
                'expense_date': item.get('expense_date', '')}
        started = time.perf_counter()
        kind = ('screenshot' if path.suffix.lower() != '.pdf' else 'trip' if file['role'] == 'trip'
                else 'pdf_invoice' if file['role'] == 'invoice' else 'pdf_other')
        try:
            entry = queue.receive(path.name, path.read_bytes())
            parsed = queue._analyze_batch_pdf(entry) if path.suffix.lower() == '.pdf' else queue._analyze_batch_image(entry)
            prediction = dict(parsed['fields'])
            if prediction.get('category') == '其他':
                prediction['category'] = ''  # Unknown is an abstention, not a category prediction.
            if kind == 'pdf_other' and parsed['role'] == 'trip':
                kind = 'trip'
            row = {'id': item['id'], 'digest': file['digest'], 'type': kind, 'gold': gold, 'predicted': prediction,
                   'qr_check': parsed.get('qr_check'), 'company_check': parsed.get('company_check'),
                   'category_rule': parsed.get('local_evidence', {}).get('category'), 'error': ''}
        except (ValueError, OSError, RuntimeError) as exc:
            row = {'id': item['id'], 'digest': file['digest'], 'type': kind,
                   'gold': gold, 'predicted': {}, 'error': type(exc).__name__}
        row['milliseconds'] = (time.perf_counter() - started) * 1000
        rows.append(row)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    return {'samples': len(rows), 'metrics': score(rows), 'rows': rows, 'oracle_unchanged': True, 'network': False}
