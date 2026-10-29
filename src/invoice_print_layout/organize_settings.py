"""Workspace-wide organizer preferences shared by uploads and mail intake."""
from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any

from invoice_print_layout.reliability import save_json

_WRITE_LOCK = threading.RLock()


class OrganizeSettings:
    def __init__(self, root: Path) -> None:
        self.path = root / 'organize-settings.json'

    def read(self) -> dict[str, bool]:
        if not self.path.exists():
            return {'auto_after_upload': True, 'use_model': True}
        if self.path.stat().st_size > 65536:
            raise ValueError('整理设置无效')
        value = json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or value.get('version') != 1 or
                any(type(value.get(key)) is not bool for key in ('auto_after_upload', 'use_model'))):
            raise ValueError('整理设置无效')
        return {key: value[key] for key in ('auto_after_upload', 'use_model')}

    def update(self, body: dict[str, Any]) -> dict[str, bool]:
        if (not isinstance(body, dict) or set(body) - {'auto_after_upload', 'use_model'} or
                any(type(value) is not bool for value in body.values())):
            raise ValueError('整理开关必须是布尔值')
        with _WRITE_LOCK:
            value = {**self.read(), **body}
            save_json(self.path, {'version': 1, **value})
            return value

    def effective_model(self, requested: Any, available: bool) -> bool:
        if type(requested) is not bool:
            raise ValueError('模型辅助选项必须是布尔值')
        return requested and self.read()['use_model'] and available
