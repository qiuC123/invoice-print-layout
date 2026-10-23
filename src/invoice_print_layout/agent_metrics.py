"""Small, content-free measurements for the existing durable agent loop.

Stage times are observations, not budgets.  The loop's existing elapsed counter
is the single activity total; stage totals must never be added to it.
"""
from __future__ import annotations

import math
from typing import Any

VERSION = 1
MAX_STEPS = 256


def initial() -> dict[str, Any]:
    return {'version': VERSION, 'stage_times_are_inclusive': True,
            'records': 0, 'stages': {}, 'steps': [],
            'user_wait_seconds': 0.0, 'waiting_since': None}


def transition(job: dict[str, Any], previous: str, now: float) -> None:
    """Observe only user-input pauses, including answer/cancel/recovery paths."""
    metrics = job.setdefault('timings', initial())
    if previous == 'needs_input' and job['state'] != 'needs_input':
        since = metrics.get('waiting_since')
        if isinstance(since, (float, int)):
            metrics['user_wait_seconds'] = metrics.get('user_wait_seconds', 0.0) + max(0.0, now - since)
        metrics['waiting_since'] = None
    if job['state'] == 'needs_input' and previous != 'needs_input':
        metrics['waiting_since'] = now


def record(job: dict[str, Any], *, stage: str, seconds: float, outcome: str,
           tool: str | None = None, cache: str | None = None,
           model_calls: int = 0) -> None:
    """Append one observation; never touch action/effect/confirmation receipts."""
    if not math.isfinite(seconds) or seconds < 0:
        return
    metrics = job.setdefault('timings', initial())
    ident = stage + (':' + tool if tool else '')
    summary = metrics['stages'].setdefault(ident, {
        'stage': stage, 'tool': tool, 'count': 0, 'seconds': 0.0,
        'errors': 0, 'cache_hits': 0, 'cache_misses': 0, 'model_calls': 0})
    summary['count'] += 1
    summary['seconds'] += seconds
    summary['errors'] += int(outcome != 'ok')
    summary['cache_hits'] += int(cache == 'hit')
    summary['cache_misses'] += int(cache == 'miss')
    summary['model_calls'] += max(0, model_calls)
    metrics['records'] += 1
    step = {'sequence': metrics['records'], 'stage': stage, 'seconds': seconds,
            'outcome': outcome, 'tool': tool, 'cache': cache, 'model_calls': max(0, model_calls)}
    metrics['steps'] = [*metrics['steps'][-(MAX_STEPS - 1):], step]


def snapshot(job: dict[str, Any], now: float) -> dict[str, Any]:
    """Decorate a decoded JSON job without writing or changing its old counters.

    Existing jobs remain readable.  ``active_seconds`` deliberately uses the
    pre-existing budget counter, so confirmation pauses never inflate it.  After
    an unclean restart that counter retains the existing recovery accounting.
    ``inactive_seconds`` also includes queue/startup time, unlike user waiting.
    """
    metrics = job.setdefault('timings', initial())
    running = job.get('run_started') if job.get('state') == 'running' else None
    active = float(job.get('elapsed', 0.0)) + (max(0.0, now - running) if running else 0.0)
    accounted_at = now if job.get('state') in {'running', 'needs_input'} else float(job.get('updated_at', now))
    wall = max(0.0, accounted_at - float(job.get('created_at', accounted_at)))
    waiting = float(metrics.get('user_wait_seconds', 0.0))
    since = metrics.get('waiting_since')
    if job.get('state') == 'needs_input' and isinstance(since, (float, int)):
        waiting += max(0.0, now - since)
    metrics.update(active_seconds=active, active_source='job_elapsed',
                   wall_seconds=wall, inactive_seconds=max(0.0, wall - active),
                   current_user_wait_seconds=waiting)
    return job
