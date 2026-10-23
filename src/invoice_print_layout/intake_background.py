"""One explicitly started, serial recognition worker per workbench process.

Saved intake rows are the recovery record. Constructing this service does not
start work, and a restart never calls an OCR engine or model on its own.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import queue
import threading
from typing import Any, Callable

from invoice_print_layout.intake_queue import IntakeQueue


class BackgroundIntake:
    def __init__(self, intake: IntakeQueue, run_lock: threading.Lock,
                 scan: Callable[[str], Any]):
        self.intake, self.run_lock, self.scan = intake, run_lock, scan
        self._lock = threading.Lock()
        self._work: queue.Queue[str | None] = queue.Queue()
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._owners: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._closed = False

    def enqueue(self, batch: Any, ids: Any, *, force_ai: Any = False) -> dict[str, Any]:
        if (not isinstance(batch, str) or not batch.strip() or len(batch) > 128 or
                not isinstance(ids, list) or not 1 <= len(ids) <= 50 or
                any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids)):
            raise ValueError('请选择1至50份不重复的已保存材料，并提供批次编号')
        if type(force_ai) is not bool:
            raise ValueError('AI辅助识别选项必须是布尔值')
        entries = [self.intake.get(key) for key in ids]
        projects = {entry.get('project_id', '') for entry in entries}
        if len(projects) != 1:
            raise ValueError('同一识别批次的材料须属于同一个项目')
        with self._lock:
            if self._closed:
                raise ValueError('识别服务正在关闭，已保存材料可在重启后继续')
            previous = self._jobs.get(batch)
            if previous:
                if set(previous['ids']) != set(ids) or previous['force_ai'] != force_ai:
                    raise ValueError('批次已存在，请刷新识别进度；本次没有重复执行')
                return deepcopy(previous)
            if any(key in self._owners for key in ids):
                raise ValueError('材料已在识别队列中，请查看原批次进度；本次没有重复执行')
            # Bound memory without ever evicting an unfinished job's identity.
            if len(self._jobs) >= 200:
                finished = next((key for key, value in self._jobs.items() if value['state'] == 'finished'), None)
                if finished is None:
                    raise ValueError('待识别批次较多，请等待已有批次结束')
                old = self._jobs.pop(finished)
                for key in old['ids']:
                    self._owners.pop(key, None)
            job = {'batch': batch, 'ids': list(ids), 'project_id': next(iter(projects)),
                   'force_ai': force_ai, 'total': len(ids), 'completed': 0, 'failed': 0,
                   'running': 0, 'state': 'queued', 'failures': [], 'scan_error': '', 'suggested': 0}
            self._jobs[batch] = job
            self._owners.update(dict.fromkeys(ids, batch))
            self._work.put(batch)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='invoice-intake', daemon=True)
                self._thread.start()
            return deepcopy(job)

    def progress(self, project_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            jobs = [deepcopy(job) for job in self._jobs.values()
                    if project_id is None or job['project_id'] == project_id]
            owned = set(self._owners)
        pending = [{'id': entry['id'], 'batch': entry.get('batch', '')}
                   for entry in self.intake.list()
                   if entry['id'] not in owned and entry['status'] == 'received' and not entry.get('item_id')
                   and (project_id is None or entry.get('project_id', '') == project_id)]
        return {'batches': jobs, 'pending': pending, 'pending_count': len(pending)}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            thread = self._thread
        self._work.put(None)
        if thread is not None:
            thread.join()

    def _run(self) -> None:
        while (batch := self._work.get()) is not None:
            with self._lock:
                if self._closed:
                    return  # Saved received rows remain available to the resume button.
                job = self._jobs[batch]
                job['state'] = 'running'
            for key in job['ids']:
                with self._lock:
                    if self._closed:
                        return
                try:
                    with self.run_lock:
                        with self._lock:
                            if self._closed:
                                return
                            job['running'] = 1
                        self.intake.prepare_batch_entry(key, force_ai=job['force_ai'])
                    with self._lock:
                        job['completed'] += 1
                except Exception:
                    # Continue the batch. Never expose original names, paths or
                    # provider error bodies in queue diagnostics.
                    with self._lock:
                        job['failed'] += 1
                        job['failures'].append({'id': key, 'message': '识别未完成，原件已保留，可逐份重试。'})
                finally:
                    with self._lock:
                        job['running'] = 0
            try:
                if job['project_id']:
                    result = self.scan(job['project_id'])
                    with self._lock:
                        job['suggested'] = result.get('suggested', 0)
            except Exception:
                with self._lock:
                    job['scan_error'] = '建议组合扫描未完成，请在材料合成台刷新建议。'
            finally:
                with self._lock:
                    job['state'] = 'finished'
