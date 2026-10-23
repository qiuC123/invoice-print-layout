(() => {
  'use strict';
  const PREFIX = 'invoice-manual-agent-v1:';
  const TERMINAL = new Set(['ready', 'needs_input', 'stopped', 'failed', 'cancelled']);
  const STATES = {queued:'等待整理', running:'正在整理', ready:'预览已准备', needs_input:'需要你补充', stopped:'已停止尝试', failed:'处理未完成', cancelled:'已取消'};
  let active = null;

  function stored(key) { try { return localStorage.getItem(PREFIX + key); } catch (_) { return null; } }
  function remember(key, value) { try { localStorage.setItem(PREFIX + key, value); } catch (_) { /* Session state still works when browser storage is unavailable. */ } }
  function automatic(projectId) { return !!projectId && stored('automatic:' + projectId) !== 'false'; }
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function money(cents) { return Number.isInteger(cents) ? (cents / 100).toFixed(2) + ' 元' : '金额待确认'; }

  function mount(options) {
    const {host, api, getProject} = options;
    if (!host || typeof api !== 'function' || typeof getProject !== 'function') throw new Error('自动整理缺少工作台接口');
    if (active) active.destroy();
    let project = '', generation = 0, revision = 0, jobs = [], rendered = '', reading = null;
    let modelKnown = false, modelAvailable = false, destroyed = false, refreshError = '', actionError = '';
    const watched = new Set(), notified = new Map(), jobLocks = new Set(), startLocks = new Map(), requests = new Map();
    const panel = node('details', undefined, 'manual-invoice-agent');
    const summary = node('summary', undefined, 'mia-summary');
    const heading = node('strong', '自动整理'), badge = node('span', '', 'mia-summary-status');
    summary.append(heading, badge); panel.append(summary);
    const body = node('div', undefined, 'mia-body'), controls = node('div', undefined, 'mia-controls');
    const automaticLabel = node('label', undefined, 'mia-check');
    const automaticInput = node('input'); automaticInput.type = 'checkbox';
    automaticLabel.append(automaticInput, node('span', '上传后自动整理'));
    const organize = node('button', '整理未处理材料'); organize.type = 'button';
    const modelLabel = node('label', undefined, 'mia-check mia-model');
    const modelInput = node('input'); modelInput.type = 'checkbox'; modelInput.disabled = true;
    modelLabel.append(modelInput, node('span', '使用已配置模型处理复杂问题'));
    controls.append(automaticLabel, organize, modelLabel);
    const hint = node('p', '整理结果进入待核对，原件保留。缺少依据时在这里补充事实。', 'mia-hint');
    const error = node('p', '', 'mia-error'); error.setAttribute('role', 'alert'); error.hidden = true;
    const pending = node('div', undefined, 'mia-pending-starts');
    const list = node('div', undefined, 'mia-jobs'); list.setAttribute('aria-live', 'polite');
    body.append(controls, hint, error, pending, list); panel.append(body); host.replaceChildren(panel);

    function current() { return String(getProject() || ''); }
    function displayError() {
      const message = actionError || refreshError;
      error.textContent = message; error.hidden = !message;
    }
    function actionFailure(reason) { actionError = reason && reason.message ? reason.message : String(reason); displayError(); }
    function requestRecords(projectId) {
      if (!requests.has(projectId)) {
        let value;
        try { value = JSON.parse(stored('requests:' + projectId) || '[]'); } catch (_) { value = []; }
        requests.set(projectId, Array.isArray(value) ? value.filter(row => row && row.projectId === projectId && typeof row.signature === 'string' && typeof row.key === 'string' && Array.isArray(row.ids)) : []);
      }
      return requests.get(projectId);
    }
    function saveRequests(projectId) {
      const rows = requestRecords(projectId);
      // Failed requests keep their exact identity. Only old, accepted requests can age out.
      while (rows.length > 12) { const index = rows.findIndex(row => row.accepted); if (index < 0) break; rows.splice(index, 1); }
      remember('requests:' + projectId, JSON.stringify(rows.map(({result, ...row}) => row)));
    }
    function updateControls() {
      automaticInput.checked = automatic(project);
      automaticInput.disabled = !project;
      modelInput.checked = !!project && modelAvailable && stored('model:' + project) !== 'false';
      modelInput.disabled = !project || !modelKnown || !modelAvailable;
      organize.disabled = !project || startLocks.has(project);
      if (!project) badge.textContent = '选择项目后可用';
      else {
        const waiting = jobs.filter(job => job.state === 'needs_input').length;
        const working = jobs.some(job => ['queued', 'running'].includes(job.state));
        badge.textContent = waiting ? waiting + ' 项待确认' : working ? '正在整理' : automatic(project) ? '上传后自动整理已开启' : '上传后由你手动整理';
      }
    }
    function setProject() {
      const next = current();
      if (next === project) return false;
      project = next; generation += 1; revision += 1; jobs = []; rendered = '';
      actionError = refreshError = ''; displayError(); updateControls(); render(); renderPending();
      return true;
    }
    function callback(name, value) {
      if (typeof options[name] !== 'function') return Promise.resolve();
      return Promise.resolve().then(() => {
        if (!destroyed && current() === value.projectId) return options[name](value);
      });
    }
    function resultSignature(job) {
      return JSON.stringify([(job.item_ids || []).slice().sort(), job.expenses || [], job.preview ? [job.preview.pdf, job.preview.xlsx] : null]);
    }
    function watchExisting(job) {
      if (watched.has(job.id)) return;
      watched.add(job.id);
      // Partial drafts already visible on initial load or before a retry are a
      // baseline, not newly written results requiring the parent's reload.
      if ((job.item_ids || []).length || job.preview) notified.set(job.id, resultSignature(job));
    }
    function resultChanged(job) {
      if (destroyed || !watched.has(job.id) || !TERMINAL.has(job.state) || job.project_id !== project || current() !== project) return;
      const itemIds = Array.isArray(job.item_ids) ? job.item_ids : [];
      if (!itemIds.length && !job.preview) return;
      const signature = resultSignature(job);
      if (notified.get(job.id) === signature) return;
      notified.set(job.id, signature);
      // Ordinary status polling never reloads the surrounding workbench. Only a
      // tracked task's newly written results request one parent-controlled refresh.
      callback('onChanged', {projectId:project, jobId:job.id, state:job.state, itemIds:itemIds.slice()}).catch(actionFailure);
    }
    function applyJobs(data, requestedProject) {
      if (data.model && typeof data.model.available === 'boolean') { modelKnown = true; modelAvailable = data.model.available; }
      jobs = (Array.isArray(data.jobs) ? data.jobs : []).filter(job => job && job.project_id === requestedProject);
      for (const job of jobs) {
        if (['queued', 'running'].includes(job.state)) watchExisting(job);
        resultChanged(job);
      }
      updateControls(); render();
    }
    function replaceJob(job) {
      if (!job || job.project_id !== project || current() !== project) return;
      jobs = [job, ...jobs.filter(row => row.id !== job.id)];
      resultChanged(job); updateControls(); render();
    }
    async function refresh() {
      if (destroyed) return null;
      setProject();
      if (!project) { render(); return null; }
      const requestedProject = project, requestedGeneration = generation, requestedRevision = revision;
      if (reading && reading.project === requestedProject && reading.generation === requestedGeneration) return reading.promise;
      const record = {project:requestedProject, generation:requestedGeneration, promise:null};
      record.promise = Promise.resolve().then(async () => {
        let again = false;
        try {
          const data = await api('/api/agent/jobs?project=' + encodeURIComponent(requestedProject));
          if (destroyed || current() !== requestedProject || generation !== requestedGeneration) return null;
          if (revision !== requestedRevision) { again = true; return null; }
          refreshError = ''; displayError(); applyJobs(data, requestedProject); return data;
        } catch (_) {
          if (!destroyed && current() === requestedProject && generation === requestedGeneration) {
            refreshError = '整理状态暂未刷新，已保存的材料和任务会保留。'; displayError();
          }
          return null;
        } finally {
          if (reading === record) reading = null;
          if (again && !destroyed) queueMicrotask(() => { void refresh(); });
        }
      });
      reading = record;
      return record.promise;
    }
    function open() {
      setProject(); panel.open = true;
      panel.scrollIntoView({block:'nearest', behavior:'smooth'});
      void refresh();
    }
    function makeButton(text, action) {
      const button = node('button', text); button.type = 'button';
      button.addEventListener('click', async () => {
        button.disabled = true;
        try { await action(); } catch (reason) { actionFailure(reason); }
        finally { if (button.isConnected) button.disabled = false; }
      });
      return button;
    }
    function originalLink(materialId, label, job) {
      const link = node('a', undefined, 'mia-original');
      link.href = '/intake-preview/' + encodeURIComponent(materialId); link.target = '_blank'; link.rel = 'noopener';
      const image = node('img'); image.src = '/intake-page/' + encodeURIComponent(materialId) + '?page=0'; image.alt = label; image.loading = 'lazy';
      link.append(image, node('span', label + ' · 查看原件'));
      if (typeof options.onOpenMaterial === 'function') link.addEventListener('click', event => {
        if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        callback('onOpenMaterial', {projectId:job.project_id, jobId:job.id, materialId}).catch(actionFailure);
      });
      return link;
    }
    async function jobAction(job, path, body) {
      if (jobLocks.has(job.id)) return;
      if (current() !== job.project_id) throw new Error('当前项目已变化，请重新查看任务');
      jobLocks.add(job.id); watchExisting(job); revision += 1; actionError = ''; displayError(); render(true);
      try {
        const data = await api(path, body);
        revision += 1; replaceJob(data.job);
      } finally {
        jobLocks.delete(job.id); rendered = ''; render();
        void refresh();
      }
    }
    function render(force = false) {
      // Live timing counters do not change any controls or evidence. Keeping
      // their polling out of the render key preserves keyboard focus and logs.
      const signature = JSON.stringify([project, jobs.map(({timings, ...job}) => job), [...jobLocks]]);
      if (!force && signature === rendered) return;
      rendered = signature; list.replaceChildren();
      if (!project) { list.append(node('p', '先选择或新建项目，再上传材料。', 'mia-empty')); return; }
      if (!jobs.length) { list.append(node('p', '还没有整理任务。可直接使用原来的上传入口。', 'mia-empty')); return; }
      const priority = job => job.state === 'needs_input' ? 0 : ['queued', 'running'].includes(job.state) ? 1 : 2;
      for (const job of [...jobs].sort((left, right) => priority(left) - priority(right))) {
        const card = node('article', undefined, 'mia-job' + (job.state === 'needs_input' ? ' mia-needs-input' : ''));
        card.dataset.jobId = job.id; card.dataset.state = job.state;
        const title = node('div', undefined, 'mia-job-heading');
        title.append(node('strong', STATES[job.state] || '整理任务'));
        if (job.state !== 'needs_input' && job.message) title.append(node('span', job.message, 'mia-muted'));
        card.append(title);
        for (const expense of job.expenses || []) {
          card.append(node('p', expense.title + ' · ' + money(expense.amount_cents), 'mia-expense-title'));
          if (expense.goods || expense.category) card.append(node('p', [expense.goods && '商品：' + expense.goods, expense.category && '类别：' + expense.category].filter(Boolean).join(' · '), 'mia-muted'));
        }
        if (job.question) {
          const question = job.question;
          card.append(node('p', question.text || '材料缺少明确依据，请补充或人工处理。', 'mia-question'));
          const evidence = question.evidence_summary;
          for (const line of Array.isArray(evidence) ? evidence : typeof evidence === 'string' ? [evidence] : []) if (typeof line === 'string') card.append(node('p', line, 'mia-evidence'));
          const labels = new Map((question.material_ids || []).map((id, index) => [id, '材料 ' + (index + 1)]));
          for (const fact of question.material_facts || []) {
            if (!fact || typeof fact !== 'object') continue;
            const description = [fact.goods, Number.isInteger(fact.amount_cents) ? money(fact.amount_cents) : '', fact.merchant].filter(Boolean).join(' · ');
            if (description) card.append(node('p', (labels.get(fact.id) || '材料') + '：' + description, 'mia-material-fact'));
          }
          const originals = node('div', undefined, 'mia-originals');
          for (const id of question.material_ids || []) originals.append(originalLink(id, labels.get(id), job));
          if (originals.childNodes.length) card.append(originals);
          for (const choice of question.choices || []) {
            const parts = [];
            for (const [kind, ids] of [['票据', choice.voucher_keys || []], ['付款', choice.payment_ids || []], ['明细', choice.support_ids || []]]) {
              const names = ids.map(id => labels.get(String(id).replace(/^q:/, ''))).filter(Boolean);
              if (names.length) parts.push(kind + '：' + names.join('、'));
            }
            const wording = choice.action === 'confirm_relation' ? '同一笔，继续生成预览' : choice.action === 'not_same_expense' ? '不是同一笔，补充材料' : choice.label || '确认此组合';
            const text = wording + (Number.isInteger(choice.amount_cents) ? ' · ' + money(choice.amount_cents) : '') + (parts.length ? '（' + parts.join('；') + '）' : '');
            const button = makeButton(text, () => jobAction(job, '/api/agent/answer', {id:job.id, choice:choice.id, question_id:question.id}));
            button.className = 'mia-choice'; button.disabled = jobLocks.has(job.id); card.append(button);
          }
          if (!(question.choices || []).length) card.append(node('p', '请补充所缺依据，或继续使用材料合成台、费用编辑。', 'mia-hint'));
        }
        const actions = node('div', undefined, 'mia-actions');
        if (['queued', 'running'].includes(job.state)) actions.append(makeButton('停止本次整理', () => jobAction(job, '/api/agent/cancel', {id:job.id})));
        if (['failed', 'stopped'].includes(job.state)) actions.append(makeButton('按剩余额度重试', () => jobAction(job, '/api/agent/retry', {id:job.id})));
        if (job.state === 'needs_input' && !(job.question?.choices || []).length && (job.item_ids || []).length) {
          const contentQuestion = job.question?.type === 'expense_content' || (job.expenses || []).some(row =>
            ['待核对购买凭证', '待核对费用'].includes(row.title) && row.goods && row.source) ||
            (job.last_results || []).some(row => row.tool === 'classify_expense' && row.result?.content_status === 'needs_input');
          if (contentQuestion) actions.append(makeButton('按新规则检查商品', () => jobAction(job, '/api/agent/retry', {id:job.id})));
          else actions.append(makeButton('重新检查对应关系', () => jobAction(job, '/api/agent/refresh-question', {id:job.id, expected_updated_at:job.updated_at})));
        }
        for (const itemId of job.item_ids || []) if (typeof options.onOpenExpense === 'function') actions.append(makeButton('查看费用', () => callback('onOpenExpense', {projectId:job.project_id, jobId:job.id, itemId})));
        if (job.preview) {
          if (typeof options.onPreview === 'function') actions.append(makeButton('查看报销预览', () => callback('onPreview', {projectId:job.project_id, jobId:job.id, itemIds:(job.item_ids || []).slice(), preview:job.preview})));
          for (const [text, name] of [['查看本次 PDF', job.preview.pdf], ['下载本次报销表', job.preview.xlsx]]) {
            if (typeof name !== 'string' || !name) continue;
            const link = node('a', text); link.href = '/export/' + encodeURIComponent(name); link.target = '_blank'; link.rel = 'noopener'; actions.append(link);
          }
          card.append(node('p', '本次预览合计 ' + money(job.preview.total_cents) + ' · 尚未核对', 'mia-muted'));
        }
        for (const button of actions.querySelectorAll('button')) button.disabled = jobLocks.has(job.id);
        if (actions.childNodes.length) card.append(actions);
        const record = node('details', undefined, 'mia-record'); record.append(node('summary', '查看处理记录'));
        const limits = job.limits || {};
        record.append(node('p', '模型 ' + (job.models || 0) + ' / ' + (limits.models || 0) + ' 次 · 工具 ' + (job.tools || 0) + ' / ' + (limits.tools || 0) + ' 次', 'mia-muted'));
        const events = node('ol'); for (const event of job.events || []) if (event && typeof event.message === 'string') events.append(node('li', event.message)); record.append(events); card.append(record);
        list.append(card);
      }
      if (jobs.some(job => job.state === 'needs_input')) panel.open = true;
    }
    function renderPending() {
      pending.replaceChildren(); if (!project) return;
      for (const record of requestRecords(project).filter(row => row.failed && !row.accepted)) {
        const card = node('div', undefined, 'mia-start-retry');
        card.append(node('span', record.ids.length + ' 份材料已保留，启动状态未确认。'));
        const button = makeButton('重试启动本批整理', () => startRecord(record));
        button.disabled = startLocks.has(project); card.append(button); pending.append(card);
      }
    }
    async function startRecord(record) {
      const existing = startLocks.get(record.projectId);
      if (existing) {
        if (existing.signature === record.signature) return existing.promise;
        throw new Error('本项目有一批材料正在启动整理，请稍后重试');
      }
      if (record.accepted) return record.result || {job:{id:record.jobId, project_id:record.projectId}, alreadyAccepted:true};
      const lock = {signature:record.signature, promise:null};
      lock.promise = Promise.resolve().then(async () => {
        try {
          if (!record.body) {
            if (!modelKnown) {
              const status = await api('/api/agent/jobs?project=' + encodeURIComponent(record.projectId));
              if (status.model && typeof status.model.available === 'boolean') { modelKnown = true; modelAvailable = status.model.available; }
              if (current() === record.projectId) { setProject(); updateControls(); }
            }
            record.body = {project_id:record.projectId, material_ids:record.ids.slice(), use_model:modelAvailable && stored('model:' + record.projectId) !== 'false', idempotency_key:record.key, options:{}};
            saveRequests(record.projectId);
          }
          revision += 1;
          const result = await api('/api/agent/start', record.body);
          record.accepted = true; record.failed = false; record.jobId = result.job.id; record.result = result;
          saveRequests(record.projectId); watched.add(result.job.id); revision += 1;
          if (current() === record.projectId) {
            setProject(); actionError = ''; displayError(); panel.open = true; replaceJob(result.job);
          }
          // Acceptance of the write is the upload hook's success boundary. A
          // later GET failure must never make the caller repeat the upload/start.
          void refresh(); return result;
        } catch (reason) {
          record.failed = true; saveRequests(record.projectId);
          if (current() === record.projectId) { setProject(); panel.open = true; actionFailure(reason); }
          throw reason;
        } finally {
          if (startLocks.get(record.projectId) === lock) startLocks.delete(record.projectId);
          if (!destroyed && current() === record.projectId) { updateControls(); renderPending(); }
        }
      });
      startLocks.set(record.projectId, lock); updateControls(); renderPending();
      return lock.promise;
    }
    async function startReceived({projectId, entryIds, batch}) {
      if (destroyed) throw new Error('自动整理入口已经关闭');
      if (typeof projectId !== 'string' || !projectId || !Array.isArray(entryIds) || entryIds.some(id => typeof id !== 'string' || !id)) throw new Error('请选择当前项目的有效材料');
      const ids = [...new Set(entryIds)].sort();
      if (!ids.length) throw new Error('没有可整理的新材料，已关联材料不会重复计费');
      if (ids.length > 20) throw new Error('每次最多整理20份材料，请先在材料合成台选择范围');
      const signature = JSON.stringify([projectId, typeof batch === 'string' ? batch : '', ids]);
      const records = requestRecords(projectId);
      let record = records.find(row => row.signature === signature);
      if (!record) { record = {projectId, signature, ids, key:'manual_' + crypto.randomUUID(), body:null, accepted:false, failed:false}; records.push(record); saveRequests(projectId); }
      return startRecord(record);
    }
    automaticInput.addEventListener('change', () => {
      setProject(); remember('automatic:' + project, String(automaticInput.checked)); updateControls();
    });
    modelInput.addEventListener('change', () => {
      if (project && modelAvailable) remember('model:' + project, String(modelInput.checked));
    });
    organize.addEventListener('click', async () => {
      setProject(); const requestedProject = project;
      if (!requestedProject || organize.disabled) return;
      organize.disabled = true; actionError = ''; displayError();
      try {
        const queue = await api('/api/intake');
        if (current() !== requestedProject) return;
        const ids = (queue.queue || []).filter(entry => entry.project_id === requestedProject && !entry.item_id && ['received', 'review'].includes(entry.status)).map(entry => entry.id);
        await startReceived({projectId:requestedProject, entryIds:ids, batch:'pending:' + ids.slice().sort().join(',')});
      } catch (reason) { if (current() === requestedProject) actionFailure(reason); }
      finally { updateControls(); }
    });
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 1500);
    const controller = {open, shouldAutoOrganize:automatic, startReceived, refresh, destroy() {
      destroyed = true; clearInterval(timer); generation += 1; host.replaceChildren();
      if (active === controller) active = null;
    }};
    active = controller; setProject(); updateControls(); render(); void refresh();
    return controller;
  }
  window.manualInvoiceAgent = {mount, shouldAutoOrganize:automatic,
    open() { if (active) active.open(); },
    refresh() { return active ? active.refresh() : Promise.resolve(null); },
    startReceived(batch) { return active ? active.startReceived(batch) : Promise.reject(new Error('自动整理入口尚未就绪，原件已保留')); },
  };
})();
