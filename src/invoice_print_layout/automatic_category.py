"""Automatic category-only updates, exact-content reuse and explicit human feedback."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Any, ContextManager, cast

from invoice_print_layout import expense_classifier as classifier
from invoice_print_layout.workbench import ExpenseStore, now


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class AutomaticCategory:
    def __init__(self, store: ExpenseStore, write_lock: ContextManager[Any]):
        self.store, self.write_lock = store, write_lock
        self.run_lock = threading.Lock()  # One OCR/model job, including across browser tabs.
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS category_attempts (
                    item_id TEXT PRIMARY KEY, input_key TEXT NOT NULL, evidence_key TEXT,
                    result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS category_experience (
                    evidence_key TEXT PRIMARY KEY, category TEXT NOT NULL, origin TEXT NOT NULL,
                    conflict INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS category_experience_disabled (evidence_key TEXT PRIMARY KEY);
            ''')

    def input_key(self, item: dict[str, Any]) -> str:
        return digest([item.get('note', ''), classifier.attachment_version(self.store, item['id']),
                       classifier.MODEL, classifier.CRITERIA, classifier.RULES])

    def record(self, item_id: str) -> dict[str, Any] | None:
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM category_attempts WHERE item_id=?', (item_id,)).fetchone()
        return dict(row) if row else None

    def eligible(self, item: dict[str, Any], record: dict[str, Any] | None) -> bool:
        if item['stage'] != 'draft' or item['verified']:
            return False
        previous = json.loads(record['result']) if record else {}
        return bool(item['category'] == '其他' or
                    (previous.get('status') == 'saved' and previous.get('category') == item['category']) or
                    previous.get('managed_category') == item['category'])

    def statuses(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        result = {}
        for item in items:
            record = self.record(item['id'])
            previous = json.loads(record['result']) if record else {}
            pending = self.eligible(item, record) and (not record or record['input_key'] != self.input_key(item))
            result[item['id']] = {**previous, 'pending': pending}
        return result

    def remember_attempt(self, item_id: str, input_key: str, evidence_key: str | None,
                         result: dict[str, Any]) -> None:
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO category_attempts VALUES (?,?,?,?)',
                       (item_id, input_key, evidence_key, json.dumps(result, ensure_ascii=False)))

    def feedback(self, before: dict[str, Any], after: dict[str, Any], *, verified: bool = False) -> None:
        """Called inside the write lock after explicit user save/verification, never after auto-save."""
        if before['category'] == after['category'] and not verified:
            return
        record = self.record(after['id'])
        key = self.input_key(after)
        category = after['category']
        if record and record['input_key'] == key and record['evidence_key'] and category != '其他':
            with self.store.connect() as db:
                old = db.execute('SELECT * FROM category_experience WHERE evidence_key=?',
                                 (record['evidence_key'],)).fetchone()
                conflict = bool(old and (old['conflict'] or
                                        (old['origin'] == 'human' and old['category'] != category)))
                db.execute('INSERT OR REPLACE INTO category_experience VALUES (?,?,?,?)',
                           (record['evidence_key'], category, 'human', int(conflict)))
        # Explicit category choices are retained, even when the user chooses Other.
        self.remember_attempt(after['id'], key, record['evidence_key'] if record and record['input_key'] == key else None,
                              {'status': 'manual', 'category': category, 'source': 'human',
                               'reason': '已保留你的分类；相同明细的确认经验会优先复用。', 'saved': True})

    def run(self, item_id: str, *, retry: bool = False) -> dict[str, Any]:
        if not self.run_lock.acquire(blocking=False):
            return {'status': 'busy', 'reason': '正在处理另一笔分类，请稍候。', 'saved': False}
        try:
            return self._run(item_id, retry)
        finally:
            self.run_lock.release()

    def save_category(self, expected: dict[str, Any], category: str, input_key: str) -> bool:
        # Compare and change only category inside one transaction, including cross-process writers.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.store.get(expected['id'])
            if current != expected or self.input_key(current) != input_key:
                return False
            row = db.execute('SELECT data FROM expenses WHERE id=?', (current['id'],)).fetchone()
            data = json.loads(row['data'])
            data.update(category=category, verified=False, legacy=False, updated_at=now())
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), current['id']))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (current['id'], now(), '自动分类已保存：'+category+'；材料和金额仍需核对'))
        return True

    def _run(self, item_id: str, retry: bool) -> dict[str, Any]:
        with self.write_lock:
            item = self.store.get(item_id)
            record = self.record(item_id)
            if not self.eligible(item, record):
                return {'status': 'skipped', 'reason': '已有人工分类、已核对或已提交，保持原记录。', 'saved': False}
            input_key = self.input_key(item)
            if record and record['input_key'] == input_key and not retry:
                return cast(dict[str, Any], json.loads(record['result']))
        evidence_key = None
        previous = json.loads(record['result']) if record else {}
        managed = previous.get('managed_category') or (previous.get('category') if previous.get('status') == 'saved' else None)
        try:
            evidence, warnings = classifier.collect(self.store, item)
            normalized = {k: re.sub(r'\s+', '', v).casefold() for k, v in evidence.items()}
            goods = re.sub(r'[*＊][^*＊]+[*＊]', '', normalized.get('goods', ''))
            specific = bool(normalized.get('purpose') or goods not in {'', '日用百货'})
            evidence_key = digest([normalized, classifier.MODEL, classifier.CRITERIA, classifier.RULES]) if specific else None
            with self.store.connect() as db:
                experience = db.execute('SELECT * FROM category_experience WHERE evidence_key=?',
                                        (evidence_key,)).fetchone()
                if db.execute('SELECT 1 FROM category_experience_disabled WHERE evidence_key=?', (evidence_key,)).fetchone():
                    experience = None
            from invoice_print_layout.expense_preferences import Preferences
            preferred = Preferences(self.store).apply(item, goods=evidence.get('goods', ''), purpose=evidence.get('purpose', ''),
                                                      receipt=any(a['role'] == 'purchase' for a in item['attachments']))
            if 'category' in preferred['conflicts']:
                answer = {'category': None, 'source': 'experience', 'reason': '可管理经验冲突，请确认或停用规则。'}
            elif 'category' in preferred['changes']:
                answer = {'category': preferred['changes']['category'], 'source': 'experience', 'reason': '复用明确商品及用途的用户规则。'}
            elif experience and experience['conflict']:
                answer = {'category': None, 'source': 'experience', 'reason': '相同内容存在不同人工分类，请补充用途或手动分类。'}
            elif experience:
                human = experience['origin'] == 'human'
                answer = {'category': experience['category'], 'source': 'experience' if human else 'cache',
                          'reason': '复用相同商品和用途的人工确认经验。' if human else '复用相同内容的分类缓存；未再次调用模型。'}
            else:
                answer = classifier.decide(evidence)
            result = {**answer, 'evidence': evidence, 'warnings': warnings, 'saved': False, 'status': 'needs_info'}
        except Exception:
            result = {'category': None, 'source': 'local', 'status': 'error', 'saved': False,
                      'reason': '自动分类暂未完成，可重试或手动分类。'}
        with self.write_lock:
            current = self.store.get(item_id)
            if current != item or self.input_key(current) != input_key:
                return {'status': 'stale', 'reason': '判断期间资料有变化，稍后按新资料重试。', 'saved': False}
            category = result.get('category')
            result['managed_category'] = managed
            if isinstance(category, str) and category in classifier.CATEGORY.values() and category != '其他' and not result.get('warnings'):
                if not self.save_category(current, category, input_key):
                    return {'status': 'stale', 'reason': '资料已变化，稍后重试。', 'saved': False}
                result.update(status='saved', saved=True, managed_category=category)
                if evidence_key:
                    with self.store.connect() as db:
                        db.execute('INSERT OR IGNORE INTO category_experience VALUES (?,?,?,0)',
                                   (evidence_key, category, 'cache'))
            elif result.get('warnings'):
                result['reason'] = '部分材料未能完整读取，保留原类别。请补充或手动选择。'
            self.remember_attempt(item_id, input_key, evidence_key, result)
        return result
