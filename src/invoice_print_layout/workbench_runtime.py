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
CAPABILITIES = ('mail_search', 'category_suggestion', 'automatic_category', 'automatic_rides', 'logistics_projects', 'payment_only_four_up',
                'unified_intake', 'expense_review', 'report_snapshot', 'graceful_shutdown', 'photo_management')


def source_build_id(package: Path | None = None) -> str:
    """Fingerprint backend source, including uncommitted edits; never use live git HEAD."""
    package = package or Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(p for p in package.rglob('*') if p.suffix in {'.py', '.mjs', '.js', '.html', '.css'}):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def identity(workspace: Path, build_id: str) -> dict[str, Any]:
    canonical = os.path.normcase(str(workspace.resolve()))
    return {'app_id': APP_ID, 'workspace_id': hashlib.sha256(canonical.encode()).hexdigest(),
            'build_id': build_id, 'capabilities': list(CAPABILITIES)}


def runtime_info(workspace: Path, startup_build: str) -> dict[str, Any]:
    return {**identity(workspace, startup_build),
            'started_at': datetime.now(timezone.utc).isoformat(), 'ready': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(identity(args.workspace, source_build_id())))


if __name__ == '__main__':
    main()
