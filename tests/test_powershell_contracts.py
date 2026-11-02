"""Detect the omitted module before execution; retain the same serial rule locally and in CI."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.powershell_contracts import calls_powershell, require_powershell_markers, pytest_runtest_setup


@pytest.mark.parametrize('source', [
    "shell = shutil.which('powershell.exe')",
    "shell = which(cmd='pwsh')",
    "subprocess.run(['powershell.exe', '-File', 'harness.ps1'])",
    r"subprocess.Popen(args=[r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'])",
    "run('pwsh -File harness.ps1', shell=True)",
])
def test_host_resolution_and_literal_shells_are_detected(source):
    assert calls_powershell(source)


@pytest.mark.parametrize('source', [
    "source = 'subprocess.run([\"powershell.exe\"])'",
    "subprocess.run(['git', 'ls-files', '*.ps1'])",
    "shutil.which('node')",
])
def test_static_audits_and_other_processes_are_not_shell_launches(source):
    assert not calls_powershell(source)


def test_pre_rework_invoice_assistant_omission_is_rejected(tmp_path):
    source = Path(__file__).with_name('test_invoice_assistant.py').read_text(encoding='utf-8')
    path = tmp_path / 'test_invoice_assistant.py'
    path.write_text(source.replace('pytestmark = pytest.mark.powershell', ''), encoding='utf-8')
    item = SimpleNamespace(path=path, get_closest_marker=lambda name: None)
    with pytest.raises(pytest.UsageError, match='test_invoice_assistant.py'):
        require_powershell_markers([item])
    item.get_closest_marker = lambda name: object()
    require_powershell_markers([item])


def test_marked_contract_is_rejected_in_an_xdist_worker():
    item = SimpleNamespace(get_closest_marker=lambda name: object(), config=SimpleNamespace(workerinput={}))
    with pytest.raises(pytest.fail.Exception, match='must run serially'):
        pytest_runtest_setup(item)
    item.config = SimpleNamespace()
    pytest_runtest_setup(item)
