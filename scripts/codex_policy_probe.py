"""Read-only Codex refusal evidence and offline rules check; NEVER executes a recorded command.

Only subprocess: the supplied official codex.exe, with `execpolicy check`.
Only writes: the explicitly selected report directory. No credentials or raw log export.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tomllib
from urllib.parse import urlsplit
from typing import Any


def read_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def config_summary(path: Path, project: Path) -> dict[str, Any]:
    if not path.is_file():
        return {'available': False}
    config = tomllib.loads(path.read_text(encoding='utf-8-sig'))
    allowed = ('sandbox_mode', 'approval_policy', 'approvals_reviewer')
    result = {key: config.get(key) for key in allowed}
    result['windows_sandbox'] = config.get('windows', {}).get('sandbox')
    result['project_trust'] = next((value.get('trust_level') for key, value in config.get('projects', {}).items()
                                  if str(Path(key)).lower() == str(project).lower()), None)
    result['hooks_configured'] = bool(config.get('hooks'))
    return result


def literal_command(source: str) -> str | None:
    # Accept one literal JSON string only. No eval, no JavaScript execution or substitutions.
    values = re.findall(r'\bcmd\s*:\s*("(?:\\.|[^"\\])*")', source)
    if len(values) != 1:
        return None
    return str(json.loads(values[0]))


def invocation_summary(source: str) -> dict[str, Any]:
    """Report only literal tool options. Missing defaults are not recovered argv."""
    result: dict[str, Any] = {'exact_argv_recovered': False,
                             'replay_equivalence': 'unproven: tool defaults and rendered argv are not losslessly recovered'}
    for key in ('login', 'tty', 'shell', 'workdir', 'sandbox_permissions'):
        found = re.findall(r'\b'+key+r'\s*:\s*("(?:\\.|[^"\\])*"|true|false)(?=\s*[,}])', source)
        if len(found) != 1:
            continue
        value = json.loads(found[0])
        if key in {'shell', 'workdir'}:
            result[key+'_sha256'] = hashlib.sha256(str(value).encode()).hexdigest()
        else:
            result[key] = value
    return result


def command_features(command: str) -> dict[str, Any]:
    features: dict[str, Any] = {name: bool(re.search(re.escape(name), command, re.I))
                              for name in ('Stop-Process', 'Start-Process', 'WindowStyle Hidden', 'if (', 'for (')}
    urls = re.findall(r'https?://[^\s\'"<>`]+', command, re.I)
    kinds: Counter[str] = Counter()
    for url in urls:
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        kinds['loopback' if host in {'127.0.0.1', 'localhost', '::1'} else 'other_or_unresolved'] += 1
    features['http_url_count'] = len(urls)
    features['url_kinds'] = dict(kinds)
    features['launch_and_url_text_cooccurrence'] = bool(urls) and bool(re.search(
        r'\b(?:Start-Process|start|saps|Invoke-Item|ii)\b', command, re.I))
    features['scope'] = 'Text features only; not parser segments, builtin decision or proof of causation.'
    return features


def refusals(path: Path) -> list[dict[str, Any]]:
    calls: dict[str, str] = {}
    contexts: dict[str, dict[str, Any]] = {}
    context: dict[str, Any] = {}
    found = []
    with path.open(encoding='utf-8') as file:
        for line in file:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            data = event.get('payload', {})
            if event.get('type') == 'turn_context':
                sandbox = data.get('sandbox_policy')
                context = {'approval_policy': data.get('approval_policy'),
                           'sandbox_policy': {'type': sandbox.get('type')} if isinstance(sandbox, dict) else None}
            if data.get('type') == 'custom_tool_call' and data.get('name') in {'exec', 'functions.exec'}:
                calls[data.get('call_id', '')] = data.get('input', '')
                contexts[data.get('call_id', '')] = context.copy()
            if data.get('type') != 'custom_tool_call_output':
                continue
            output = data.get('output', [])
            if isinstance(output, str):
                output = [{'text': output}]
            refusal = next((block.get('text', '') for block in output if isinstance(block, dict)
                and block.get('text', '').startswith('Script error:\nexec_command failed: CreateProcess')
                and 'blocked by policy' in block.get('text', '')), None)
            if refusal:
                call_id = data.get('call_id', '')
                found.append({'at': event.get('timestamp'), 'call_id': call_id,
                              'command': literal_command(calls.get(call_id, '')),
                              'historical_turn_context': contexts.get(call_id, {}),
                              'invocation': invocation_summary(calls.get(call_id, ''))})
    return found


def check_rules(executable: Path, rules: list[Path], shell: Path, command: str) -> dict[str, Any]:
    if executable.name.lower() != 'codex.exe' or not executable.is_file():
        return {'status': 'official_cli_not_found'}
    if not rules:
        return {'status': 'no_local_rule_files'}
    argv = [str(executable), 'execpolicy', 'check']
    for rule in rules:
        argv += ['--rules', str(rule)]
    # Everything after -- is input DATA for the policy evaluator, not a command to run.
    argv += ['--', str(shell), '-Command', command]
    try:
        result = subprocess.run(argv, shell=False, capture_output=True, timeout=15, encoding='utf-8')
        if result.returncode:
            return {'status': 'check_failed', 'exit_code': result.returncode}
        data = json.loads(result.stdout)
        matched = data.get('matchedRules', [])
        return {'status': 'checked', 'decision': data.get('decision', 'no_match'),
                'matched_count': len(matched), 'scope': 'supplied_local_rule_files_only'}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {'status': 'check_unavailable'}


def nearby_logs(path: Path, event: dict[str, Any]) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    epoch = int(datetime.fromisoformat(event['at'].replace('Z', '+00:00')).timestamp())
    db = read_db(path)
    try:
        rows = db.execute('''SELECT id,ts,level,target,feedback_log_body FROM logs
            WHERE ts BETWEEN ? AND ? AND (target LIKE '%policy%' OR target LIKE '%approv%'
                OR target LIKE '%guardian%' OR target LIKE '%sandbox%' OR target='codex_core::tools::parallel')
            LIMIT 200''', (epoch-3, epoch+3)).fetchall()
        result = []
        for row in rows:
            body = row['feedback_log_body'] or ''
            exact = event['call_id'] in body
            if not exact and row['target'] == 'codex_core::tools::parallel':
                continue
            # Never export raw body: tool calls, HTTP headers and tokens can appear in logs.
            signals = [value for value in ('blocked by policy', 'approval required by policy rule',
                       'rejected by execpolicy', 'approval_policy=Never', 'sandbox_policy=DangerFullAccess') if value in body]
            result.append({'id': row['id'], 'ts': row['ts'], 'level': row['level'], 'target': row['target'],
                           'same_call_id': exact, 'signals': signals})
        return result
    finally:
        db.close()


def build_report(home: Path, project: Path, thread_id: str, cli: Path, shell: Path) -> dict[str, Any]:
    db = read_db(home / 'state_5.sqlite')
    try:
        row = db.execute('SELECT id,rollout_path,sandbox_policy,approval_mode,cli_version FROM threads WHERE id=?',
                         (thread_id,)).fetchone()
        if row is None:
            raise ValueError('Task not present in the selected local database')
        task = {key: row[key] for key in ('id', 'sandbox_policy', 'approval_mode', 'cli_version')}
        events = refusals(Path(row['rollout_path']))
    finally:
        db.close()
    rules = sorted((home / 'rules').glob('*.rules')) + sorted((project / '.codex/rules').glob('*.rules'))
    counts: Counter[str] = Counter()
    for file in rules:
        counts.update(re.findall(r'\bdecision\s*=\s*[\"\'](allow|prompt|forbidden)[\"\']', file.read_text(encoding='utf-8')))
    results = []
    for event in events[-10:]:
        command = event['command']
        entry = {key: event[key] for key in ('at', 'call_id', 'historical_turn_context', 'invocation')}
        entry['failure'] = 'CreateProcess rejected: blocked by policy'
        entry['command_recovered'] = command is not None
        if command is not None:
            entry['command_sha256'] = hashlib.sha256(command.encode()).hexdigest()
            entry['command_chars'] = len(command)
            entry['features'] = command_features(command)
            entry['local_rule_check'] = check_rules(cli, rules, shell, command)
        entry['logs'] = nearby_logs(home / 'logs_2.sqlite', event)
        results.append(entry)
    return {'created_at': datetime.now(timezone.utc).isoformat(), 'task': task,
            'config_defaults_not_effective_session': config_summary(home / 'config.toml', project),
            'local_rules': {'files': [str(p) for p in rules], 'explicit_decision_counts': dict(counts),
                            'sha256': [hashlib.sha256(p.read_bytes()).hexdigest() for p in rules]},
            'refusals': results, 'shell_for_offline_check': str(shell),
            'limits': ['No recorded command was executed. No permissions/configuration were changed.',
                       'No matching local rule does not imply execution is allowed by other enforcement layers.',
                       'Task database shows current stored settings, not necessarily historical settings for every event.',
                       'Historical turn context is reported when present; exact executed argv and historical runtime binary remain unproven.',
                       'Log lookup is bounded to 3 seconds around refusal, selected targets only; absent evidence is not proof of no review.',
                       'No credentials, raw scripts, raw rule patterns or raw logs are exported.'],
            'root_cause': 'Execution policy rejected process creation; specific rule/reviewer reason remains unproven.'}


def markdown(report: dict[str, Any]) -> str:
    lines = ['# Codex 执行拦截探针报告', '',
        '只读检查和离线规则判断；未重启服务、未执行被拒绝命令、未调整权限。', '',
        '## 当前任务配置', '',
        f"- sandbox_policy：`{report['task']['sandbox_policy']}`",
        f"- approval_mode：`{report['task']['approval_mode']}`",
        f"- CLI版本：`{report['task']['cli_version']}`", '',
        '## 历史拦截与本地规则离线检查', '',
        '| UTC时间 | Stop-Process | Start-Process | HTTP URL数 | 本地规则检查 | 命中数 |',
        '|---|---|---|---|---|---|']
    for event in report['refusals']:
        features, check = event.get('features', {}), event.get('local_rule_check', {})
        lines.append(f"| {event['at']} | {features.get('Stop-Process')} | {features.get('Start-Process')} | {features.get('http_url_count')} | {check.get('decision', check.get('status', 'not_checked'))} | {check.get('matched_count', '-')} |")
    lines += ['', '## 结论与边界', '',
        '- 拒绝发生在工具创建进程之前，不能归因为工作台Python异常。',
        '- `no_match`仅表示本次提供的本地规则没有命中，不表示完整运行策略允许。',
        '- 现有日志未提供可归因的具体规则或审查理由，不能断言由自动审批、某个命令或Windows策略造成。',
        '- 即使多次命令共同包含Start-Process，也只是相关性，不是因果证明。',
        '- HTTP URL只记录数量和本机/其他类型；文本共现不是同一解析片段命中的证明。历史turn_context可补充当时权限，但完整argv与历史运行时仍需另证。',
        '- 未导出密钥、完整命令、规则正文或原始日志；详细时间、调用编号和日志行编号见同目录JSON。', '',
        '[OpenAI规则检查文档](https://learn.chatgpt.com/docs/agent-configuration/rules)',
        '[OpenAI审批说明](https://learn.chatgpt.com/docs/agent-approvals-security)', '']
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home()/'.codex')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--thread-id', required=True)
    parser.add_argument('--codex-exe', required=True, type=Path)
    parser.add_argument('--shell', required=True, type=Path, help='Shell path from the observed rejected invocation; not executed')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = build_report(args.home.resolve(), args.project.resolve(), args.thread_id, args.codex_exe.resolve(), args.shell.resolve())
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.output/'report.md').write_text(markdown(report), encoding='utf-8')
    print(json.dumps({'refusals': len(report['refusals']), 'report': str(args.output/'report.md'),
                      'checks': [r.get('local_rule_check') for r in report['refusals']]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
