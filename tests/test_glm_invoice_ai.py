from __future__ import annotations

import base64
import copy
import io
import json
from pathlib import Path
from typing import Any
import urllib.error

from PIL import Image
import pytest

from invoice_print_layout import glm_invoice_ai as glm


KEY = 'synthetic-only-secret-token'
TEXT = '电子发票（普通发票）\n发票号码：26360000000000000123\n销售方：虚构印刷有限公司\n价税合计（小写）：¥50.00\n开票日期：2026年10月12日\n*印刷服务*印刷费'


def extraction(**changes: Any) -> dict[str, Any]:
    result: dict[str, Any] = {'text': '', 'role': 'invoice', 'fields': {
        'invoice_number': '26360000000000000123', 'merchant': '虚构印刷有限公司', 'amount': '50.00'},
        'issues': [], 'evidence': {'invoice_number': '发票号码：26360000000000000123',
            'merchant': '销售方：虚构印刷有限公司', 'amount': '价税合计（小写）：¥50.00'}}
    result.update(changes)
    return result


class Response:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload, self.status = payload, status
        self.read_limit = 0

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.payload[:limit]


class Opener:
    def __init__(self, response: Response | Exception) -> None:
        self.response = response
        self.calls: list[tuple[Any, int]] = []

    def open(self, request: Any, *, timeout: int) -> Response:
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def network(monkeypatch: pytest.MonkeyPatch, answer: Any, **envelope_changes: Any) -> Opener:
    content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    envelope = {'model': glm.MODEL, 'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
    envelope.update(envelope_changes)
    opener = Opener(Response(json.dumps(envelope, ensure_ascii=False).encode()))
    monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda handler: opener)
    return opener


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('GLM_API_KEY', raising=False)
    monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda handler: pytest.fail('unexpected network'))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> glm.GlmInvoiceAI:
    (tmp_path / 'invoice-ai.json').write_text('{"enabled":true}', encoding='utf-8')
    monkeypatch.setenv('GLM_API_KEY', KEY)
    return glm.GlmInvoiceAI(tmp_path)


def png() -> bytes:
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(output, format='PNG')
    return output.getvalue()


def test_no_configuration_is_offline_even_with_process_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GLM_API_KEY', KEY)
    client = glm.GlmInvoiceAI(tmp_path)
    assert client.public_status()['available'] is False
    assert client.enabled is False
    with pytest.raises(glm.GlmAIError) as error:
        client.extract(TEXT)
    assert error.value.failure_code == 'config_unavailable'


def test_disabled_config_does_not_read_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'invoice-ai.json').write_text('{"enabled":false,"secret_env_file":"absent.env"}')
    monkeypatch.setenv('GLM_API_KEY', 'not a key')
    client = glm.GlmInvoiceAI(tmp_path)
    assert client.enabled is False
    assert client.public_status()['available'] is False
    assert '未启用' in client.public_status()['message']


@pytest.mark.parametrize('config', [[], {}, {'enabled': 'true'}, {'enabled': 1}, {'enabled': None},
                                    '{"enabled":true,"enabled":false}', 'broken'])
def test_bad_config_never_enables_network(tmp_path: Path, config: Any) -> None:
    (tmp_path / 'invoice-ai.json').write_text(config if isinstance(config, str) else json.dumps(config))
    assert not glm.GlmInvoiceAI(tmp_path).public_status()['available']


def test_config_and_credential_file_size_bounds(tmp_path: Path) -> None:
    config = tmp_path / 'invoice-ai.json'
    config.write_text(' ' * (glm.MAX_TEXT_CHARS + 1))
    assert not glm.GlmInvoiceAI(tmp_path).enabled
    config.write_text('{"enabled":true,"secret_env_file":"key.env"}')
    (tmp_path / 'key.env').write_bytes(b' ' * (1024 * 1024 + 1))
    assert not glm.GlmInvoiceAI(tmp_path).public_status()['available']


@pytest.mark.parametrize('secret_text', [f'GLM_API_KEY={KEY}', f'export GLM_API_KEY="{KEY}" # note',
    f'OTHER_TOKEN=$(not-executed)\nGLM_API_KEY=\'{KEY}\'\nOTHER_KEY=private'])
def test_only_explicit_env_assignment_loaded(tmp_path: Path, secret_text: str) -> None:
    (tmp_path / 'invoice-ai.json').write_text('{"enabled":true,"secret_env_file":"key.env"}')
    (tmp_path / 'key.env').write_text(secret_text, encoding='utf-8-sig')
    status = glm.GlmInvoiceAI(tmp_path).public_status()
    assert status['available'] is True
    assert KEY not in json.dumps(status) and 'key.env' not in json.dumps(status)


@pytest.mark.parametrize('secret_text', ['GLM_API_KEY=$(echo insecure)', 'GLM_API_KEY=${OTHER}',
    'GLM_API_KEY=`echo insecure`', 'GLM_API_KEY="two tokens"', 'GLM_API_KEY="unfinished',
    'OTHER_KEY=private', f'GLM_API_KEY={KEY}\nGLM_API_KEY=another-secret-token'])
def test_secret_file_is_not_shell_or_ambiguous(tmp_path: Path, secret_text: str) -> None:
    (tmp_path / 'invoice-ai.json').write_text('{"enabled":true,"secret_env_file":"key.env"}')
    (tmp_path / 'key.env').write_text(secret_text)
    assert not glm.GlmInvoiceAI(tmp_path).public_status()['available']


def test_process_key_precedes_absent_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'invoice-ai.json').write_text('{"enabled":true,"secret_env_file":"absent.env"}')
    monkeypatch.setenv('GLM_API_KEY', KEY)
    assert glm.GlmInvoiceAI(tmp_path).public_status()['available']


def test_enabled_without_key_reports_safe_error(tmp_path: Path) -> None:
    (tmp_path / 'invoice-ai.json').write_text('{"enabled":true}')
    client = glm.GlmInvoiceAI(tmp_path)
    assert client.enabled and not client.public_status()['available']
    with pytest.raises(glm.GlmAIError) as error:
        client.extract(TEXT)
    assert error.value.failure_code == 'config_unavailable'


def test_native_text_extraction_keeps_original_and_uses_official_contract(client: glm.GlmInvoiceAI,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    opener = network(monkeypatch, extraction())
    result = client.extract(TEXT)
    assert result['text'] == TEXT
    assert result['role'] == 'invoice' and result['fields']['amount'] == '50.00'
    assert result['issues'] == []
    assert 'expense_date' not in result['fields'] and 'payment_status' not in result['fields']
    assert result['model'] == glm.MODEL
    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert request.full_url == glm.ENDPOINT and timeout == 60
    assert request.get_header('Authorization') == 'Bearer ' + KEY
    body = json.loads(request.data)
    assert body['model'] == glm.MODEL and body['stream'] is False
    assert body['thinking'] == {'type': 'enabled'} and body['reasoning_effort'] == 'high'
    assert body['response_format'] == {'type': 'json_object'}
    assert '发票和收据没有支付状态是正常情况' in body['messages'][0]['content']
    assert 'expense_date留空，不列为issues' in body['messages'][0]['content']
    assert KEY not in request.data.decode()


def test_png_is_in_memory_base64_and_not_a_file(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    opener = network(monkeypatch, extraction(text=TEXT))
    picture = png()
    before = set(tmp_path.rglob('*'))
    result = client.extract('', images=[picture])
    assert result['text'] == TEXT
    body = json.loads(opener.calls[0][0].data)
    image_url = body['messages'][1]['content'][1]['image_url']['url']
    assert image_url == 'data:image/png;base64,' + base64.b64encode(picture).decode()
    assert set(tmp_path.rglob('*')) == before


@pytest.mark.parametrize('text,images', [('', None), ('x' * (glm.MAX_TEXT_CHARS + 1), None),
    ('', [b'bad']), ('', [b'\x89PNG\r\n\x1a\n']), ('', [png()] * 5),
    ('', [b'\x89PNG\r\n\x1a\n' + b'x' * glm.MAX_IMAGE_BYTES]), ('', 'not a list')],
    ids=['empty', 'text-too-large', 'not-png', 'truncated-png', 'too-many-images', 'png-too-large', 'not-list'])
def test_input_limits_fail_before_network(client: glm.GlmInvoiceAI, text: str, images: Any) -> None:
    with pytest.raises(glm.GlmAIError):
        client.extract(text, images=images)


def test_maximum_image_count_accepted(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch) -> None:
    opener = network(monkeypatch, extraction(text=TEXT))
    client.extract('', images=[png()] * 4)
    assert len(json.loads(opener.calls[0][0].data)['messages'][1]['content']) == 5


@pytest.mark.parametrize('role', ['invoice', 'receipt', 'order', 'trip', 'detail', 'other'])
def test_roles_preserved(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    network(monkeypatch, extraction(role=role))
    assert client.extract(TEXT)['role'] == role


@pytest.mark.parametrize('change', [ {'project_id': 'invented'}, {'fields': {'project_id': 'invented'}},
    {'role': 'purchase'}, {'role': None}, {'issues': 'wrong'}, {'issues': [1]},
    {'issues': ['x' * 241]}, {'issues': ['x'] * 21}, {'fields': {'amount': 50}},
    {'fields': {'merchant': 'x' * 241}}, {'evidence': {'amount': 'invented amount50'}},
    {'text': 'invented replacement'}, {'fields': {'merchant': '虚构公司'}},
    {'fields': {'payment_status': 'confirmed'}, 'evidence': {'payment_status': '电子发票'}}])
def test_invalid_extraction_rejected(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch,
                                     change: dict[str, Any]) -> None:
    network(monkeypatch, extraction(**change))
    with pytest.raises(glm.GlmAIError, match='识别字段无效'):
        client.extract(TEXT)


@pytest.mark.parametrize('amount', ['0', '-50', 'NaN', 'Infinity', '50.001', '500.00', '1e2', True,
                                  '99999999999.99'])
def test_amount_cannot_be_nondecimal_zero_negative_or_unsupported(client: glm.GlmInvoiceAI,
    monkeypatch: pytest.MonkeyPatch, amount: Any) -> None:
    answer = extraction()
    answer['fields']['amount'] = amount
    network(monkeypatch, answer)
    with pytest.raises(glm.GlmAIError):
        client.extract(TEXT)


def test_integer_visible_amount_normalized_to_cents(client: glm.GlmInvoiceAI,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    answer = extraction()
    answer['fields']['amount'] = '50'
    network(monkeypatch, answer)
    assert client.extract(TEXT)['fields']['amount'] == '50.00'


def test_missing_amount_remains_empty_with_issue(client: glm.GlmInvoiceAI,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    network(monkeypatch, extraction(fields={}, evidence={}))
    result = client.extract(TEXT)
    assert 'amount' not in result['fields']
    assert any('金额未能确定' in issue for issue in result['issues'])


@pytest.mark.parametrize('field', ['title', 'note'])
def test_unproven_optional_display_field_discarded_without_losing_identity(client: glm.GlmInvoiceAI,
    monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    answer = extraction()
    answer['fields'][field] = '机器生成的展示概括'
    network(monkeypatch, answer)
    result = client.extract(TEXT)
    assert field not in result['fields']
    assert result['role'] == 'invoice' and result['fields']['amount'] == '50.00'
    assert result['fields']['merchant'] == '虚构印刷有限公司'
    assert result['fields']['invoice_number'] == '26360000000000000123'
    assert result['issues'] == []


@pytest.mark.parametrize('field', ['amount', 'merchant'])
def test_unproven_core_field_still_rejects_entire_extraction(client: glm.GlmInvoiceAI,
    monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    answer = extraction()
    answer['evidence'].pop(field)
    network(monkeypatch, answer)
    with pytest.raises(glm.GlmAIError, match='识别字段无效'):
        client.extract(TEXT)


@pytest.mark.parametrize('value,quote', [('2026-10-12', '开票日期：2026年10月12日'),
    ('2026-10-11', '支付时间：2026年10月12日'), ('2026-02-30', '支付时间：2026年02月30日')])
def test_invalid_or_invoice_date_not_expense_date(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch,
                                                 value: str, quote: str) -> None:
    answer = extraction()
    answer['fields']['expense_date'] = value
    answer['evidence']['expense_date'] = quote
    network(monkeypatch, answer)
    result = client.extract(TEXT + '\n' + quote)
    assert 'expense_date' not in result['fields'] and result['issues'] == []
    assert result['fields']['amount'] == '50.00' and result['field_warnings']


def test_explicit_consumption_date_normalized(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = extraction()
    answer['fields']['expense_date'] = '2026-10-02'
    answer['evidence']['expense_date'] = '消费日期：2026年10月2日'
    network(monkeypatch, answer)
    assert client.extract(TEXT + '\n消费日期：2026年10月2日')['fields']['expense_date'] == '2026-10-02'


@pytest.mark.parametrize('quote', [
    '支付时间2026-10-04 19:50:52', '支付时间2026-10-0419:50:52',
    '支付时间2026-10-04T19:50:52', '支付时间2026/10/04 19:50:52',
    '支付时间2026.10.0419:50:52', '支付时间2026年10月4日19:50:52',
    '支付时间2026年10月4日19时50分52秒', '支付时间2026年10月4日 19时50分',
    '支付时间2026-10-4 19:50:52', '支付时间2026 - 10 - 04 19 : 50 : 52',
    '支付时间2026-10-04 00:00:00', '支付时间2026-10-04 23:59:59',
    '消费日期2026-10-04', '订单日期2026年10月4日', '支付时间2026-10-04 19:50',
])
def test_visible_date_and_legal_attached_time_accepted(client: glm.GlmInvoiceAI,
    monkeypatch: pytest.MonkeyPatch, quote: str) -> None:
    answer = extraction()
    answer['fields']['expense_date'] = '2026-10-04'
    answer['evidence']['expense_date'] = quote
    network(monkeypatch, answer)
    result = client.extract(TEXT + '\n' + quote)
    assert result['fields']['expense_date'] == '2026-10-04'
    assert result['issues'] == []


@pytest.mark.parametrize('quote', [
    '支付时间2026-10-04 25:50:52', '支付时间2026-10-04 19:60:52',
    '支付时间2026-10-04 19:50:60', '支付时间2026-10-0419:50:',
    '支付时间2026-10-0419:50:5200', '支付时间2026-10-04195052',
    '支付时间2026-10-04123456789012345678', '支付时间12026-10-0419:50:52',
    '支付时间2026-10-04Tno', '支付时间2026-10-04T24:00:00',
    '支付时间2026-10-04T19:5:52', '支付时间2026-10-04T19:50:5',
    '支付时间2026-10-04:19:50', '支付时间2026年10月4日19时60分52秒',
    '支付时间2026年10月4日19时50分61秒', '支付时间2026-13-04 19:50:52',
    '支付时间2026-02-30 19:50:52', '支付时间2026-10-05 19:50:52',
    '支付时间2026-10-04 19:50 :99', '开票时间2026-10-0419:50:52',
    '支付时间2026-10-04 -01:00:00', '支付时间2026-10-04 +19:50:52',
])
def test_long_identifiers_bad_dates_or_illegal_time_not_accepted_as_date(client: glm.GlmInvoiceAI,
    monkeypatch: pytest.MonkeyPatch, quote: str) -> None:
    answer = extraction()
    answer['fields']['expense_date'] = '2026-10-04'
    answer['evidence']['expense_date'] = quote
    network(monkeypatch, answer)
    result = client.extract(TEXT + '\n' + quote)
    assert 'expense_date' not in result['fields'] and 'expense_date' not in result['evidence']
    assert result['issues'] == [] and result['fields']['amount'] == '50.00'
    assert result['field_warnings']


def test_synthetic_payment_date_with_attached_time_retains_all_correct_fields(client: glm.GlmInvoiceAI,
    monkeypatch: pytest.MonkeyPatch) -> None:
    text = '微信支付\n-124.00\n商户虚构便利店\n当前状态支付成功\n支付时间2026-10-0419:50:52'
    network(monkeypatch, {'text': '', 'role': 'payment', 'fields': {'merchant': '虚构便利店', 'amount': '124.00',
        'payment_status': 'succeeded', 'expense_date': '2026-10-04'}, 'issues': [],
        'evidence': {'merchant': '商户虚构便利店', 'amount': '-124.00', 'payment_status': '当前状态支付成功',
                     'expense_date': '支付时间2026-10-0419:50:52'}})
    result = client.extract(text)
    assert result['role'] == 'payment' and result['issues'] == []
    assert result['fields'] == {'merchant': '虚构便利店', 'amount': '124.00',
                              'payment_status': 'succeeded', 'expense_date': '2026-10-04'}


@pytest.mark.parametrize('code', sorted(glm.FAILURE_CODES))
def test_diagnostic_messages_cannot_echo_caller_or_provider_text(code: str) -> None:
    error = glm.GlmAIError('provider body Authorization Bearer ' + KEY,
                          failure_code=code, failure_field='amount')
    assert error.failure_code == code and error.failure_field == 'amount'
    assert str(error) == glm.FAILURE_MESSAGES[code]
    assert KEY not in repr(error) and 'Authorization' not in str(error)


def test_unknown_failure_metadata_is_not_exposed() -> None:
    error = glm.GlmAIError(KEY, failure_code=KEY, failure_field='C:/private/' + KEY)
    assert error.failure_code == 'internal_error' and error.failure_field == ''
    assert str(error) == glm.FAILURE_MESSAGES['internal_error']


@pytest.mark.parametrize('change,code,field', [
    ({'fields': {'unknown': 'anything'}}, 'field_schema', ''),
    ({'fields': {'amount': 50}}, 'field_schema', 'amount'),
    ({'fields': {'amount': '-50'}}, 'field_schema', 'amount'),
    ({'fields': {'merchant': '不存在的商家'}}, 'evidence_invalid', 'merchant'),
    ({'evidence': {'amount': '不存在的证据50.00'}}, 'evidence_invalid', 'amount'),
    ({'evidence': {}}, 'evidence_invalid', 'merchant'),
])
def test_extraction_reports_only_allowlisted_failure_kind_and_field(client: glm.GlmInvoiceAI,
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], code: str, field: str) -> None:
    network(monkeypatch, extraction(**change))
    with pytest.raises(glm.GlmAIError) as error:
        client.extract(TEXT)
    assert error.value.failure_code == code and error.value.failure_field == field


def test_response_diagnostics_survive_request_and_extraction_layers(client: glm.GlmInvoiceAI,
    monkeypatch: pytest.MonkeyPatch) -> None:
    for answer in ('broken', '[1]', '{"role":"invoice","role":"other"}'):
        network(monkeypatch, answer)
        with pytest.raises(glm.GlmAIError) as error:
            client.extract(TEXT)
        assert error.value.failure_code == 'json_invalid'
        assert error.value.failure_field == ''
    network(monkeypatch, extraction(), model='different-model')
    with pytest.raises(glm.GlmAIError) as error:
        client.extract(TEXT)
    assert error.value.failure_code == 'response_schema'


@pytest.mark.parametrize('field,value,quote', [('order_number', '26360000000000000123',
                                             '发票号码：26360000000000000123')])
def test_wrong_order_anchor_is_discarded_without_losing_valid_core(client: glm.GlmInvoiceAI,
        monkeypatch: pytest.MonkeyPatch, field: str, value: str, quote: str) -> None:
    response = extraction()
    response['fields'][field] = value
    response['evidence'][field] = quote
    network(monkeypatch, response)
    result = client.extract(TEXT + '\n' + quote)
    assert field not in result['fields']
    assert result['fields']['amount'] == '50.00' and result['fields']['merchant'] == '虚构印刷有限公司'
    assert result['fields']['invoice_number'] == '26360000000000000123'
    assert any('订单号' in warning for warning in result['field_warnings'])


@pytest.mark.parametrize('status,expected', [('支付成功', 'succeeded'), ('付款成功', 'succeeded'),
    ('交易成功', 'succeeded'), ('支付失败', 'failed'), ('已退款', 'refunded'),
    ('退款成功', 'refunded'), ('待支付', 'unpaid'), ('未付款', 'unpaid'), ('状态未知', 'unknown')])
def test_payment_success_is_required_even_if_model_claims_success(client: glm.GlmInvoiceAI,
    monkeypatch: pytest.MonkeyPatch, status: str, expected: str) -> None:
    text = '微信支付\n虚构印刷有限公司\n-50.00\n当前状态\n' + status
    network(monkeypatch, {'text': '', 'role': 'payment', 'fields': {'amount': '50', 'payment_status': 'succeeded'},
                         'issues': [], 'evidence': {'amount': '-50.00', 'payment_status': status}})
    result = client.extract(text)
    assert result['fields']['payment_status'] == expected
    assert result['evidence']['payment_status'] == status
    assert result['role'] == ('payment' if expected == 'succeeded' else 'other')
    assert bool(result['issues']) is (expected != 'succeeded')


def test_refunded_payment_overrides_success_on_same_page(client: glm.GlmInvoiceAI,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    text = '微信支付\n-50.00\n支付成功\n已全额退款'
    network(monkeypatch, {'text': '', 'role': 'payment', 'fields': {'amount': '50'},
                         'issues': [], 'evidence': {'amount': '-50.00'}})
    assert client.extract(text)['role'] == 'other'


def test_payment_without_visible_status_remains_blocked(client: glm.GlmInvoiceAI,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    network(monkeypatch, {'text': '', 'role': 'payment', 'fields': {'amount': '50'},
                         'issues': [], 'evidence': {'amount': '-50.00'}})
    result = client.extract('微信支付\n虚构印刷有限公司\n-50.00')
    assert result['role'] == 'other'
    assert result['fields']['payment_status'] == 'unknown'
    assert any('未能确认付款成功' in issue for issue in result['issues'])


def test_qr_delivery_is_not_formal_invoice(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch) -> None:
    text = '扫码下载发票\n发票号码：26360000000000000123\n价税合计50.00'
    answer = extraction(fields={'invoice_number': '26360000000000000123', 'amount': '50.00'},
                        evidence={'invoice_number': '发票号码：26360000000000000123', 'amount': '价税合计50.00'})
    network(monkeypatch, answer)
    result = client.extract(text)
    assert result['role'] == 'other'
    assert any('正式发票' in issue for issue in result['issues'])


@pytest.mark.parametrize('answer', ['```json\n{"option_id":"a"}\n```', '{"option_id":"a","answer":"extra"}',
    '{"option_id":"a","option_id":"review"}', '{"option_id":NaN}', '["a"]', 'broken',
    {'option_id': 'invented'}, {'option_id': 1}, {'option_id': KEY}])
def test_invalid_decision_no_silent_salvage(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch,
                                           answer: Any) -> None:
    network(monkeypatch, answer)
    with pytest.raises(glm.GlmAIError) as error:
        client.decide({'project_id': 'synthetic'}, [{'id': 'a'}])
    assert KEY not in str(error.value)


def test_decide_preserves_materials_and_respects_review(client: glm.GlmInvoiceAI,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    state = {'project_id': 'synthetic', 'vouchers': [{'id': 'q:1', 'amount_cents': 5000,
             'text': '虚构商家 实付50元'}], 'payments': [{'id': 'p1', 'amount_cents': 5000}]}
    options = [{'id': 'o1', 'voucher_ids': ['q:1'], 'payment_ids': ['p1']}]
    before = copy.deepcopy((state, options))
    opener = network(monkeypatch, {'option_id': 'review'})
    result = client.decide(state, options)
    assert result == {'option_id': 'review', 'model': glm.MODEL}
    assert (state, options) == before
    body = json.loads(opener.calls[0][0].data)
    assert json.loads(body['messages'][1]['content']) == {'state': state, 'options': options}
    assert '多个业务上同样合理' in body['messages'][0]['content']
    assert '{"option_id":"候选id或review"}' in body['messages'][0]['content']
    assert '键名option_id不可替换' in body['messages'][0]['content']
    assert '禁止answer、choice、理由或其他字段' in body['messages'][0]['content']


def test_valid_candidate_selection(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch) -> None:
    network(monkeypatch, {'option_id': 'candidate-1'})
    assert client.decide({}, [{'id': 'candidate-1'}])['option_id'] == 'candidate-1'


@pytest.mark.parametrize('options', [[], [{'id': 'review'}]])
def test_empty_or_review_only_returns_review_without_api(client: glm.GlmInvoiceAI, options: Any) -> None:
    assert client.decide({}, options) == {'option_id': 'review', 'model': glm.MODEL}


@pytest.mark.parametrize('state,options', [({}, [{'id': 'a'}, {'id': 'a'}]), ({}, [{'id': 'x y'}]),
    ({}, [{'id': 1}]), ({}, [1]), ({}, [{'id': str(i)} for i in range(129)]),
    ({'text': 'x' * glm.MAX_TEXT_CHARS}, [{'id': 'a'}]), ({'amount': float('nan')}, [{'id': 'a'}]),
    ({'text': object()}, [{'id': 'a'}]), ([], [{'id': 'a'}])])
def test_decision_input_bounds(client: glm.GlmInvoiceAI, state: Any, options: Any) -> None:
    with pytest.raises(glm.GlmAIError):
        client.decide(state, options)


@pytest.mark.parametrize('exception', [TimeoutError('provider leaked ' + KEY),
    urllib.error.URLError(KEY), urllib.error.HTTPError(glm.ENDPOINT, 429, KEY, {}, io.BytesIO(KEY.encode()))])
def test_transport_error_is_safe_and_not_retried(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch,
                                                exception: Exception) -> None:
    opener = Opener(exception)
    monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda handler: opener)
    with pytest.raises(glm.GlmAIError) as error:
        client.extract(TEXT)
    assert len(opener.calls) == 1
    assert KEY not in str(error.value) and '429' not in str(error.value)
    assert error.value.failure_code == 'network_error' and error.value.failure_field == ''
    assert error.value.__suppress_context__


@pytest.mark.parametrize('envelope', [{'model': 'glm-other'}, {'choices': []}, {'choices': [{
    'finish_reason': 'length', 'message': {'content': '{"option_id":"a"}'}}]}, {'choices': [{
    'finish_reason': 'stop', 'message': {'content': None}}]}, {'choices': [{
    'finish_reason': 'stop', 'message': {'content': '{"option_id":"a"}', 'tool_calls': [{'id': 'x'}]}}]}])
def test_provider_envelope_is_validated(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch,
                                        envelope: dict[str, Any]) -> None:
    network(monkeypatch, {'option_id': 'a'}, **envelope)
    with pytest.raises(glm.GlmAIError):
        client.decide({}, [{'id': 'a'}])


def test_response_size_bound_and_non200(client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch) -> None:
    for response in (Response(b'x' * (glm.MAX_RESPONSE_BYTES + 1)), Response(b'private', status=302)):
        monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda handler: Opener(response))
        with pytest.raises(glm.GlmAIError) as error:
            client.extract(TEXT)
        assert error.value.failure_code == ('response_schema' if response.status == 200 else 'network_error')
    assert response.read_limit == 0


def test_redirect_handler_never_forwards_credentials() -> None:
    assert glm._NoRedirect().redirect_request(None, None, 302, 'redirect', None, 'https://elsewhere') is None
