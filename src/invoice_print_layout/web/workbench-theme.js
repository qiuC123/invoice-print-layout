'use strict';
(() => {
  const key = 'invoice-workbench-theme';
  const root = document.documentElement;
  const valid = value => value === 'light' || value === 'dark';
  let theme = 'light';
  try {
    const saved = localStorage.getItem(key);
    if (valid(saved)) theme = saved;
  } catch (_) { /* Storage may be unavailable in an embedded browser. */ }

  // This script runs before the stylesheets so the first paint uses the saved mode.
  root.dataset.theme = theme;
  const originalScheme = root.style.colorScheme;
  const syncScheme = () => {
    const scoped = root.matches('.invoice-assistant, .embedded-workbench');
    root.style.colorScheme = scoped ? theme : originalScheme;
  };
  syncScheme();
  new MutationObserver(syncScheme).observe(root, {attributes: true, attributeFilter: ['class']});

  const bind = () => {
    const select = document.getElementById('themeSelect');
    if (!select || select.dataset.workbenchThemeBound === 'true') return;
    select.dataset.workbenchThemeBound = 'true';
    select.value = theme;
    select.addEventListener('change', () => {
      if (!valid(select.value)) { select.value = theme; return; }
      theme = select.value;
      root.dataset.theme = theme;
      syncScheme();
      try { localStorage.setItem(key, theme); }
      catch (_) { /* Keep a working choice for this page even when it cannot be saved. */ }
    });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind, {once: true});
  else bind();
})();
