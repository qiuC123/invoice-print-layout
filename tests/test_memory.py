from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from invoice_print_layout.memory import refresh_memory_status
from invoice_print_layout.storage import append_history, ensure_workspace


def _make_bot_batch(root: Path, batch_id: str, created_at: str) -> Path:
    batch = root / "机器人收件箱" / "已处理" / batch_id
    (batch / "files").mkdir(parents=True)
    (batch / "files" / "source.pdf").write_bytes(b"example")
    (batch / "metadata.json").write_text(
        json.dumps({"created_at": created_at}),
        encoding="utf-8",
    )
    return batch


def test_memory_summary_backfills_legacy_history_and_only_lists_cleanup(
    tmp_path: Path,
) -> None:
    paths = ensure_workspace(tmp_path / "workspace")
    archive = paths.archived / "2026-08-01_滴滴_12.30元_张三"
    archive.mkdir()
    (archive / "原始电子发票.pdf").write_bytes(b"invoice")
    append_history(
        paths.history,
        {
            "created_at": "2026-08-01T12:00:00",
            "output": str(paths.completed / "2026-08-01_滴滴_12.30元_张三.pdf"),
            "archive": str(archive),
            "invoice_hash": "invoice-hash",
            "trip_hash": "trip-hash",
        },
    )
    old_batch = _make_bot_batch(
        paths.root,
        "old",
        "2026-08-20T12:00:00",
    )
    new_batch = _make_bot_batch(
        paths.root,
        "new",
        "2026-09-02T12:00:00",
    )

    status = refresh_memory_status(paths, now=datetime(2026, 9, 4, 12, 0, 0))

    assert status.record_count == 1
    assert status.unresolved_count == 0
    assert status.total_amount == Decimal("12.30")
    assert [item.path for item in status.cleanup_candidates] == [old_batch]
    assert old_batch.exists()
    assert new_batch.exists()
    assert (archive / "原始电子发票.pdf").exists()
    content = status.summary_path.read_text(encoding="utf-8")
    assert "12.30 元" in content
    assert "本次只列出候选，没有删除任何文件" in content


def test_memory_summary_uses_structured_history_fields(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / "workspace")
    append_history(
        paths.history,
        {
            "record_version": 2,
            "created_at": "2026-09-04T12:00:00",
            "invoice_date": "2026-09-03",
            "category": "打车",
            "provider": "曹操",
            "amount": "52.14",
            "person_name": "李四",
            "output": str(paths.completed / "opaque-name.pdf"),
        },
    )

    status = refresh_memory_status(paths, now=datetime(2026, 9, 4, 12, 0, 0))

    assert status.record_count == 1
    assert status.unresolved_count == 0
    content = status.summary_path.read_text(encoding="utf-8")
    assert "| 2026-09-04T12:00:00 | 2026-09-03 | 打车 | 曹操 | 52.14 元 | 李四 |" in content
