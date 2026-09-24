import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


spec=importlib.util.spec_from_file_location('policy_probe',Path(__file__).parents[1]/'scripts/codex_policy_probe.py')
probe=importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_readonly_sqlite(tmp_path):
    path=tmp_path/'sample.sqlite'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE example(x)')
    db=probe.read_db(path)
    try:
        with pytest.raises(sqlite3.OperationalError):db.execute('INSERT INTO example VALUES (1)')
    finally:db.close()
    with pytest.raises(sqlite3.OperationalError):probe.read_db(tmp_path/'absent.sqlite')
    assert not (tmp_path/'absent.sqlite').exists()


def test_literal_parse_never_evaluates(tmp_path):
    command='Stop-Process -Id 123; $(Write-Output PRIVATE_TOKEN)'
    code='text(await tools.exec_command({cmd:'+json.dumps(command)+'}))'
    assert probe.literal_command(code)==command
    assert probe.literal_command('tools.exec_command({cmd:computedVariable})') is None
    assert probe.literal_command(code+code) is None


def test_refusals_ignore_quoted_error_and_return_correlated_literal(tmp_path):
    path=tmp_path/'session.jsonl'
    prefix='Script error:\nexec_command failed: CreateProcess {message: "blocked by policy"}'
    records=[
        {'payload':{'type':'custom_tool_call','name':'exec','call_id':'call1','input':'tools.exec_command({cmd:"Stop-Process -Id 123"})'}},
        {'timestamp':'2026-01-01T00:00:00Z','payload':{'type':'custom_tool_call_output','call_id':'call1','output':[{'text':prefix}]}},
        {'payload':{'type':'custom_tool_call_output','call_id':'other','output':[{'text':'Quoted prior log: '+prefix}]}},
    ]
    path.write_text('\n'.join(json.dumps(r) for r in records),encoding='utf-8')
    result=probe.refusals(path)
    assert len(result)==1 and result[0]['command']=='Stop-Process -Id 123'


def test_only_execpolicy_is_launched_and_raw_patterns_not_exported(tmp_path,monkeypatch):
    cli=tmp_path/'codex.exe';cli.touch()
    rule=tmp_path/'default.rules';rule.write_text('')
    calls=[]
    def run(argv,**kwargs):
        calls.append((argv,kwargs))
        return SimpleNamespace(returncode=0,stdout=json.dumps({'decision':'forbidden','matchedRules':[{'pattern':'PRIVATE_TOKEN'}]}))
    monkeypatch.setattr(probe.subprocess,'run',run)
    command='Stop-Process -Id 123; Start-Process anything'
    result=probe.check_rules(cli,[rule],Path('pwsh.exe'),command)
    argv,kwargs=calls[0]
    assert argv[:3]==[str(cli),'execpolicy','check']
    assert argv[-4:]==['--','pwsh.exe','-Command',command]
    assert kwargs['shell'] is False and kwargs['timeout']==15
    assert 'PRIVATE_TOKEN' not in json.dumps(result) and result['matched_count']==1


def test_config_reports_only_allowed_fields(tmp_path):
    path=tmp_path/'config.toml'
    path.write_text('sandbox_mode="workspace-write"\n[env]\nAPI_KEY="PRIVATE_TOKEN"\n[mcp_servers.secret]\npassword="PRIVATE_PASSWORD"',encoding='utf-8')
    result=probe.config_summary(path,tmp_path)
    assert result['sandbox_mode']=='workspace-write'
    assert 'PRIVATE' not in json.dumps(result)


def test_logs_redact_body_and_filter_targets(tmp_path):
    path=tmp_path/'logs.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE logs(id,ts,level,target,feedback_log_body)')
        db.executemany('INSERT INTO logs VALUES (?,?,?,?,?)',[
            (1,1767225600,'INFO','codex_core::tools::parallel','call1 tool call completed PRIVATE_TOKEN'),
            (2,1767225600,'DEBUG','codex_http_client::client','Authorization PRIVATE_TOKEN'),
            (3,1767225600,'INFO','codex_core::tools::parallel','other_call PRIVATE_TOKEN'),
        ])
    result=probe.nearby_logs(path,{'at':'2026-01-01T00:00:00Z','call_id':'call1'})
    assert len(result)==1 and result[0]['id']==1
    assert 'PRIVATE_TOKEN' not in json.dumps(result)
