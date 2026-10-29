"""F14 invented PDFs match the four reviewed, external redacted span layouts.

No external JSON or original business document is loaded by these tests.
"""
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.agent_content import goods_from_text
from invoice_print_layout.agent_evidence import material_facts
from invoice_print_layout.agent_jobs import AgentLoop, AgentService, encoded
from invoice_print_layout.agent_product_rules import _source_products
from invoice_print_layout.agent_transport import pair_status
from tests.test_invoice_agent import environment, start
from tests.test_organize_recovery import flag_expense, save_queue


def put(page: Any, x: float, top: float, text: str, size: float = 9, width: float = 0) -> None:
    point = pymupdf.Point(x, top + size)
    scale = width / pymupdf.get_text_length(text, fontname='china-s', fontsize=size) if width else 1
    page.insert_text(point, text, fontname='china-s', fontsize=size,
                     morph=(point, pymupdf.Matrix(scale, 1)))


def ride_layout(environment: Any, provider: str, *, extra: str = '', malformed: bool = False,
                trip_provider: str = '', trip_amount: str = '', table_extra: bool = False) -> list[dict[str, Any]]:
    _, tools, project = environment
    didi = provider == 'didi'
    amount = '195.80' if didi else '17.34'
    seller = '上海滴滴畅行科技有限公司' if didi else '苏州市吉利优行电子科技有限公司'
    platform = '滴滴出行' if didi else '曹操出行'
    rows = []
    for role in ('invoice', 'trip'):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=595.3, height=(557.8 if didi else 396.9) if role == 'invoice' else 841.9)
            if role == 'invoice':
                put(page, 198, 22.7 if didi else 25.4, '电子发票（普通发票）', 20, 199.2)
                put(page, 94, 39.5 if didi else 41.3, '旅客运输服务')
                put(page, 440, 34.4, '发票号码:')
                put(page, 483.4, 35.6, '99999999999999999999', width=90)
                put(page, 440, 52.1, '开票日期:')
                put(page, 483.4, 53.2, '2030年01月02日', width=63)
                for x, label, value in [(17.5, '购买方信息', '某某某某'), (301.5, '销售方信息', seller)]:
                    for n, char in enumerate(label):
                        put(page, x, 88.4 + 10.8 * n, char)
                    put(page, x + 14, 92.8, '名称：')
                    put(page, x + 41, 93.9, value)
                    put(page, x + 14, 123.7, '统一社会信用代码/纳税人识别号：', width=139.5)
                    put(page, x + 154, 124.2, '99999999AA9AAAAA9A', 10, 108.1)
                header_y = 148 if didi else 151.1
                header_x = [76.6, 205.9, 297.4, 391.8, 452, 557.4] if didi else [77.1, 252.8, 337.8, 411.5, 443.1, 564.6]
                for x, label in zip(header_x, ['项目名称', '单价', '数量', '金额', '税率/征收率', '税额']):
                    put(page, x, header_y, label, width=49.3 if label == '税率/征收率' else 0)
                # Caocao's header/footer are emitted before its two tax rows in stream order.
                table_y = 223 if didi else 213.5
                table_x = [28.9, 93.1, 195.9, 289.7, 397.4, 479.9, 520.2] if didi else [24.8, 80.6, 176.3, 271.5, 384.8, 465.9, 510.3]
                def table_header() -> None:
                    for x, label in zip(table_x, ['出行人', '有效身份证件号', '出行日期', '出发地', '到达地', '等级', '交通工具类型']):
                        put(page, x, table_y, label)
                if not didi:
                    put(page, 55.5, 199.5, '合')
                    put(page, 98.6, 199.5, '计')
                    table_header()
                    put(page, 53.9, 279.4, '价税合计（大写）')
                    put(page, 409.6, 279.6, '（小写）', 8.5)
                    put(page, 17.5, 301.3, '备')
                    put(page, 17.5, 324.4, '注')
                    put(page, 53.9, 365.3, '开票人：', width=35.8)
                put(page, 13.5 if didi else 12.8, 161.7 if didi else 162.7,
                    '*交通运输服务*客运服' if didi else '*交通运输服务*客运服务费', width=90 if didi else 107.5)
                for x, value in zip([203.6, 306.4, 389.6, 472.3, 561.9] if didi else [230.4, 351.3, 407.1, 463.2, 564.6],
                                    ['190.10', '1', '190.10', '3%', '5.70'] if didi else ['20.242718', '1', '20.24', '3%', '0.61']):
                    put(page, x, 161.7 if didi else 162.7, value, width=len(value) * 4.5)
                put(page, 13.5 if didi else 12.8, 174.1 if didi else 175.1,
                    '务费' if didi else '*交通运输服务*客运服务费', width=18 if didi else 107.5)
                if not didi:
                    for x, value in [(407.1, '-3.41'), (463.2, '3%'), (560.1, '-0.10')]:
                        put(page, x, 175.1, value, width=len(value) * 4.5)
                if extra:
                    put(page, 13.5, 187, extra)
                if didi:
                    put(page, 55.5, 211.6, '合')
                    put(page, 98.6, 211.6, '计')
                put(page, 403.2, 212.8 if didi else 199.4, '¥190.10' if didi else '¥16.83', width=33)
                put(page, 550, 212.8 if didi else 199.4, '¥5.70' if didi else '¥0.51', width=27)
                if didi:
                    table_header()
                    # Seven passenger rows, two-line places, empty grade cells.
                    for n, y in enumerate([236.8, 261.6, 286.5, 311.3, 336.1, 360.9, 385.8]):
                        origin_x = [260.4, 258.2, 258.2, 258.2, 255.9, 258.2, 271.7][n]
                        destination_x = [370.4, 365.9, 365.9, 368.2, 365.9, 379.4, 365.9][n]
                        for x, value, width in [(33.4, '某某', 18), (84.1, '999999********9999', 81),
                                                (191.4, '无法识别' if malformed and n == 0 else '2030-01-01', 45),
                                                (origin_x, '某某|某+某某(某', [85.5, 90, 90, 90, 94.5, 90, 63][n]),
                                                (destination_x, 'AAAA某某999某', [81, 90, 90, 85.5, 90, 63, 90][n]),
                                                (533.7, '出租车', 27)]:
                            put(page, x, y, value, width=width)
                        put(page, [287.4, 298.7, 264.9, 264.9, 280.7, 264.9, 271.7][n], y + 12.4,
                            '某某某某)', width=[31.5, 9, 76.5, 76.5, 45, 76.5, 63][n])
                        put(page, [401.9, 372.7, 401.9, 406.4, 372.7, 379.4, 372.7][n], y + 12.4,
                            '某某99某', width=[18, 76.5, 18, 9, 76.5, 63, 76.5][n])
                    put(page, 36.3, 439.6, '价税合计（大写）')
                    put(page, 396.8, 439.6, '（小写）')
                if table_extra:
                    put(page, 25, 420 if didi else 240, '未知独立商品')
                put(page, 445, 442.1 if didi else 278, '¥' + amount, 11, 38)
                if didi:
                    put(page, 160.9, 441.1, '某某某某某某某某', width=80)
                    put(page, 36.2, 459.1, '某某:某某某某某某某  某某:99999999999', width=300)
                    put(page, 55.3, 526.7, '开票人：')
                    put(page, 94.3, 527.8, '某某')
                else:
                    put(page, 182.8, 278, '壹拾柒圆叁角肆分', width=80)
                    put(page, 33.3, 297.3, '购方地址：虚构路99号; 电话：0000-00000000;', width=304.6)
                    put(page, 33.3, 306.6, '购方开户行：虚构银行; 银行账号：99999999999999999;', width=389.7)
                    # The detached value is last in the stream, slightly overlapping its label.
                    put(page, 86.5, 365.3, '甲乙', width=17.9)
            else:
                platform = trip_provider or platform
                total = trip_amount or amount
                title = platform + '-行程单' if didi else '第三方网约车服务提供方' + platform + '—行程单'
                put(page, 214 if didi else 83, 190 if didi else 79, title, 22.5, 167.3 if didi else 429.3)
                base = 278.2 if didi else 115.9
                put(page, 58.5, base, '· 某某某某：9999-99-99', width=99.8)
                put(page, 292.2, base, '· 行程起止日期：2030-01-01 至 2030-01-01', width=182.1)
                put(page, 58.5, base + 13.9, '· 行程人手机号：99999999999', width=125.8)
                put(page, 292.2, base + 13.9, f'· 共{7 if didi else 1}笔行程， 合计{total}元', width=113.3)
                xs = [67.2, 93.8, 131.1, 180.2, 252.3, 369.9, 439.1, 479.9, 513] if didi else [69.6, 102.4, 150.2, 209.1, 255.7, 346.2, 421.7, 471.2, 510.6]
                for x, label in zip(xs, ['序号', '车型', '上车时间', '城市', '起点', '终点', '里程[公里]', '金额[元]', '备注']):
                    put(page, x, 336.4 if didi else 174, label, 7.5, 35 if '里程' in label else 0)
                amounts = ['44.50', '51.10', '16.00', '23.00', '25.90', '18.90', '16.40'] if didi else [total]
                for n, money in enumerate(amounts):
                    origin_x = [221.7, 227.7, 206, 206, 218.5, 206, 215.7][n] if didi else 243.2
                    destination_x = [343.7, 323.5, 342.2, 346, 323.5, 333.3, 323.5][n] if didi else 309.6
                    origin_width = [76.1, 64.2, 107.6, 107.6, 82.5, 107.6, 88.2][n] if didi else 40
                    destination_width = [67.2, 107.6, 70.2, 62.7, 107.6, 88.2, 107.6][n] if didi else 88.2
                    for x, value, width in zip([73, 89.3, 122.2, 178.7, origin_x, destination_x, 450.6, 485.9] if didi else [75.4, 97.9, 141.3, 207.6, origin_x, destination_x, 433.2, 477.2],
                                               [str(n + 1), '快车' if didi else platform, '99-99 99:99 某某', '某某某', '某某|某某(某某)', 'AAAA某某(某某某)', '3.52', money],
                                               [3.5, 24, 47.7, 18, origin_width, destination_width, 12, 15.5]):
                        put(page, x, (371.6 + n * 28.02) if didi else 209.3, value, 6, width)
                put(page, 502.2, 752.3 if didi else 777.4, '页码：1/1', 7.5, 34.5)
            payload, text = pdf.tobytes(), page.get_text()
        row = tools.intake.receive(f'{provider}-{role}.pdf', payload, project_id=project)
        save_queue(tools, row, status='review', role=role, revision=1, text=text,
                   fields={'amount': amount if role == 'invoice' else trip_amount or amount,
                           'category': '打车', 'merchant': seller if role == 'invoice' else platform})
        rows.append(row)
    return rows


def inspect_rows(tools: Any, rows: list[dict[str, Any]], *, legacy: bool = False) -> dict[str, Any]:
    goods = []
    for row in rows:
        fact = material_facts(row)
        source_goods = goods_from_text(row['text'], {}) if legacy else fact['goods']
        goods.extend({**g, 'material_id': row['id'], 'sha256': row['digest']} for g in source_goods)
    return tools.content._inspect_products({'goods': goods}, rows)


@pytest.mark.parametrize('provider', ['didi', 'caocao'])
@pytest.mark.parametrize('legacy', [False, True])
def test_passenger_layout_pair_has_complete_products(environment: Any, provider: str, legacy: bool) -> None:
    _, tools, _ = environment
    rows = ride_layout(environment, provider)
    assert pair_status(*rows) == 'supported'
    checked = inspect_rows(tools, rows, legacy=legacy)
    assert (checked['status'], checked['coverage_complete'], checked['category']) == ('exact', True, '打车'), checked
    facts = material_facts(rows[0])
    assert all(g['name'] == '客运服务费' for g in facts['goods'])
    assert len(facts['goods']) == (1 if provider == 'didi' else 2)
    assert sum(g['amount_cents'] + g['tax_cents'] for g in facts['goods']) == facts['total_cents']
    assert '999999********9999' not in encoded(facts)


def test_caocao_detached_issuer_after_remarks_is_not_a_product(environment: Any) -> None:
    rows = ride_layout(environment, 'caocao')
    text = rows[0]['text']
    assert text.index('开票人：') < text.index('*交通运输服务*客运服务费')
    assert text.index('购方地址：') < text.index('购方开户行：') < text.index('甲乙')
    facts = material_facts(rows[0])
    assert facts['source_text'].splitlines()[-1] == '甲乙'
    assert _source_products(facts, facts['source_text']) == ['客运服务费']


@pytest.mark.parametrize('provider', ['didi', 'caocao'])
@pytest.mark.parametrize('problem', ['extra', 'platform', 'amount', 'source_text_truncated', 'goods_text_truncated'])
def test_passenger_layout_keeps_original_guards(environment: Any, monkeypatch: Any, provider: str, problem: str) -> None:
    _, tools, _ = environment
    rows = ride_layout(environment, provider, extra='*其他商品*未知仪器' if problem == 'extra' else '',
                       trip_provider=('曹操出行' if provider == 'didi' else '滴滴出行') if problem == 'platform' else '',
                       trip_amount='18.00' if problem == 'amount' else '')
    if problem.endswith('truncated'):
        from invoice_print_layout import agent_evidence
        def truncated(entry: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            fact = material_facts(entry, **kwargs)
            fact[problem] = True
            return fact
        monkeypatch.setattr(agent_evidence, 'material_facts', truncated)
    checked = inspect_rows(tools, rows)
    assert checked['status'] != 'exact', checked
    if problem in {'platform', 'amount'}:
        assert pair_status(*rows) != 'supported'


@pytest.mark.parametrize('provider,malformed', [('didi', True), ('didi', False), ('caocao', False)])
def test_passenger_layout_malformed_table_fails_closed(environment: Any, provider: str, malformed: bool) -> None:
    _, tools, _ = environment
    rows = ride_layout(environment, provider, malformed=malformed, table_extra=not malformed)
    assert material_facts(rows[0])['issues']
    assert inspect_rows(tools, rows)['status'] == 'needs_input'


@pytest.mark.parametrize('mutation', ['quote', 'digest'])
def test_legacy_alias_needs_its_own_grounded_quote(environment: Any, mutation: str) -> None:
    _, tools, _ = environment
    rows = ride_layout(environment, 'didi')
    goods = [{**good, 'material_id': row['id'], 'sha256': row['digest']}
             for row in rows for good in goods_from_text(row['text'], {})]
    goods[0]['quote' if mutation == 'quote' else 'sha256'] = '未知独立商品' if mutation == 'quote' else 'a' * 64
    assert tools.content._inspect_products({'goods': goods}, rows)['status'] == 'needs_input'


@pytest.mark.parametrize('provider', ['didi', 'caocao'])
def test_startup_v2_rechecks_v1_empty_receipt_once(environment: Any, provider: str) -> None:
    jobs, tools, _ = environment
    rows = ride_layout(environment, provider)
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    assert len(done['item_ids']) == 1, done
    ident = done['item_ids'][0]
    flag_expense(tools, ident)
    # F12's text fallback saved one short service record, including the wrap.
    with tools.store.connect() as db:
        item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
        item['agent_content']['goods'] = [
            {**good, 'material_id': row['id'], 'sha256': row['digest']}
            for row in rows for good in goods_from_text(row['text'], {})]
        db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), ident))
    jobs.update(done['id'], deferred_content=[ident])
    jobs.cache(done['id'], 'organize-review-repair-v1', {'material_ids': [], 'item_ids': []})
    snapshots = []
    for _ in range(2):
        service = AgentService(tools.store)
        current = tools.store.get(ident)
        assert not current.get('agent_review_reason'), current['agent_content']
        assert current['agent_content']['product_check']['status'] == 'exact'
        assert service.jobs.get(done['id'])['deferred_content'] == []
        assert len(tools.store.list_items()) == 1
        snapshots.append(deepcopy([current, service.jobs.get(done['id'])]))
        with tools.store.connect() as db:
            receipts = {r['key'] for r in db.execute('SELECT key FROM invoice_agent_actions WHERE job_id=?', (done['id'],))}
        assert {'organize-review-repair-v1', 'organize-review-repair-v2'} <= receipts
        service.close()
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize('mutation', ['not_deferred', 'not_flagged', 'human_edit', 'changed_source', 'verified'])
def test_v2_keeps_out_of_scope_or_changed_expenses(environment: Any, mutation: str) -> None:
    jobs, tools, _ = environment
    rows = ride_layout(environment, 'caocao')
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    ident = done['item_ids'][0]
    flag_expense(tools, ident)
    jobs.update(done['id'], deferred_content=[] if mutation == 'not_deferred' else [ident])
    jobs.cache(done['id'], 'organize-review-repair-v1', {'material_ids': [], 'item_ids': []})
    with tools.store.connect() as db:
        item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
        if mutation == 'not_flagged':
            item.pop('agent_review_reason')
        elif mutation == 'human_edit':
            item['title'] = '人工修改'
        elif mutation == 'verified':
            item['verified'] = True
        elif mutation == 'changed_source':
            Path(rows[0]['path']).write_bytes(b'changed synthetic source')
        before = encoded(item)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (before, ident))
    service = AgentService(tools.store)
    with tools.store.connect() as db:
        assert db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'] == before
        assert not db.execute('SELECT 1 FROM invoice_agent_actions WHERE job_id=? AND key=?',
                              (done['id'], 'organize-review-repair-v2')).fetchone()
    assert len(tools.store.list_items()) == 1
    service.close()
