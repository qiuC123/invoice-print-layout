'use strict';
// Background recognition never owns a modal or disables ordinary editing.
(() => {
  let timer = null, reading = false, rerun = false, scope = '', failures = 0;
  const notified = new Set();
  const available = () => state.background_intake_ready === true;
  async function refresh(projectId = currentInvoiceProjectId()) {
    if (!available()) return;
    scope = projectId || '';
    if (reading) {rerun = true; return;}
    const requestedScope = scope; reading = true; clearTimeout(timer);
    try {
      const response = await fetch('/api/intake/progress?' + new URLSearchParams({project: requestedScope}));
      const result = await response.json();
      if (!response.ok) throw Error(result.error || '无法读取后台识别进度');
      failures = 0;
      const finished = result.batches.filter(job => job.state === 'finished' && !notified.has(job.batch));
      finished.forEach(job => notified.add(job.batch));
      document.dispatchEvent(new CustomEvent('intake-progress', {detail: {...result, projectId: requestedScope, finished}}));
      if (result.batches.some(job => job.state !== 'finished')) timer = setTimeout(() => refresh(scope), 750);
    } catch (error) {
      document.dispatchEvent(new CustomEvent('intake-progress-error', {detail: {projectId: requestedScope, message: error.message}}));
      if (++failures <= 2) timer = setTimeout(() => refresh(scope), 1500);
    } finally {reading = false; if (rerun) {rerun = false; queueMicrotask(() => refresh(scope));}}
  }
  async function enqueue(batch, ids, projectId) {
    const job = await api('intake/enqueue', {batch, ids});
    void refresh(projectId);
    return job;
  }
  async function resume(projectId) {
    const response = await fetch('/api/intake/progress?' + new URLSearchParams({project: projectId || ''}));
    const result = await response.json();
    if (!response.ok) throw Error(result.error || '无法读取待继续的材料');
    // The user explicitly resumes saved rows. A page load alone never enqueues.
    for (let index = 0; index < result.pending.length; index += 50) {
      await enqueue(crypto.randomUUID(), result.pending.slice(index, index + 50).map(entry => entry.id), projectId);
    }
    void refresh(projectId);
  }
  function render(container, result) {
    const active = result.batches.filter(job => job.state !== 'finished');
    const jobs = active.length ? active : result.batches.slice(-1);
    const total = jobs.reduce((sum, job) => sum + job.total, 0);
    const done = jobs.reduce((sum, job) => sum + job.completed + job.failed, 0);
    const failed = jobs.reduce((sum, job) => sum + job.failed, 0);
    const scanError = jobs.some(job => job.scan_error);
    container.hidden = !total && !result.pending_count;
    container.replaceChildren();
    if (total) {
      const text = document.createElement('span'); text.setAttribute('role', 'status');
      text.textContent = active.length ? `识别中 ${done}/${total} · 已保存，可关闭窗口或编辑其他材料。` :
        `识别完成 ${done}/${total}${failed ? ` · ${failed}份未完成，可逐份重试` : ''}${scanError ? ' · 请刷新建议组合' : ''}。`;
      container.append(text);
    }
    if (result.pending_count) {
      const button = document.createElement('button'); button.type = 'button'; button.dataset.resumeIntake = '';
      button.textContent = `继续识别未完成的 ${result.pending_count} 份`;
      button.onclick = () => act(async () => {
        button.disabled = true;
        try {await resume(result.projectId);} finally {button.disabled = false;}
      });
      container.append(button);
    }
  }
  window.invoiceIntakeProgress = {available, enqueue, refresh, render};
})();
