"""Bounded amount windows with a separate, complete-project competition audit.

Task scope limits what may be proposed, never what may contradict uniqueness.
Positive amount bounds safely exclude oversized proofs; unknown records stay
potential competitors instead of silently disappearing from the window.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from invoice_print_layout.workbench import amount_cents


MAX_POOL = 20
MAX_ENUMERATION = 8192
MAX_SUBSETS = 128
INVALID_PAYMENT = frozenset({'failed', 'refunded', 'unpaid', 'cancelled', 'canceled'})


def known_cents(entry: dict[str, Any]) -> int | None:
    try:
        value = amount_cents((entry.get('fields') or {}).get('amount') or '')
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None


def failed_payment(entry: dict[str, Any]) -> bool:
    return entry.get('role') == 'payment' and (entry.get('fields') or {}).get('payment_status') in INVALID_PAYMENT


@dataclass
class EnumerationBudget:
    remaining: int = MAX_ENUMERATION
    exhausted: bool = False

    def spend(self) -> bool:
        self.remaining -= 1
        if self.remaining < 0:
            self.exhausted = True
            return False
        return True


def amount_groups(rows: list[dict[str, Any]], target: int,
                  cents: Callable[[dict[str, Any]], int], budget: EnumerationBudget,
                  *, minimum: int = 1, maximum: int = MAX_POOL) -> list[list[dict[str, Any]]] | None:
    """Enumerate whole positive-sum groups or report truncation, never a prefix.

    Each anchor uses its exact amount upper bound and an already evidence-filtered
    pool. A full-set sum is unique for positive values, including 9+ part payments.
    If any bound is hit the caller must decline automatic proposals for that scan.
    """
    pool = [(row, cents(row)) for row in rows if 0 < cents(row) <= target]
    if len(pool) > MAX_POOL:
        budget.exhausted = True
        return None
    if not pool or maximum < minimum:
        return []
    pool.sort(key=lambda pair: (-pair[1], str(pair[0]['id'])))
    if sum(value for _, value in pool) == target and minimum <= len(pool) <= maximum:
        return [[row for row, _ in pool]] if budget.spend() else None
    suffix = [0] * (len(pool) + 1)
    for index in range(len(pool) - 1, -1, -1):
        suffix[index] = suffix[index + 1] + pool[index][1]
    groups: list[list[dict[str, Any]]] = []

    def search(index: int, left: int, chosen: list[dict[str, Any]]) -> None:
        if budget.exhausted or not budget.spend():
            return
        if left == 0:
            if len(chosen) >= minimum:
                groups.append(list(chosen))
                if len(groups) > MAX_SUBSETS:
                    budget.exhausted = True
            return
        if index == len(pool) or len(chosen) >= maximum or suffix[index] < left:
            return
        row, value = pool[index]
        if value <= left:
            search(index + 1, left - value, [*chosen, row])
        search(index + 1, left, chosen)

    search(0, target, [])
    return None if budget.exhausted else groups


def outside_competitor(row: dict[str, Any], entries: list[dict[str, Any]],
                       scope: set[str] | None, usable: set[str], *, payment_context: bool = False,
                       all_payment_alternatives: bool = False,
                       budget: EnumerationBudget | None = None) -> bool:
    """Audit every omitted source, including analysed records outside the job.

    This intentionally stays conservative for same/unknown amounts. A model may
    not upgrade the resulting ambiguity. Known oversized positive proofs cannot
    participate in a conserved group; confirmed failed payments cannot fund it.
    """
    selected = {key.removeprefix('q:') for key in row['voucher_keys']} | set(row['payment_ids']) | set(row.get('support_ids', []))
    target = int(row['amount_cents'])
    if row['payment_ids'] and all_payment_alternatives:
        # Deterministic paths must not hide an equal-sum alternative merely
        # because a different shop failed their stricter local relation rule.
        # This still permits separate scoped expenses with different totals.
        payments = [e for e in entries if e.get('role') == 'payment' and not failed_payment(e)
                    and known_cents(e) is not None]
        groups = amount_groups(payments, target, lambda e: int(known_cents(e) or 0),
                               budget if budget is not None else EnumerationBudget())
        if groups is None or any({e['id'] for e in group} != set(row['payment_ids']) for group in groups):
            return True
    for entry in entries:
        ident = str(entry['id'])
        if ident in selected or failed_payment(entry):
            continue
        external = scope is not None and ident not in scope
        blocked = ident not in usable
        if not external and not blocked:
            continue
        role = entry.get('role')
        value = known_cents(entry)
        # Any explicit positive amount above the target is an arithmetic exclusion.
        if value is not None and value > target:
            continue
        if role == 'payment' and (row['payment_ids'] or payment_context):
            return True
        if role in {'purchase', 'order', 'trip', 'trip_sheet', 'detail'} and row.get('support_ids') and (
                value is None or value == target):
            return True
        # Counted vouchers outside scope must not become invisible to selected
        # payments or an order, or to a stand-alone same-amount service invoice.
        if role in {'invoice', 'receipt'} and row['voucher_keys'] and (
                row['payment_ids'] or row.get('support_ids') or value is None or value == target):
            return True
    return False
