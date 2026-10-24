"""Optional private buyer profile checks. No profile means no checks or issues."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any
import unicodedata


def identity(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).upper()


def read_profile(root: Path) -> dict[str, list[str]] | None:
    try:
        value = json.loads((root / 'company-profile.json').read_text(encoding='utf-8'))
        if not isinstance(value, dict) or any(not isinstance(value.get(key, []), list) or
                any(not isinstance(entry, str) or len(entry) > 200 for entry in value.get(key, [])) for key in ('names', 'tax_ids')):
            return None
        result = {key: [identity(entry) for entry in value.get(key, []) if identity(entry)] for key in ('names', 'tax_ids')}
        return result if any(result.values()) else None
    except (OSError, ValueError):
        return None


def check(profile: dict[str, list[str]] | None, name: str, fingerprint: str) -> dict[str, Any] | None:
    if not profile:
        return None
    mismatches = []
    if name and profile['names'] and identity(name) not in profile['names']:
        mismatches.append('name')
    hashes = {'sha256:' + hashlib.sha256(identity(value).encode()).hexdigest() for value in profile['tax_ids']}
    if fingerprint and hashes and fingerprint not in hashes:
        mismatches.append('tax_id')
    read = [key for key, value in [('name', name), ('tax_id', fingerprint)] if value]
    required = [key for key, value in [('name', profile['names']), ('tax_id', profile['tax_ids'])] if value]
    return {'status': 'mismatch' if mismatches else 'unreadable' if not read else
                     'match' if all(key in read for key in required) else 'partial',
            'buyer_name': name, 'name_read': bool(name), 'tax_id_read': bool(fingerprint),
            'mismatches': mismatches}


def check_pdf(root: Path, page: Any) -> dict[str, Any] | None:
    profile = read_profile(root)
    if not profile:
        return None
    from invoice_print_layout.agent_evidence import _words, _actor, _buyer_fingerprint
    words = _words(page)
    buyer = _actor(page, words, '购买方', 1)
    return check(profile, buyer['value'] if buyer else '', _buyer_fingerprint(words, buyer))


def check_screenshot(root: Path, text: str) -> dict[str, Any] | None:
    names = set(re.findall(r'^\s*购买方名称\s*[:：]\s*([^\n]+)', text, flags=re.M))
    name = names.pop().strip() if len(names) == 1 else ''
    ids = set(re.findall(r'^\s*购买方(?:税号|纳税人识别号|统一社会信用代码)\s*[:：]\s*([A-Z0-9]{15,20})\s*$', text, flags=re.M|re.I)) if name else set()
    fingerprint = 'sha256:' + hashlib.sha256(identity(ids.pop()).encode()).hexdigest() if len(ids) == 1 else ''
    return check(read_profile(root), name, fingerprint)
