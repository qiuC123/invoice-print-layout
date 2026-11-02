"""Local pytest guards for private task growth and Office process ownership."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SUFFIXES = {'.py', '.ps1', '.psm1', '.js', '.mjs', '.cjs', '.vbs', '.bat', '.cmd'}
OFFICE_COM = re.compile(r'\b(?:Excel|PowerPoint|Word)\.Application\b|\b(?:win32com|Dispatch\w*|GetActiveObject|GetObject)\b', re.I)
OFFICE_PROCESS = re.compile(r'(?<![\w$])(?:excel|powerpnt|winword)(?:\.exe)?(?![\w.])', re.I)
QUIT = re.compile(r'\.\s*Quit\s*\(', re.I)
QUIT_REASON = re.compile(r'(?:#|//|\').*office-quit-ok:\s*[^\W\d_]', re.I)


def check_task_size(path: Path) -> int | None:
    """Return UTF-8 file bytes, allowing public clones without private task state."""
    if not path.exists():
        return None
    size = path.stat().st_size
    assert size <= 20_000, (
        f'{path.name}: {size} bytes exceeds the 20,000-byte UTF-8 limit. '
        'Compress completed stages to one line each in 已完成索引; append their original '
        'text to TASK-archive-<year-month>.md. Move index lines from last month and '
        'earlier into the archive too.'
    )
    return size


def check_office_source(path: Path, *, label: str | None = None) -> None:
    """Check source text only; never launch Office or execute scanned commands."""
    source = path.read_text(encoding='utf-8-sig')
    name = label or path.name
    errors = []
    if OFFICE_COM.search(source):
        for number, line in enumerate(source.splitlines(), 1):
            if QUIT.search(line) and not QUIT_REASON.search(line):
                errors.append(
                    f'{name}:{number}: Office application Quit may close user documents. '
                    'Close only the document you opened. Only after proving the application '
                    'instance is independently owned, add a same-line office-quit-ok: comment '
                    'explaining that ownership; never approve an attached user instance.'
                )

    # shortcut: literal commands and simple PowerShell assignments; extend parsing if indirect command construction is introduced.
    logical = re.sub(r'(?:`|\\|\^)\r?\n|\|\s*\r?\n', lambda m: '| ' if m[0].startswith('|') else ' ', source)
    logical = re.sub(r'([\[(,])[^\S\n]*\n|\n\s*([\])])', lambda m: (m[1] or m[2]) + ' ', logical)
    named_variables: set[str] = set()
    for command in re.split(r'[\r\n;]+', logical):
        variables = {value.lower() for value in re.findall(r'\$\w+', command)}
        named = OFFICE_PROCESS.search(command) or bool(variables & named_variables)
        assignment = re.match(r'\s*(\$\w+)\s*=', command)
        if assignment:
            target = assignment[1].lower()
            value = command.split('=', 1)[1]
            references = {value.lower() for value in re.findall(r'\$\w+', value)}
            if (OFFICE_PROCESS.search(value) and re.search(r'\b(?:Get-Process|gps|ps)\b', value, re.I)) or references & named_variables:
                named_variables.add(target)
            else:
                named_variables.discard(target)
        if not named:
            continue
        taskkill = re.search(r'\btaskkill(?:\.exe)?\b', command, re.I) and re.search(r'/(?:im\b|fi\b.*imagename)', command, re.I)
        stop = re.search(r'\b(?:Stop-Process|kill|spps)\b', command, re.I)
        by_name = stop and (
            re.search(r'\b(?:Get-Process|gps|ps)\b', command, re.I)
            or re.search(r'-Name\b', command, re.I)
            or bool(variables & named_variables)
            or not re.search(r'-Id\b', command, re.I)
        )
        kill_method = re.search(r'\.\s*(?:Kill|Terminate)\s*\(', command, re.I)
        wmi = re.search(r'\bwmic\b.*\bterminate\b', command, re.I)
        if taskkill or by_name or kill_method or wmi:
            errors.append(
                f'{name}: Office process termination by name is forbidden (no exceptions). '
                'Record the PID of your own child when starting it, verify ownership, '
                'and terminate only that PID; do not enumerate or stop Office by name.'
            )
    assert not errors, '\n'.join(errors)


def test_private_task_size() -> None:
    if check_task_size(ROOT / 'TASK.md') is None:
        pytest.skip('Private TASK.md is gitignored and absent in public clones / hosted CI')


def test_tracked_scripts_preserve_user_office() -> None:
    tracked = subprocess.run(
        ['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True,
    ).stdout.decode('utf-8').split('\0')
    paths = {ROOT / name for name in tracked if Path(name).suffix.lower() in SCRIPT_SUFFIXES}
    paths.add(Path(__file__).resolve())
    errors = []
    for path in sorted(paths):
        try:
            check_office_source(path, label=path.relative_to(ROOT).as_posix())
        except AssertionError as error:
            errors.append(str(error))
    assert not errors, '\n'.join(errors)


def test_task_size_uses_bytes_and_reports_the_remedy(tmp_path: Path) -> None:
    path = tmp_path / 'TASK.md'
    assert check_task_size(path) is None
    path.write_bytes(b'x' * 20_000)
    assert check_task_size(path) == 20_000
    path.write_text('\u4e2d' * 6_667, encoding='utf-8')
    with pytest.raises(AssertionError, match='20001 bytes.*20,000-byte') as failure:
        check_task_size(path)
    for remedy in ('已完成索引', 'TASK-archive-<year-month>.md', 'last month'):
        assert remedy in str(failure.value)
    assert str(tmp_path) not in str(failure.value)


# Templates keep deliberately unsafe commands out of the tracked-source scan.
def sample(template: str) -> str:
    return template.format(office='Power' + 'Point', process='EX' + 'CEL', quit='Qu' + 'it')


@pytest.mark.parametrize('template', [
    '$app = New-Object -ComObject {office}.Application\n$app.{quit}()',
    'import win32com.client\napp.{quit}(False)',
    'app = Dispatch("Word.Application")\napp.{quit}()',
    '$app = New-Object -ComObject {office}.Application\n$app.{quit}() # office-quit-ok:',
    'taskkill /im {process}.exe /f',
    'TASKKILL.EXE /F /IM "{process}.EXE"',
    'subprocess.run(["taskkill", "/IM", "{process}.exe", "/F"])',
    'subprocess.run([\n "taskkill",\n "/IM",\n "{process}.exe",\n "/F"\n])',
    'Stop-Process -Name {process} -Force',
    'sToP-pRoCeSs -Force -Name "{process}.exe" # office-quit-ok: no exception',
    'Stop-Process {process}',
    'Get-Process {process} | Stop-Process',
    'Get-Process -Name "{process}" |\n Stop-Process -Force',
    'Get-Process {process} | ForEach-Object {{ Stop-Process -Id $_.Id }}',
    'Get-Process {process} | ForEach-Object {{ $_.Kill() }}',
    '$targets = Get-Process {process}\n$targets | Stop-Process',
    '$targets = Get-Process -Name {process}; Stop-Process -Id $targets.Id',
    '$targets = Get-Process {process}\n$copy = $targets\n$copy.Kill()',
    'wmic process where name="{process}.exe" call terminate',
    'Stop-Process `\n -Name {process}',
    'taskkill ^\n /IM {process}.exe',
    'taskkill /fi "IMAGENAME eq {process}.exe" /f',
    'gps {process} | kill',
])
def test_unsafe_office_samples_are_rejected(tmp_path: Path, template: str) -> None:
    path = tmp_path / 'unsafe.ps1'
    path.write_text(sample(template), encoding='utf-8')
    with pytest.raises(AssertionError, match='Close only the document|Record the PID') as failure:
        check_office_source(path)
    assert str(tmp_path) not in str(failure.value)


@pytest.mark.parametrize('process', ['EX' + 'CEL', 'POWER' + 'PNT', 'WIN' + 'WORD'])
@pytest.mark.parametrize('command', ['taskkill /im {}.exe /f', 'Stop-Process -Name {}', 'Get-Process {} | Stop-Process'])
def test_all_office_process_names_are_rejected(tmp_path: Path, process: str, command: str) -> None:
    path = tmp_path / 'unsafe.cmd'
    path.write_text(command.format(process.lower()), encoding='utf-8')
    with pytest.raises(AssertionError, match='no exceptions'):
        check_office_source(path)


@pytest.mark.parametrize('template', [
    '$app = New-Object -ComObject {office}.Application\n$doc.Close($false)',
    'import win32com.client\nbook.Close(False)',
    'taskkill /PID 123 /T /F',
    'subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"])',
    'Stop-Process -Id $child.Id',
    '$app = New-Object -ComObject Excel.Application\nif ($owned) {{ $app.{quit}() }} # office-quit-ok: new COM PID absent from pre-existing PID snapshot',
    'app.{quit}() // Non-Office application',
    'Get-Process {process} | Select-Object Id',
    '$prior = Get-Process {process}\nStop-Process -Id $child.Id',
    '$target = Get-Process {process}\n$target = $child\nStop-Process -Id $target.Id',
    'taskkill /im unrelated.exe /f',
])
def test_owned_children_and_document_close_are_allowed(tmp_path: Path, template: str) -> None:
    path = tmp_path / 'safe.ps1'
    path.write_text(sample(template), encoding='utf-8')
    check_office_source(path)


def test_real_excel_cleanup_without_ownership_comment_is_rejected(tmp_path: Path) -> None:
    source = ROOT / 'src/invoice_print_layout/report_excel_pdf.ps1'
    current = source.read_text(encoding='utf-8-sig')
    assert 'office-quit-ok:' in current
    check_office_source(source)
    old = tmp_path / source.name
    old.write_text(re.sub(r' # office-quit-ok:[^\n]*', '', current), encoding='utf-8')
    with pytest.raises(AssertionError, match=r'report_excel_pdf.ps1:\d+: Office application'):
        check_office_source(old)
