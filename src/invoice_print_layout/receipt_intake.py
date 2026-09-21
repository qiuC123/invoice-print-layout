"""Local receipt OCR and conservative, editable suggestions. No cloud calls."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from rapidocr_onnxruntime import RapidOCR

from invoice_print_layout.takeout import OcrLine, _ocr_lines, TakeoutError
from invoice_print_layout.workbench import ExpenseStore, MAX_BYTES, ROLES, amount_cents, now

_engine: Any = None
_ocr_lock = threading.Lock()


def validate_image(name: str, payload: bytes) -> str:
    suffix = Path(name).suffix.lower()
    formats = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP'}
    if suffix not in formats or not payload or len(payload) > MAX_BYTES:
        raise ValueError('识别入口支持20MB以内的PNG/JPG/WebP截图；PDF请在已有事项中上传')
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != formats[suffix] or image.width * image.height > 25_000_000:
                raise ValueError('图片格式不符或像素超过2500万，请换一张清晰截图')
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('请上传静态截图')
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('图片损坏或无法读取，请重新选择原图') from exc
    return suffix


def read_receipt(payload: bytes, suffix: str) -> list[OcrLine]:
    global _engine
    # A lazy, bounded CPU engine; originals never leave this computer.
    with _ocr_lock, tempfile.TemporaryDirectory(prefix='receipt-ocr-') as folder:
        path = Path(folder) / ('receipt' + suffix)
        path.write_bytes(payload)
        if _engine is None:
            _engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        return _ocr_lines(path, _engine)


def rows_of(lines: list[OcrLine]) -> list[str]:
    """Join OCR boxes on the same baseline, e.g. order label + number."""
    rows: list[list[OcrLine]] = []
    for line in sorted(lines, key=lambda x: (x.top + x.bottom, x.left)):
        if rows:
            anchor = rows[-1][0]
            tolerance = max(5, min(line.bottom-line.top, anchor.bottom-anchor.top) * .55)
            if abs((line.top+line.bottom-anchor.top-anchor.bottom)/2) <= tolerance:
                rows[-1].append(line)
                continue
        rows.append([line])
    return [' '.join(x.text for x in sorted(row, key=lambda x: x.left)) for row in rows]


def parse_receipt(lines: list[OcrLine]) -> dict[str, Any]:
    rows = rows_of(lines)
    compact = [re.sub(r'\s+', '', row).replace('￥', '¥') for row in rows]
    text = '\n'.join(compact)
    fields = {'title': '待核对购买凭证', 'merchant': '', 'category': '其他',
              'amount': '', 'expense_date': '', 'order_number': '', 'note': ''}
    warnings: list[str] = []
    evidence: dict[str, str] = {}
    role = 'other'
    purchase_start: int | None = None

    # Keep payment/invoice screenshots out of the purchase-details role.
    if '发票号码' in text and ('电子发票' in text or '增值税' in text):
        role = 'invoice'
        warnings.append('这是发票图片，建议关联已有事项；开票日期不是消费日期。')
    elif any(word in text for word in ('交易单号', '微信支付', '支付宝', '支付成功')) and not any(word in text for word in ('价格明细', '订单已送达', '商品明细')):
        role = 'payment'
        warnings.append('这是支付凭证，不能替代购买明细；建议关联已有事项。')
    else:
        shops = [row for row in compact if re.match(r'^(?:闪购|商家[:：]|店铺[:：])', row)]
        for row in shops:
            candidate = re.sub(r'^(?:闪购|商家[:：]|店铺[:：])', '', row)
            candidate = re.split(r'蜂鸟|联系商家|进入店铺|[>›]', candidate)[0].strip()
            if candidate and '订单' not in candidate:
                fields['merchant'] = candidate
                evidence['merchant'] = row
                break
        # Classify the purchased section, not the advertising or recipient address.
        start = next((i for i, row in enumerate(compact) if fields['merchant'] and fields['merchant'] in row), None)
        purchase_start = start
        section = compact[start:] if start is not None else []
        stop = next((i for i, row in enumerate(section) if row.startswith(('价格明细', '收货地址', '订单号', '发票'))), len(section))
        goods = '\n'.join(section[:stop])
        materials = ('口罩', '胶带', '螺丝', '电线', '插座', '五金', '纸巾', '手套', '工具', '垃圾袋', '文具')
        food = ('咖啡', '蜜雪冰城', '奶茶', '饮品', '餐厅', '快餐', '汉堡', '面馆', '饭店')
        if any(word in goods for word in materials):
            fields['category'], role = '材料采购', 'purchase'
            evidence['category'] = '商品区：' + '、'.join(word for word in materials if word in goods)
        elif any(word in goods for word in food):
            fields['category'], role = '外卖', 'order'
            evidence['category'] = '商家／商品区：' + '、'.join(word for word in food if word in goods)
        elif start is not None:
            role = 'purchase'
        if fields['merchant']:
            fields['title'] = fields['merchant'] + (' · 材料采购' if fields['category'] == '材料采购' else ' · 订单')

    # Explicit order labels only: phone numbers and invoice IDs are not order IDs.
    orders: set[str] = set()
    for i, row in enumerate(compact):
        if '订单号' not in row and '订单编号' not in row:
            continue
        after = re.split(r'订单(?:编号|号)[:：]?', row, maxsplit=1)[1]
        if not after and i + 1 < len(compact):
            after = compact[i+1]
        match = re.match(r'(\d{12,30})(?!\d)', after)
        if match:
            orders.add(match.group(1))
            evidence['order_number'] = row + ' ' + match.group(1)
    if len(orders) == 1:
        fields['order_number'] = orders.pop()
    elif orders:
        warnings.append('发现多个订单号，请选择单个订单截图或手动核对。')

    # Rank labelled totals above item prices. Never add every visible price.
    amounts: list[tuple[int, str, str]] = []
    for row in compact[purchase_start:] if purchase_start is not None else compact:
        for match in re.finditer(r'(实付款|实付金额|实际付款|实付|合计|小写)[）)]?[:：]?¥?(\d+(?:\.\d{1,2})?)(?![\d.])', row):
            label, number = match.groups()
            priority = 3 if label in {'实付款', '实付金额', '实际付款'} else 2 if label == '实付' and ('价格明细' in row or '总优惠' in row) else 1
            if role == 'invoice' and label == '小写':
                priority = 4
            amounts.append((priority, number, row))
    if amounts:
        best = max(x[0] for x in amounts)
        choices = {f'{amount_cents(x[1])/100:.2f}' for x in amounts if x[0] == best}
        if len(choices) == 1:
            fields['amount'] = choices.pop()
            evidence['amount'] = next(x[2] for x in amounts if x[0] == best)
        else:
            warnings.append('多个金额无法确定总实付，已留空，请核对。')
    dates: set[str] = set()
    for row in rows:
        match = re.search(r'(?:下单时间|下单日期|支付时间|消费日期)[:：]?\s*(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})(?!\d)', row)
        if match:
            try:
                dates.add(date(*(int(x) for x in match.groups())).isoformat())
                evidence['expense_date'] = row
            except ValueError:
                pass
    if len(dates) == 1:
        fields['expense_date'] = dates.pop()
    for key, label in [('amount', '总实付金额'), ('expense_date', '消费日期'), ('order_number', '订单号'), ('merchant', '商家')]:
        if not fields[key]:
            warnings.append(label + '未能确定，已留空，可手动补充。')
    if fields['category'] == '其他':
        warnings.append('类别未能确定，请选择费用类别和凭证用途。')
    fields['note'] = '本地OCR预填，待核对原图。' + (' 分类依据：' + evidence['category'] if 'category' in evidence else '')
    return {'fields': fields, 'role': role, 'warnings': warnings, 'evidence': evidence,
            'text': '\n'.join(rows)[:12000]}


def find_duplicates(store: ExpenseStore, digest: str, order_number: str = '') -> list[dict[str, Any]]:
    with store.connect() as db:
        return _duplicates(db, digest, order_number)


def _duplicates(db: Any, digest: str, order_number: str) -> list[dict[str, Any]]:
    file_ids = {r['item_id'] for r in db.execute('SELECT item_id FROM attachments WHERE digest=?', (digest,))}
    results = []
    for row in db.execute('SELECT id,data FROM expenses'):
        data = json.loads(row['data'])
        same_order = bool(order_number and data.get('order_number') == order_number)
        if row['id'] in file_ids or same_order:
            results.append({'id': row['id'], 'title': data['title'], 'stage': data['stage'],
                            'reason': '相同文件' if row['id'] in file_ids else '相同订单号'})
    return results


def recognize(store: ExpenseStore, name: str, payload: bytes) -> dict[str, Any]:
    suffix = validate_image(name, payload)
    digest = hashlib.sha256(payload).hexdigest()
    duplicates = find_duplicates(store, digest)
    if duplicates:
        return {'duplicates': duplicates, 'fields': None, 'warnings': ['此凭证已在台账中，无需重复登记。']}
    try:
        result = parse_receipt(read_receipt(payload, suffix))
    except TakeoutError:
        result = parse_receipt([])
        result['warnings'].insert(0, '本地OCR未读出文字，可以对照原图手动填写后保存。')
    result['duplicates'] = find_duplicates(store, digest, result['fields']['order_number'])
    return result


def save_receipt(store: ExpenseStore, name: str, payload: bytes, values: dict[str, Any], role: str) -> dict[str, Any]:
    """Create the matter + attachment in one transaction, or return duplicates."""
    suffix = validate_image(name, payload)
    if role not in ROLES or role == 'package':
        raise ValueError('凭证用途无效')
    if values.get('amount') in (None, ''):
        raise ValueError('请核对并填写支出金额，未知金额不能按0元登记')
    data = store._validate({**values, 'expense_date': values.get('expense_date', '')})
    digest = hashlib.sha256(payload).hexdigest()
    target = store.files / (digest + suffix)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        duplicates = _duplicates(db, digest, data['order_number'])
        if duplicates:
            return {'duplicates': duplicates}
        # Store bytes before committing any record: failures cannot leave an empty matter.
        if not target.exists():
            temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        data.update(id=uuid.uuid4().hex[:10], created_at=now(), updated_at=now(), stage='draft', verified=False)
        db.execute('INSERT INTO expenses VALUES (?,?,?)', (data['id'], 'receipt:' + digest, json.dumps(data, ensure_ascii=False)))
        db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)',
                   (uuid.uuid4().hex, data['id'], Path(name.replace('\\', '/')).name, role, str(target), digest))
        db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                   (data['id'], now(), '上传凭证并登记；识别字段待核对'))
    return {'item': store.get(data['id']), 'duplicates': []}
