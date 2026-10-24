"""Local, conservative mail pairing. Original PDFs remain untouched."""
from __future__ import annotations

import hashlib
from copy import copy
import json
import os
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from datetime import date
from pathlib import Path
from typing import Any

import pymupdf

from invoice_print_layout.documents import compact_text, inspect_pdf, PdfInspectionError
from invoice_print_layout.models import DocumentKind, PdfDocument, RideProvider
from invoice_print_layout.reliability import save_json
from invoice_print_layout.storage import read_history
from invoice_print_layout.workbench import ExpenseStore, now
from invoice_print_layout.file_cache import FileSignature, file_signature


@dataclass
class Material:
    document: PdfDocument
    key: str
    start: str = ''
    end: str = ''


@lru_cache(maxsize=512)
def _material_result(signature: FileSignature) -> Material | str:
    path = Path(signature[0])
    try:
        material = _parse_material(path)
    except (ValueError, OSError) as error:
        if file_signature(path) != signature:
            raise PdfInspectionError('材料在读取期间已变化') from error
        return str(error)
    if file_signature(path) != signature:
        raise PdfInspectionError('材料在读取期间已变化')
    return material


def read_material(path: Path) -> Material:
    signature = file_signature(path)
    result = _material_result(signature)
    if file_signature(path) != signature:
        raise PdfInspectionError('材料在读取期间已变化')
    if isinstance(result, str):
        raise PdfInspectionError(result)
    return copy(result)


def _parse_material(path: Path) -> Material:
    document = inspect_pdf(path)
    with pymupdf.open(path) as pdf:  # type: ignore[no-untyped-call]
        text = compact_text(''.join(page.get_text() for page in pdf))
    if document.kind == DocumentKind.INVOICE:
        return Material(document, 'invoice:' + str(document.invoice_number))
    # Re-exporting a trip sheet may change its application date, not the trips.
    text = re.sub(r'申请日期[:：]?\d{4}-\d{2}-\d{2}', '', text)
    key = 'trip:' + hashlib.sha256(text.encode()).hexdigest()
    match = re.search(r'行程起止日期[:：]?(\d{4}-\d{2}-\d{2})至(\d{4}-\d{2}-\d{2})', text)
    start, end = match.groups() if match else ('', '')
    if start:
        date.fromisoformat(start)
        date.fromisoformat(end)
        if start > end:
            raise PdfInspectionError('行程日期范围无效')
    return Material(document, key, start, end)


def organize_rides(store: ExpenseStore, *, folder: Path | None = None) -> dict[str, Any]:
    folder = folder or store.root / '邮件收件'
    folder.mkdir(exist_ok=True)
    result: dict[str, Any] = {'created': 0, 'supplemented': 0, 'duplicates': 0, 'issues': {}, 'items': []}
    mail: dict[str, set[tuple[str, str]]] = {}
    for row in read_history(store.workspace / 'mail_imports.jsonl'):
        if row.get('message_uid') and row.get('attachment_hash'):
            mail.setdefault(str(row['attachment_hash']), set()).add(
                (str(row.get('imported_at', '')), str(row['message_uid'])))
    with store.connect() as provenance_db:
        if provenance_db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone():
            for queued in provenance_db.execute('SELECT digest,data FROM intake_queue'):
                info = json.loads(queued['data'])
                if info.get('batch'):
                    mail.setdefault(queued['digest'], set()).add(('upload', info['batch']))

    def issue(path: Path, reason: str) -> None:
        result['issues'][hashlib.sha256(str(path).encode()).hexdigest()] = reason

    # PDF parsing happens before acquiring the write transaction. Re-read rows
    # and file identities under the lock; a changing source defers the scan.
    with store.connect() as source_db:
        source_rows = [tuple(row) for row in source_db.execute(
            "SELECT id,item_id,path,digest FROM attachments WHERE role IN ('invoice','trip')")]
    paths = sorted(folder.glob('*.pdf'))
    prepared: dict[Path, tuple[FileSignature | None, Material | None, str]] = {}
    for path in dict.fromkeys([Path(row[2]) for row in source_rows] + paths):
        signature = None
        material = None
        digest = ''
        try:
            signature = file_signature(path)
            try:
                material = read_material(path)
                digest = material.document.sha256
            except (ValueError, OSError):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if file_signature(path) != signature:
                material = None
                signature = None
        except (ValueError, OSError):
            pass
        prepared[path] = signature, material, digest

    def unchanged(path: Path) -> bool:
        if path not in prepared:
            return False
        try:
            return file_signature(path) == prepared[path][0]
        except OSError:
            return prepared[path][0] is None

    with store.connect() as db:
        # Serialize source lookup, expense creation, attachment links and receipts.
        db.execute('BEGIN IMMEDIATE')
        current_rows = [tuple(row) for row in db.execute(
            "SELECT id,item_id,path,digest FROM attachments WHERE role IN ('invoice','trip')")]
        if set(current_rows) != set(source_rows) or any(not unchanged(Path(row[2])) for row in current_rows):
            issue(folder, '台账材料在整理期间已变化，本轮未自动配对，请重新整理')
            save_json(store.root / 'automatic-rides.json', result)
            return result
        used = {r[0] for r in db.execute('SELECT digest FROM attachments UNION SELECT digest FROM automatic_ride_files')}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='expense_project_deleted_materials'").fetchone():
            used.update(row['digest'] for row in db.execute('SELECT digest FROM expense_project_deleted_materials'))
        # Keep archived originals on disk while excluding them from this legacy
        # directory organizer, including scans triggered by another material.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone():
            from invoice_print_layout.intake_archive import INACTIVE_STATUSES
            used.update(row['digest'] for row in db.execute('SELECT digest,data FROM intake_queue')
                        if json.loads(row['data']).get('status') in INACTIVE_STATUSES)
        owners: dict[str, set[str]] = {}
        materials: dict[str, Material] = {}
        variants: dict[str, list[Material]] = {}
        conflicts: set[str] = set()
        for _, item_id, source_path, _ in current_rows:
            material = prepared[Path(source_path)][1]
            if material is None:
                continue
            owners.setdefault(material.key, set()).add(item_id)
            materials[material.key] = material
        for path in sorted(folder.glob('*.pdf')):
            if not unchanged(path):
                issue(path, '材料在整理期间已变化，请重新整理')
                continue
            _, material, digest = prepared[path]
            if digest in used:
                continue
            if material is None:
                issue(path, '未识别为可自动整理的打车材料，请核对材料类型')
                continue
            prior = materials.get(material.key)
            if prior and (prior.document.amount != material.document.amount or
                          prior.document.provider != material.document.provider):
                conflicts.add(material.key)
            materials.setdefault(material.key, material)
            variants.setdefault(material.key, []).append(material)

        def compatible(trip: Material, invoice: Material) -> bool:
            t, i = trip.document, invoice.document
            if t.amount != i.amount or t.provider == RideProvider.UNKNOWN or t.provider != i.provider:
                return False
            if trip.end and i.invoice_date and trip.end > i.invoice_date.isoformat():
                return False
            # Require provenance in addition to amount and provider; filenames A/B are not evidence.
            t_sources = set().union(*(mail.get(m.document.sha256, set()) for m in variants.get(trip.key, [trip])))
            i_sources = set().union(*(mail.get(m.document.sha256, set()) for m in variants.get(invoice.key, [invoice])))
            return bool(t_sources & i_sources)

        handled: set[str] = set()

        def attach(item_id: str, keys: list[str]) -> bool:
            row = db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()
            data = json.loads(row['data'])
            if data.get('deleted'):
                for key in keys:
                    for material in variants.get(key, []):
                        issue(material.document.path, '对应费用已删除，请先恢复后补充材料；未重新记账')
                handled.update(keys)
                return False
            changed = False
            for key in keys:
                for material in variants.get(key, []):
                    doc = material.document
                    existing = item_id in owners.get(key, set())
                    if data['stage'] != 'draft' and not existing:
                        issue(doc.path, '对应费用已提交或已报销，需核对后补充材料')
                        continue
                    if not existing:
                        target = store.files / (doc.sha256 + '.pdf')
                        if not target.exists():
                            temp = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                            try:
                                payload = doc.path.read_bytes()
                                if hashlib.sha256(payload).hexdigest() != doc.sha256:
                                    raise ValueError('材料在整理期间已变化，本轮未自动配对')
                                temp.write_bytes(payload)
                                os.replace(temp, target)
                            finally:
                                temp.unlink(missing_ok=True)
                        db.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?)',
                                   (uuid.uuid4().hex, item_id, doc.path.name, doc.kind.value, str(target), doc.sha256))
                        owners.setdefault(key, set()).add(item_id)
                        changed = True
                    else:
                        result['duplicates'] += 1
                    db.execute('INSERT OR IGNORE INTO automatic_ride_files VALUES (?,?,?)',
                               (doc.sha256, item_id, key))
                handled.add(key)
            if changed:
                data.update(verified=False, updated_at=now())
                data.pop('review_requested', None)
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item_id))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                           (item_id, now(), '邮件打车材料自动配对归档；金额只计算一次，项目及用途待核对'))
            return changed

        # Match against all existing expense materials, including closed matters.
        for key in variants:
            if key in conflicts:
                continue
            targets = owners.get(key, set())
            if len(targets) == 1:
                if attach(next(iter(targets)), [key]):
                    result['supplemented'] += 1

        trips = [m for m in materials.values() if m.document.kind == DocumentKind.TRIP and m.key not in conflicts]
        invoices = [m for m in materials.values() if m.document.kind == DocumentKind.INVOICE and m.key not in conflicts]
        edges = [(t, i) for t in trips for i in invoices if compatible(t, i)
                 and (t.key in variants or i.key in variants)]
        for trip, invoice in edges:
            keys = [trip.key, invoice.key]
            if not any(k in variants and k not in handled for k in keys):
                continue
            if sum(t.key == trip.key for t, _ in edges) != 1 or sum(i.key == invoice.key for _, i in edges) != 1:
                continue
            targets = owners.get(trip.key, set()) | owners.get(invoice.key, set())
            if len(targets) > 1:
                continue
            if targets:
                item_id = next(iter(targets))
                data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
                if data['category'] != '打车' or data['amount_cents'] != int(invoice.document.amount * 100):
                    continue
                if attach(item_id, keys):
                    result['supplemented'] += 1
            else:
                amount = invoice.document.amount
                span = trip.start + ('至' + trip.end if trip.end != trip.start else '')
                data = store._validate({'category': '打车', 'title': f'{invoice.document.provider.value}打车 · {span or "行程日期待核对"} · {amount:.2f}元',
                                        'project': '待分配项目', 'merchant': invoice.document.provider.value,
                                        'amount': str(amount), 'expense_date': trip.start,
                                        'invoice_state': 'requested',
                                        'note': f'邮件自动整理，发票及行程单配对；行程期间：{span or "待核对"}。项目及用途待核对。'})
                item_id = uuid.uuid4().hex[:10]
                data.update(id=item_id, created_at=now(), updated_at=now(), stage='draft', verified=False)
                db.execute('INSERT INTO expenses VALUES (?,?,?)',
                           (item_id, 'ride-mail:' + str(invoice.document.invoice_number), json.dumps(data, ensure_ascii=False)))
                attach(item_id, keys)
                result['created'] += 1
            result['items'].append(item_id)
        for key, docs in variants.items():
            if key not in handled:
                for material in docs:
                    issue(material.document.path, '同一发票号码内容冲突，请核对' if key in conflicts else
                          '等待对应发票或行程单；金额、来源或配对关系不明确，需核对')
    save_json(store.root / 'automatic-rides.json', result)
    return result
