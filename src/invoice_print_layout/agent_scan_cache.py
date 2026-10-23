"""Bounded per-worker projections; original bytes are checked on every reuse."""
from __future__ import annotations

from collections import OrderedDict
import copy
import hashlib
from pathlib import Path
from typing import Any

from invoice_print_layout.agent_jobs import digest
from invoice_print_layout.material_matching import MaterialMatching


class AgentMaterialMatching(MaterialMatching):
    """Keep the manual matcher's rules, avoiding repeated native seller parsing."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._card_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.card_cache_hits = 0
        self.card_cache_misses = 0

    def _queue_card(self, entry: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(entry.get('path') or ''))
        if path.suffix.lower() != '.pdf':
            return super()._queue_card(entry)
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self.store.workspace.resolve()):
                return super()._queue_card(entry)
            actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if actual != entry.get('digest'):
                return super()._queue_card(entry)
            key = digest([str(resolved), actual, entry])
        except (OSError, ValueError, TypeError):
            return super()._queue_card(entry)
        prior = self._card_cache.get(key)
        if prior is not None:
            self.card_cache_hits += 1
            self._card_cache.move_to_end(key)
            return copy.deepcopy(prior)
        self.card_cache_misses += 1
        result = super()._queue_card(entry)
        self._card_cache[key] = copy.deepcopy(result)
        while len(self._card_cache) > 128:
            self._card_cache.popitem(last=False)
        return result
