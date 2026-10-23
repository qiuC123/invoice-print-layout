"""Read-only runtime identity shared by the launcher and HTTP server."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

APP_ID = 'invoice-print-layout.workbench'
INVOICE_APP_ID = 'invoice-print-layout.invoice-assistant'
APP_MODES = ('workbench', 'invoice')
CAPABILITIES = ('mail_search', 'category_suggestion', 'automatic_category', 'automatic_rides', 'logistics_projects', 'payment_only_four_up',
                'unified_intake', 'expense_review', 'report_snapshot', 'graceful_shutdown', 'photo_management')


def source_build_id(package: Path | None = None) -> str:
    """Fingerprint backend source, including uncommitted edits; never use live git HEAD."""
    package = package or Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(p for p in package.rglob('*') if p.suffix in {'.py', '.mjs', '.js', '.html', '.css', '.ps1'}):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def identity(workspace: Path, build_id: str, mode: str = 'workbench') -> dict[str, Any]:
    if mode not in APP_MODES:
        raise ValueError('Unsupported application mode')
    canonical = os.path.normcase(str(workspace.resolve()))
    capabilities = list(CAPABILITIES)
    if mode == 'invoice':
        capabilities = [item for item in capabilities if item not in {'logistics_projects', 'photo_management'}]
        capabilities.append('expense_projects')
    return {'app_id': INVOICE_APP_ID if mode == 'invoice' else APP_ID, 'app_mode': mode,
            'workspace_id': hashlib.sha256(canonical.encode()).hexdigest(),
            'build_id': build_id, 'capabilities': capabilities}


def runtime_info(workspace: Path, startup_build: str, mode: str = 'workbench') -> dict[str, Any]:
    return {**identity(workspace, startup_build, mode),
            'started_at': datetime.now(timezone.utc).isoformat(), 'ready': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--mode', choices=APP_MODES, default='workbench')
    args = parser.parse_args()
    print(json.dumps(identity(args.workspace, source_build_id(), args.mode)))


if __name__ == '__main__':
    main()
