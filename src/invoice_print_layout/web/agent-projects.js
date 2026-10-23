/* Project navigation uses the same local project API as the manual workbench. */
(() => {
  'use strict';
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function mount({host, api, onSelect, onCreated, isBusy = () => false}) {
    let projects = [], current = '', externalBusy = false, saving = false, selecting = false, acknowledged = false;
    let rendered = '', opener = null;
    host.classList.add('agent-projects');
    host.setAttribute('aria-label', '项目导航');
    const heading = element('div', undefined, 'agent-projects-heading');
    const title = element('h2', '项目');
    title.id = 'agent-projects-heading';
    const create = element('button', '＋ 新建项目', 'agent-projects-create');
    create.type = 'button';
    heading.append(title, create);
    const searchLabel = element('label', '搜索项目', 'agent-projects-search');
    const search = element('input');
    search.type = 'search'; search.id = 'agent-projects-search'; search.placeholder = '按名称或地点搜索';
    search.autocomplete = 'off'; searchLabel.append(search);
    const navigation = element('nav', undefined, 'agent-projects-list');
    navigation.setAttribute('aria-labelledby', title.id);
    const empty = element('p', undefined, 'agent-projects-empty');
    const mobileLabel = element('label', '当前项目', 'agent-projects-mobile');
    const mobile = element('select');
    mobile.id = 'agent-projects-select'; mobileLabel.append(mobile);
    const status = element('p', undefined, 'agent-projects-status');
    status.setAttribute('role', 'status'); status.hidden = true;
    host.append(heading, searchLabel, navigation, empty, mobileLabel, status);

    const dialog = element('dialog', undefined, 'agent-projects-dialog');
    dialog.id = 'agent-projects-dialog'; dialog.setAttribute('aria-labelledby', 'agent-projects-dialog-title');
    const form = element('form');
    const dialogTitle = element('h2', '新建项目'); dialogTitle.id = 'agent-projects-dialog-title';
    const nameLabel = element('label', '项目名称');
    const name = element('input'); name.id = 'agent-projects-name'; name.name = 'name'; name.required = true;
    name.maxLength = 200; name.autocomplete = 'off'; name.placeholder = '例如：南昌车展'; nameLabel.append(name);
    const siteLabel = element('label', '地点（可选）');
    const site = element('input'); site.id = 'agent-projects-site'; site.name = 'site_name';
    site.maxLength = 200; site.autocomplete = 'off'; site.placeholder = '例如：南昌'; siteLabel.append(site);
    const hint = element('p', '创建后可直接上传材料，费用和原件都按项目保存。', 'muted');
    const error = element('p', undefined, 'agent-projects-error'); error.setAttribute('role', 'alert'); error.hidden = true;
    const actions = element('div', undefined, 'agent-projects-dialog-actions');
    const cancel = element('button', '取消'); cancel.type = 'button';
    const submit = element('button', '创建项目', 'primary'); submit.type = 'submit';
    actions.append(cancel, submit); form.append(dialogTitle, nameLabel, siteLabel, hint, error, actions); dialog.append(form);
    host.append(dialog);

    function blocked() { return externalBusy || !!isBusy() || saving || selecting; }
    function controls() {
      const busy = blocked();
      create.disabled = busy; mobile.disabled = busy || !projects.length;
      navigation.querySelectorAll('button').forEach(button => { button.disabled = busy; });
      name.disabled = saving || acknowledged || externalBusy || !!isBusy();
      site.disabled = name.disabled;
      submit.disabled = saving || acknowledged || externalBusy || !!isBusy() || !name.value.trim();
      submit.textContent = saving ? '正在创建…' : acknowledged ? '已创建' : '创建项目';
      cancel.disabled = saving;
    }
    function showStatus(message) { status.textContent = message; status.hidden = !message; }
    async function select(id) {
      if (blocked() || !id || id === current || !projects.some(project => project.id === id)) {
        mobile.value = current; return;
      }
      selecting = true; controls(); showStatus('正在切换项目…');
      try { await onSelect(id); showStatus(''); }
      catch (failure) { showStatus(failure.message || '项目切换失败，请重试'); }
      finally { selecting = false; mobile.value = current; controls(); }
    }
    function renderList() {
      const query = search.value.trim().toLocaleLowerCase('zh-CN');
      const filtered = projects.filter(project => (project.name + ' ' + (project.site_name || '')).toLocaleLowerCase('zh-CN').includes(query));
      const fingerprint = JSON.stringify([filtered, current]);
      if (fingerprint !== rendered) {
        rendered = fingerprint;
        const focusedId = navigation.contains(document.activeElement) ? document.activeElement.dataset.projectId : '';
        navigation.replaceChildren();
        for (const project of filtered) {
          const button = element('button', undefined, 'agent-projects-item'); button.type = 'button';
          button.dataset.projectId = project.id;
          const label = element('span', project.name); button.append(label);
          if (project.site_name) button.append(element('small', project.site_name));
          if (project.id === current) button.setAttribute('aria-current', 'page');
          button.addEventListener('click', () => { void select(project.id); });
          navigation.append(button);
        }
        if (focusedId) [...navigation.querySelectorAll('button')].find(button => button.dataset.projectId === focusedId)?.focus();
      }
      empty.textContent = projects.length ? '没有找到匹配的项目' : '还没有项目，先新建一个项目开始整理。';
      empty.hidden = filtered.length > 0;
      controls();
    }
    function update(nextProjects, currentId) {
      projects = nextProjects.map(project => ({id: String(project.id), name: String(project.name || '未命名项目'), site_name: String(project.site_name || '')}));
      current = String(currentId || '');
      const optionsFingerprint = JSON.stringify(projects);
      if (mobile.dataset.options !== optionsFingerprint) {
        mobile.dataset.options = optionsFingerprint; mobile.replaceChildren();
        const placeholder = element('option', projects.length ? '请选择项目' : '先新建一个项目');
        placeholder.value = ''; mobile.append(placeholder);
        for (const project of projects) {
          const option = element('option', project.name); option.value = project.id; mobile.append(option);
        }
      }
      mobile.value = current; renderList();
    }
    function dismiss() {
      if (saving) return;
      dialog.close(); error.hidden = true; form.reset(); acknowledged = false; controls();
      if (opener && opener.isConnected && !opener.disabled) opener.focus();
    }
    function openCreate() {
      if (blocked()) return;
      if (dialog.open) { name.focus(); return; }
      opener = document.activeElement; form.reset(); error.hidden = true; acknowledged = false;
      controls(); dialog.showModal(); name.focus();
    }
    create.addEventListener('click', openCreate);
    cancel.addEventListener('click', dismiss);
    dialog.addEventListener('cancel', event => { event.preventDefault(); dismiss(); });
    name.addEventListener('input', controls);
    search.addEventListener('input', renderList);
    mobile.addEventListener('change', () => { void select(mobile.value); });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (blocked() || acknowledged || !name.value.trim() || !form.reportValidity()) return;
      const fields = {name: name.value.trim(), site_name: site.value.trim()};
      saving = true; error.hidden = true; controls();
      try {
        const project = await api('/api/projects', fields);
        acknowledged = true;
        if (!project || typeof project.id !== 'string' || !project.id) throw new Error('项目已创建，但返回结果无法确认；请关闭窗口并刷新页面查看。');
        try { await onCreated(project); }
        catch { throw new Error('项目已创建，但列表刷新失败；请关闭窗口并刷新页面查看。'); }
        saving = false; dismiss(); showStatus('项目已创建，可以上传材料了。');
      } catch (failure) {
        error.textContent = failure.message || '创建失败，请重试'; error.hidden = false;
      } finally {
        saving = false; controls();
        if (dialog.open && !acknowledged) name.focus();
      }
    });
    update([], '');
    return {update, openCreate, setBusy(value) {externalBusy = !!value; controls();}};
  }
  window.AgentProjects = {mount};
})();
