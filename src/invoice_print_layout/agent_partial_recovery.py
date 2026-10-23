"""Resume an owned hotel draft after a durable, source-bound relation answer.

This is a payment-completion operation on one existing expense, never a general
supplement, a second expense, or permission to bypass material contradictions.
"""
from __future__ import annotations

import copy
import json
import sqlite3
from typing import TYPE_CHECKING, Any
import uuid

from invoice_print_layout.agent_combinations import invoice_complete, selection, source_category
from invoice_print_layout.agent_evidence import material_facts
from invoice_print_layout.agent_jobs import digest, encoded
from invoice_print_layout.agent_relations import candidates
from invoice_print_layout.reimbursement_people import common_person
from invoice_print_layout.workbench import now

if TYPE_CHECKING:
    from invoice_print_layout.agent_tools import InvoiceTools


class PartialRecovery:
    def __init__(self, tools: InvoiceTools):
        self.tools, self.jobs, self.store = tools, tools.jobs, tools.store
        self.matching = tools.matching

    def _plan(self, job: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any]:
        effects = [json.loads(r['data']) for r in db.execute(
            'SELECT data FROM invoice_agent_effects WHERE job_id=?', (job['id'],))]
        owned = {e['item_id'] for e in effects if e.get('item_id')}
        if len(owned) != 1 or owned != set(job.get('item_ids', [])):
            raise ValueError('只能补全当前任务自己创建的一笔住宿草稿')
        item_id = next(iter(owned))
        raw, item, invoice_sources, content_binding = self.tools.content._load(job, item_id)
        if (item.get('deleted') or item.get('merged_into') or item.get('amount_pending') or
                item.get('category') != '酒店' or item.get('material_basis') != 'standard' or
                item.get('stage') != 'draft' or item.get('verified') or item.get('amount_cents', 0) <= 0):
            raise ValueError('只能补全项目未变、未核对且金额明确的标准住宿草稿')
        if len(invoice_sources) != 1 or not invoice_complete(invoice_sources[0]) or source_category(invoice_sources[0]) != '酒店':
            raise ValueError('原草稿必须只有一份完整的住宿发票')
        invoice = invoice_sources[0]
        match_id = (item.get('material_match') or {}).get('group_id')
        row = db.execute('SELECT data FROM material_matches WHERE id=?', (match_id,)).fetchone()
        group = json.loads(row['data']) if row else {}
        source = db.execute('SELECT source FROM expenses WHERE id=?', (item_id,)).fetchone()
        if (group.get('schema_version') != 3 or group.get('mode') != 'registration' or
                group.get('project_id') != job['project_id'] or group.get('item_id') != item_id or
                group.get('status') != 'provisional' or not group.get('provisional') or
                group.get('voucher_keys') != ['q:' + invoice['id']] or
                group.get('payment_ids') or group.get('payment_count') or group.get('support_ids') or
                group.get('member_before') or group.get('member_after') or group.get('reused_item') or
                source is None or source['source'] != 'material-create:' + str(match_id) or
                set(group.get('queue_before', {})) != {invoice['id']} or
                set(group.get('queue_after', {})) != {invoice['id']}):
            raise ValueError('原费用的单票登记组已变化，不能自动补全')
        state = self.matching._member_state(db, item_id)
        previous = group.get('aggregate_after') or {}
        created = [e for e in effects if e.get('item_id') == item_id and e.get('content_base')]
        if (len(created) != 1 or not previous.get('data') or
                created[0]['content_base'] != self.tools.content._base(previous['data'])):
            raise ValueError('缺少当前任务的原始草稿授权快照')
        # ContentTools already proves the current base and source binding. Only
        # its actual display/category outputs may differ from registration.
        content_fields = {'title', 'display_name', 'project_type', 'project_name', 'category',
                          'report_section', 'report_group', 'agent_content', 'updated_at'}
        def without_content(value: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in value.items() if k not in content_fields}
        if without_content(previous['data']) != without_content(item):
            raise ValueError('住宿草稿包含后续改动，不能覆盖')
        guarded = copy.deepcopy(group)
        guarded['aggregate_after']['data'] = item
        self.matching._assert_group_snapshot(db, guarded)
        self.matching._check_member_originals(state)
        if (len(state['attachments']) != 1 or state['attachments'][0]['role'] != 'invoice' or
                state['attachments'][0]['digest'] != invoice['digest'] or
                invoice.get('material_match_id') != match_id or invoice.get('status') != 'linked' or
                invoice.get('project_id') != job['project_id']):
            raise ValueError('原住宿发票关联或原件已变化')
        for key, entry in group['queue_before'].items():
            if (entry.get('item_id') or entry.get('revision') + 1 != group['queue_after'][key]['revision'] or
                    entry.get('digest') != invoice['digest']):
                raise ValueError('原住宿发票登记快照无效')

        task = [self.matching._queue(db, key) for key in job['material_ids']]
        payments = [e for e in task if e['id'] != invoice['id']]
        if not payments or any(e.get('role') != 'payment' for e in payments):
            raise ValueError('住宿补全只接受本任务的成功付款原件')
        for entry in payments:
            self.tools._entry(job, entry['id'])
            self.matching._rules_material_state(self.matching._queue_card(entry), job['project_id'],
                                                db=db, allow_name_review=True)
        common_person([item, *payments])
        self.matching._check_transaction_selection(db, [invoice, *payments], {item_id})
        pool = [json.loads(r['data']) for r in db.execute('SELECT data FROM intake_queue')]
        pool = [e for e in pool if e.get('project_id') == job['project_id'] and
                e.get('status') in {'received', 'review'} and not e.get('item_id')]
        entries = [*pool, invoice]
        facts = {e['id']: material_facts(e, cache_root=self.store.root) for e in entries}
        states: dict[str, dict[str, Any]] = {invoice['id']: {'name_conflict': False}}
        for entry in pool:
            try:
                states[entry['id']] = self.matching._rules_material_state(
                    self.matching._queue_card(entry), job['project_id'], db=db, allow_name_review=True)
            except (ValueError, TypeError):
                continue  # The full pool still audits these blocked competitors.
        rows = candidates(entries, states, facts, scope=set(job['material_ids']))
        expected = set(job['material_ids'])
        full = [r for r in rows if selection(r) == expected and not r.get('competing') and
                r.get('voucher_keys') == ['q:' + invoice['id']] and
                set(r.get('payment_ids', [])) == {e['id'] for e in payments} and not r.get('support_ids') and
                r.get('category') == '酒店' and r.get('amount_cents') == item['amount_cents']]
        if len(full) != 1:
            raise ValueError('付款并非唯一、完整且金额守恒的住宿候选，请在人工工作台检查')
        candidate = full[0]
        snapshot = self.tools.fingerprint(job)
        refs = [{'id': e['id'], 'revision': e['revision'], 'digest': e['digest']} for e in sorted(task, key=lambda e: e['id'])]
        plan = {**candidate, 'type': 'complete_partial_expense', 'item_id': item_id, 'match_id': match_id,
                'fingerprint': snapshot, 'expense_version': digest(state), 'expense_raw': digest(raw),
                'group_version': digest(group), 'content_binding': content_binding, 'material_refs': refs}
        plan['id'] = digest(plan)[:32]
        return {'plan': plan, 'item': item, 'group': group, 'payments': payments, 'invoice': invoice, 'facts': facts}

    def _question(self, prepared: dict[str, Any]) -> dict[str, Any]:
        plan, facts = prepared['plan'], prepared['facts']
        choice = {'id': 'confirm:' + plan['id'], 'label': '四笔均对应且同额付款独立，补全原费用并继续',
                  'action': 'confirm_relation', 'claim': 'same_expense', 'proposal': plan,
                  **{k: plan[k] for k in ('voucher_keys', 'payment_ids', 'support_ids', 'amount_cents')}}
        # The wording stays true for other split counts as well as the four-part
        # hotel case. Equal-valued source files are not declared duplicates.
        choice['label'] = '这些付款均对应且同额付款独立，补全原费用并继续'
        choices = [choice, {'id': 'not-same', 'label': '不是同一笔，保留原费用与材料', 'action': 'not_same_expense'}]
        descriptions = []
        for ref in plan['material_refs']:
            fact = facts[ref['id']]
            actors = fact.get('actors') or {}
            merchant = next((str(actors[k].get('value') or '') for k in ('seller', 'shop', 'collector')
                             if isinstance(actors.get(k), dict) and actors[k].get('value')), '')
            descriptions.append({'id': ref['id'], 'amount_cents': fact['total_cents'], 'merchant': merchant,
                                 'goods': '、'.join(str(g.get('name') or '') for g in fact.get('goods', [])) or
                                          '、'.join(str(d.get('value') or '') for d in fact.get('descriptions', []))})
        question = {'type': 'material_relation',
                    'text': '请确认这些付款全部对应这张住宿发票，且同额付款是独立交易。确认后补全原费用；金额不重复计入，仍需人工核对。',
                    'snapshot': plan['fingerprint'], 'material_ids': [r['id'] for r in plan['material_refs']],
                    'material_refs': plan['material_refs'], 'material_facts': descriptions,
                    'evidence_summary': plan['anchors'], 'choices': choices}
        question['id'] = digest([question['snapshot'], question['type'], question['material_refs'], choices])[:32]
        return question

    def question(self, job: dict[str, Any]) -> dict[str, Any]:
        """Read only: the caller persists this new question through Jobs."""
        with self.store.connect() as db:
            return self._question(self._plan(job, db))

    def complete(self, job: dict[str, Any]) -> dict[str, Any]:
        approval = job.get('approval') or {}
        selected = approval.get('choice_data') or {}
        plan = selected.get('proposal') or {}
        if selected.get('action') != 'confirm_relation' or plan.get('type') != 'complete_partial_expense':
            raise ValueError('缺少原费用补全的用户确认')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record = db.execute('SELECT data FROM invoice_agent_actions WHERE job_id=? AND key=?',
                                (job['id'], 'user-confirmation:' + str(approval.get('question_id')))).fetchone()
            receipt = json.loads(record['data']) if record else {}
            if (receipt.get('actor') != 'user' or receipt.get('action') != 'confirm_relation' or
                    receipt.get('question_id') != approval.get('question_id') or receipt.get('choice') != approval.get('choice') or
                    receipt.get('snapshot') != approval.get('snapshot') or receipt.get('choice_data') != selected or
                    receipt.get('material_refs') != plan.get('material_refs')):
                raise ValueError('必须先通过正式问题入口持久记录原件确认')
            effect_key = 'partial-expense:' + str(plan.get('id'))
            previous = db.execute('SELECT data FROM invoice_agent_effects WHERE job_id=? AND key=?',
                                  (job['id'], effect_key)).fetchone()
            if previous:
                result: dict[str, Any] = json.loads(previous['data'])
                return result
            self.tools._active(db, job)
            live = json.loads(db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (job['id'],)).fetchone()['data'])
            if live.get('approval') != approval or live.get('project_id') != job['project_id']:
                raise ValueError('当前任务确认已变化')
            prepared = self._plan(job, db)
            current_question = self._question(prepared)
            if (current_question['id'] != approval.get('question_id') or
                    prepared['plan'] != plan or current_question['snapshot'] != receipt['snapshot']):
                raise ValueError('确认后费用、材料或竞争池已变化，请重新确认')
            item, group, payments = prepared['item'], prepared['group'], prepared['payments']
            item_id, match_id = item['id'], group['id']
            metadata = self.matching._check_transaction_selection(db, [prepared['invoice'], *payments], {item_id})
            records, added = [], []
            for entry in payments:
                self.matching._check_entry(db, entry, job['project_id'],
                                           {r['id']: r['revision'] for r in plan['material_refs']}, {'payment'})
                attachment_id = uuid.uuid4().hex
                db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)', (attachment_id, item_id,
                           entry['name'], 'payment', entry['path'], entry['digest']))
                added.append(attachment_id)
                group['queue_before'][entry['id']] = dict(entry)
                linked = {**entry, 'status': 'linked', 'item_id': item_id, 'material_match_id': match_id,
                          'revision': entry['revision'] + 1, 'issues': [],
                          'result': '用户确认付款对应原住宿费用；金额不重复计入，未核验或提交'}
                self.matching._write_queue(db, linked)
                records.append({'key': 'q:' + entry['id'], 'queue_id': entry['id'], 'role': 'payment',
                                'amount_cents': prepared['facts'][entry['id']]['total_cents'],
                                **{k: entry.get('fields', {}).get(k, '') for k in ('title', 'category', 'expense_date', 'merchant')}})
            total = item['amount_cents']
            recognition = {**group.get('recognition', {}), 'payments': records,
                           'payment_total_cents': total, 'difference_cents': 0}
            group.update(payment_ids=plan['payment_ids'], payment_count=len(payments), payment_total_cents=total,
                         payment_amounts={r['queue_id']: r['amount_cents'] for r in records},
                         attachment_ids=[*group['attachment_ids'], *added], alignment_status='balanced', recognition=recognition)
            item.update(metadata, updated_at=now())
            item['material_match'].update(payment_count=len(payments), payment_total_cents=total,
                                          alignment_status='balanced', recognition=recognition)
            item['agent_relation'] = {'relation_kind': 'user_confirmed', 'confirmation_id': receipt['question_id'],
                                      'anchors': plan['anchors']}
            attachments = [dict(r) for r in db.execute(
                'SELECT path,role,digest FROM attachments WHERE item_id=? ORDER BY id', (item_id,))]
            sources = [self.matching._queue(db, key) for key in job['material_ids']]
            # Author a fresh, real source binding. Existing extraction facts are
            # not silently relabelled as covering the newly attached payments.
            item['agent_content'] = {'status': 'pending', 'job_id': job['id'],
                                     'binding': self.tools.content._source_binding(attachments, sources, db=db),
                                     'base': self.tools.content._base(item),
                                     'recovery_confirmation_id': receipt['question_id']}
            db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item_id, now(), '按用户确认补全住宿付款材料，保留原费用及金额；未核验或提交'))
            group.update(aggregate_after=self.matching._member_state(db, item_id),
                         queue_after={key: self.matching._queue(db, key) for key in group['queue_before']},
                         supplemented_at=now(), recovery_confirmation_id=receipt['question_id'])
            db.execute('UPDATE material_matches SET data=? WHERE id=?', (encoded(group), match_id))
            result = {'item_id': item_id, 'completion_id': effect_key,
                      'revisions': {e['id']: e['revision'] for e in sources},
                      'proof': '已按当前原件的用户确认补全原住宿费用；金额、类别及未核对状态保留'}
            db.execute('INSERT INTO invoice_agent_effects VALUES (?,?,?)', (job['id'], effect_key, encoded(result)))
        return result
