"""Automatic category caches cannot overrule complete current source checks."""
from __future__ import annotations

import re
import threading
from typing import Any

import pymupdf
import pytest

from invoice_print_layout import expense_classifier as classifier
from invoice_print_layout.agent_evidence import material_facts
from invoice_print_layout.agent_jobs import AgentLoop
from invoice_print_layout.automatic_category import AutomaticCategory, digest
from tests.test_invoice_agent import environment, material, start
from tests.test_invoice_agent_content import pair


def remember(env: Any, goods: str, category: str, *, origin: str = 'cache',
             disabled: bool = False) -> None:
    _, tools, _ = env
    AutomaticCategory(tools.store, threading.Lock())
    evidence = {'goods': goods, 'purpose': ''}
    normalized = {key: re.sub(r'\s+', '', value).casefold() for key, value in evidence.items()}
    key = digest([normalized, classifier.MODEL, classifier.CRITERIA, classifier.RULES])
    with tools.store.connect() as db:
        db.execute('INSERT INTO category_experience VALUES (?,?,?,0)', (key, category, origin))
        if disabled:
            db.execute('INSERT INTO category_experience_disabled VALUES (?)', (key,))


def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(classifier, 'ask_jev', lambda _: pytest.fail('No model call authorized'))


def test_automatic_cache_cannot_release_food_as_materials(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    remember(environment, '鼠标蛋糕', '材料采购')
    no_model(monkeypatch)
    done = AgentLoop(jobs).run(start(environment, pair(environment, '鼠标蛋糕'))['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0 and not done.get('preview')
    item = tools.store.get(done['item_ids'][0])
    assert item['agent_content']['decision']['category'] is None
    assert item['agent_content']['decision']['source'] != 'experience'
    assert '材料采购' in item['agent_content']['product_check']['forbidden_categories']
    assert not item['verified']


def test_exact_complete_rules_precede_an_automatic_wrong_cache(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    remember(environment, '鼠标\n亚克力导视牌', '餐饮')
    no_model(monkeypatch)
    done = AgentLoop(jobs).run(start(environment, pair(environment, '鼠标、亚克力导视牌'))['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0
    item = tools.store.get(done['item_ids'][0])
    assert item['category'] == '材料采购' and item['title'] == '鼠标、亚克力导视牌采购'
    assert item['agent_content']['decision']['source'] == 'rules'
    assert not item['verified']


def test_explicit_human_experience_retains_its_priority(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    remember(environment, '鼠标\n亚克力导视牌', '餐饮', origin='human')
    no_model(monkeypatch)
    done = AgentLoop(jobs).run(start(environment, pair(environment, '鼠标、亚克力导视牌'))['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0
    item = tools.store.get(done['item_ids'][0])
    assert item['category'] == '餐饮' and item['agent_content']['decision']['source'] == 'experience'
    assert not item['verified']


def test_disabled_human_experience_cannot_override_current_exact_rules(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    remember(environment, '鼠标\n亚克力导视牌', '餐饮', origin='human', disabled=True)
    no_model(monkeypatch)
    done = AgentLoop(jobs).run(start(environment, pair(environment, '鼠标、亚克力导视牌'))['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0
    item = tools.store.get(done['item_ids'][0])
    assert item['category'] == '材料采购' and item['agent_content']['decision']['source'] == 'rules'


def test_a_cache_cannot_supply_an_uncovered_source_product(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    remember(environment, '鼠标', '材料采购')
    no_model(monkeypatch)
    done = AgentLoop(jobs).run(start(environment, pair(environment, '鼠标\n午餐'))['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0 and not done.get('preview')
    item = tools.store.get(done['item_ids'][0])
    assert not item['agent_content']['product_check']['coverage_complete']
    assert item['agent_content']['decision']['category'] is None


def test_unread_pdf_pages_cannot_be_classified_from_only_the_first_ten(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, project = environment
    with pymupdf.open() as pdf:
        for index in range(11):
            page = pdf.new_page()
            text = ('收据\n商家名称：合成工具商店\n商品名称：鼠标\n实付：17.00' if index == 0 else
                    '商品名称：午餐' if index == 10 else '')
            for line_number, line in enumerate(text.splitlines()):
                page.insert_text((35, 45 + line_number * 24), line, fontname='china-s', fontsize=12)
        payload = pdf.tobytes()
    received = tools.intake.receive('synthetic-eleven-pages.pdf', payload, project_id=project)
    receipt = tools.intake.analyze_local(received['id'])
    facts = material_facts(receipt)
    assert facts['issues'] and facts['complete'] is False
    payment = material(tools, project, 'payment', '17', 'eleven-pages-payment')
    no_model(monkeypatch)
    done = AgentLoop(jobs).run(start(environment, [receipt, payment])['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0 and not done.get('preview')
    assert all(not tools.store.get(ident)['verified'] for ident in done['item_ids'])
