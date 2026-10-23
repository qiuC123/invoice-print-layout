"""Exercise the shipped early asset bootstrap without touching any ledger."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("scenario", [
    "single-failure", "permanent-failure", "delayed-success", "cascade-order",
    "manual-recovery", "cross-origin", "storage-denied", "theme-fallback",
    "theme-already-bound", "theme-invalid", "timeout-recovery",
])
def test_workbench_asset_recovery(scenario: str) -> None:
    node = os.environ.get("RUNTIME_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise the actual asset bootstrap")
    script = Path(__file__).with_name("workbench_asset_recovery.cjs")
    result = subprocess.run(
        [node, str(script), scenario], capture_output=True, text=True,
        encoding="utf-8", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {scenario}" in result.stdout
