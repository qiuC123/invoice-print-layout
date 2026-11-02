"""One marker selects all shell contracts; missing markers and parallel execution fail."""
import ast
import re

import pytest


SHELL = re.compile(r'(?:^|[\\/])(?:powershell|pwsh)(?:\.exe)?(?:$|[\s"])', re.I)


def calls_powershell(source: str) -> bool:
    # shortcut: literal shells and which() resolution; extend if dynamic shell construction is introduced.
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
        if name not in {'which', 'run', 'Popen', 'call', 'check_call', 'check_output'}:
            continue
        argument = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg in {'args', 'cmd'}), None)
        if name != 'which' and isinstance(argument, (ast.List, ast.Tuple)):
            argument = argument.elts[0] if argument.elts else None
        if argument is not None and any(isinstance(value, ast.Constant) and isinstance(value.value, str)
                                        and SHELL.search(value.value) for value in ast.walk(argument)):
            return True
    return False


def require_powershell_markers(items):
    modules, missing = {}, set()
    for item in items:
        if item.path.suffix != '.py':
            continue
        if item.path not in modules:
            modules[item.path] = calls_powershell(item.path.read_text(encoding='utf-8-sig'))
        if modules[item.path] and not item.get_closest_marker('powershell'):
            missing.add(str(item.path))
    if missing:
        raise pytest.UsageError('PowerShell modules need pytestmark = pytest.mark.powershell; '
                                'run them with -m powershell -n 0: ' + ', '.join(sorted(missing)))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(items):
    # Check before -m deselection, so the parallel phase cannot silently hide an omission.
    require_powershell_markers(items)
    return (yield)


def pytest_runtest_setup(item):
    if item.get_closest_marker('powershell') and hasattr(item.config, 'workerinput'):
        pytest.fail('PowerShell contracts must run serially: use -m powershell -n 0', pytrace=False)
