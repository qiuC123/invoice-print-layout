from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from invoice_print_layout.cli import app
from invoice_print_layout.storage import read_person_name


def test_process_command_creates_workspace_and_prompts_for_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    result = CliRunner().invoke(
        app,
        ["process", "--workspace", str(workspace), "--no-open-completed"],
        input="Test User\n",
    )

    assert result.exit_code == 0
    assert read_person_name(workspace / "config.toml") == "Test User"
    assert (workspace / "待处理").is_dir()
    assert (workspace / "已完成").is_dir()
    assert (workspace / "本次处理结果.txt").exists()
