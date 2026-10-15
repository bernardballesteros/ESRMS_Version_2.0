// Announcement management dashboard. All data comes from the Flask API, which
// reads and writes public.announcements in Supabase. Nothing is cached locally.
(function () {
  const root = document.getElementById('ann-dashboard');
  if (!root) return;
  const { h, icon, fmtDate, fmtTime, CATEGORY_STYLE, PRIORITY_STYLE, toast, api } = window.AnnUI;
  const cfg = JSON.parse(document.getElementById('ann-config').textContent);
  const opts = cfg.options;
  const $ = (sel) => root.querySelector(sel);

  const listEl = $('#ann-list');
  const featuredEl = $('#ann-featured');
  const filters = $('#ann-filters');
  const formDialog = $('#ann-form-dialog');
  const form = $('#ann-form');
  const F = form.elements;  // form.title / form.name would hit the form's own attributes
  const detailDialog = $('#ann-detail-dialog');
  const confirmDialog = $('#ann-confirm-dialog');
  const templatesDialog = $('#ann-templates-dialog');
  const templatePicker = $('#ann-template-picker');

  let items = [];
  let featured = null;
  let editingId = null;
  let templates = [];
  let loadSeq = 0;

  const STATUS_BADGE = {
    active:    'bg-red-600 text-white',
    scheduled: 'bg-sky-500/15 text-sky-300 border border-sky-500/40',
    draft:     'bg-slate-500/15 text-slate-300 border border-slate-500/40 border-dashed',
    expired:   'bg-amber-500/10 text-amber-300 border border-amber-500/30',
    archived:  'bg-slate-700/60 text-slate-300 border border-slate-600',
  };
  const statusBadge = (s) => h('span', { class: `px-2.5 py-0.5 rounded-md text-[0.6875rem] font-semibold capitalize ${STATUS_BADGE[s] || ''}`, text: s });
  const urgent = (a) => a.category === 'emergency' || a.category === 'important' || a.priority === 'critical' || a.priority === 'high';

  // ------------------------------------------------------------ loading

  async function load({ quiet = false } = {}) {
    const seq = ++loadSeq;
    const params = new URLSearchParams();
    for (const [k, v] of new FormData(filters)) if (v) params.set(k, v);
    listEl.setAttribute('aria-busy', 'true');
    try {
      const data = await api(`${cfg.api.list}?${params}`);
      if (seq !== loadSeq) return;  // a newer request already answered
      items = data.items;
      featured = data.featured;
      renderCounts(data.counts);
      renderFeatured();
      renderList();
    } catch (err) {
      if (seq !== loadSeq) return;
      if (!quiet || !items.length) renderListError(err.message);
      toast(err.message, 'error');
    } finally {
      if (seq === loadSeq) listEl.setAttribute('aria-busy', 'false');
    }
  }

  function renderCounts(c) {
    for (const el of root.querySelectorAll('[data-count]')) el.textContent = c[el.dataset.count] ?? 0;
    const badge = document.querySelector('[data-announcement-badge]');
    if (badge) { badge.textContent = c.active; badge.classList.toggle('hidden', !c.active); }
  }

  function renderListError(message) {
    listEl.replaceChildren(h('div', { class: 'rounded-xl border border-red-500/40 bg-red-950/40 p-6 text-center' },
      icon('cloud_off', 'text-red-400 text-[2rem]'),
      h('p', { class: 'text-sm text-red-200 mt-2', text: message }),
      h('button', { type: 'button', class: 'mt-3 px-4 py-2 rounded-lg text-sm bg-gradient-to-r from-[#00D9FF] to-[#1685FF] hover:brightness-110 !text-[#050A12] text-white', onclick: () => load(), text: 'Try again' })));
  }

  // ------------------------------------------------------------ featured

  function renderFeatured() {
    if (!featured) {
      featuredEl.replaceChildren(h('div', { class: 'rounded-2xl border border-dashed border-white/10 bg-[#0f1729]/60 p-8 text-center' },
        icon('push_pin', 'text-slate-500 text-[2rem]'),
        h('p', { class: 'text-sm text-slate-400 mt-2', text: 'No featured announcement. Pin one, or publish an active announcement.' })));
      return;
    }
    const a = featured;
    const hot = urgent(a);
    featuredEl.replaceChildren(h('article', {
      class: `relative overflow-hidden rounded-2xl p-6 sm:p-7 border-2 ${hot
        ? 'border-red-600/80 bg-gradient-to-br from-[#3a0b14] via-[#1c0d18] to-[#0b1222] shadow-[0_0_40px_-10px_rgba(220,38,38,0.6)]'
        : 'border-sky-600/50 bg-gradient-to-br from-[#0c2340] via-[#0d1830] to-[#0b1222]'}`,
    },
      // Storm/warning backdrop like the reference.
      h('div', { class: 'pointer-events-none absolute inset-y-0 right-0 w-1/2 hidden sm:block bg-[radial-gradient(ellipse_at_top_right,rgba(148,163,184,0.18),transparent_60%)]' }),
      icon(hot ? 'warning' : 'campaign', `pointer-events-none absolute right-6 bottom-4 hidden sm:block text-[7.5rem] ${hot ? 'text-red-600/80' : 'text-sky-500/30'}`),
      h('div', { class: 'relative flex gap-4 sm:gap-5 sm:pr-40' },
        h('span', { class: `shrink-0 w-14 h-14 rounded-full flex items-center justify-center ${hot ? 'bg-red-600' : 'bg-sky-600'}` }, icon('notifications_active', 'text-white text-[1.875rem]')),
        h('div', { class: 'min-w-0' },
          h('div', { class: 'flex flex-wrap items-center gap-2' },
            h('span', { class: `px-2.5 py-1 rounded-md text-[0.6875rem] font-bold uppercase tracking-wide ${hot ? 'bg-red-600 text-white' : 'bg-sky-600 text-white'}`, text: opts.categories[a.category] }),
            a.is_pinned && h('span', { class: 'flex items-center gap-1 text-[0.6875rem] text-slate-300' }, icon('push_pin', 'text-[0.875rem]'), 'Pinned'),
            a.effective_status !== 'active' && statusBadge(a.effective_status)),
          h('h2', { class: 'text-xl sm:text-2xl font-bold text-white mt-2 break-words', text: a.title }),
          h('p', { class: 'text-sm text-slate-200 mt-2 whitespace-pre-line line-clamp-4 break-words', text: a.message }),
          h('div', { class: 'flex flex-wrap items-center gap-x-4 gap-y-2 mt-4 text-xs text-slate-300' },
            h('span', { class: 'flex items-center gap-1.5' }, icon('calendar_month', 'text-red-400 text-[1rem]'), fmtDate(a.published_at), ' â€¢ ', fmtTime(a.published_at)),
            h('span', { class: 'flex items-center gap-1.5' }, icon('priority_high', 'text-[1rem]'), `${opts.priorities[a.priority]} priority`),
            h('span', { class: 'flex items-center gap-1.5' }, icon('groups', 'text-[1rem]'), audienceText(a))),
          h('div', { class: 'flex flex-wrap gap-2 mt-4' },
            smallBtn(a.is_pinned ? 'Unpin' : 'Pin', 'push_pin', () => setPinned(a, !a.is_pinned)),
            smallBtn('Edit', 'edit', () => openForm(a)),
            smallBtn('Details', 'visibility', () => openDetails(a))))),
    ));
  }

  const smallBtn = (text, ic, onclick, extra = '') =>
    h('button', { type: 'button', class: `inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs bg-white/10 hover:bg-white/20 text-white ${extra}`, onclick }, icon(ic, 'text-[1rem]'), text);

  function audienceText(a) {
    if (a.target_audience === 'barangay') return `Brgy. ${a.barangay}`;
    if (a.target_audience === 'selected_groups') return (a.target_groups || []).map((g) => opts.groups[g] || g).join(', ');
    return opts.audiences[a.target_audience];
  }

  // ------------------------------------------------------------ list

  function renderList() {
    $('#ann-result-count').textContent = `${items.length} shown`;
    if (!items.length) {
      const filtered = [...new FormData(filters).entries()].some(([k, v]) => v && k !== 'sort');
      listEl.replaceChildren(h('div', { class: 'rounded-xl border border-dashed border-white/10 bg-[#0f1729]/60 p-10 text-center' },
        icon(filtered ? 'filter_alt_off' : 'campaign', 'text-slate-500 text-[2.25rem]'),
        h('p', { class: 'text-sm text-slate-300 mt-2', text: filtered ? 'No announcements match these filters.' : 'No announcements yet.' }),
        h('p', { class: 'text-xs text-slate-500 mt-1', text: filtered ? 'Try clearing the filters.' : 'Create the first one to notify your team and community.' }),
        !filtered && h('button', { type: 'button', class: 'mt-4 px-4 py-2 rounded-lg text-sm bg-gradient-to-r from-[#00D9FF] to-[#1685FF] hover:brightness-110 !text-[#050A12] text-white font-semibold', onclick: () => openForm(), text: 'Create Announcement' })));
      return;
    }
    listEl.replaceChildren(...items.map(card));
  }

  function card(a) {
    const style = CATEGORY_STYLE[a.category] || CATEGORY_STYLE.general;
    const archived = a.effective_status === 'archived';
    const when = a.published_at || a.created_at;
    return h('article', { class: `group rounded-xl bg-[#0f1729]/90 border border-white/[0.07] hover:border-white/15 p-4 transition ${archived ? 'opacity-70' : ''}` },
      h('div', { class: 'flex gap-3 sm:gap-4' },
        h('span', { class: `shrink-0 w-11 h-11 rounded-lg flex items-center justify-center ${style.tile}` }, icon(style.icon, 'text-[1.5rem]')),
        h('div', { class: 'flex-1 min-w-0' },
          h('div', { class: 'flex flex-wrap items-center gap-2' },
            h('span', { class: `text-[0.6875rem] font-bold uppercase tracking-wide ${style.label}`, text: opts.categories[a.category] }),
            h('span', { class: `px-1.5 py-px rounded border text-[0.625rem] font-semibold ${PRIORITY_STYLE[a.priority]}`, text: opts.priorities[a.priority] }),
            a.is_pinned && icon('push_pin', 'text-[0.875rem] text-red-400'),
            a.target_audience !== 'all_citizens' && h('span', { class: 'text-[0.625rem] text-slate-500', text: `â†’ ${audienceText(a)}` })),
          h('button', { type: 'button', class: 'block text-left font-semibold text-white mt-0.5 hover:underline break-words', onclick: () => openDetails(a), text: a.title }),
          h('p', { class: 'text-xs text-slate-400 mt-1 line-clamp-2 break-words', text: a.message })),
        h('div', { class: 'hidden sm:flex flex-col items-end justify-between gap-2 shrink-0 text-right' },
          h('div', { class: 'text-[0.6875rem] text-slate-400 leading-tight' }, h('div', { text: fmtDate(when) }), h('div', { text: fmtTime(when) })),
          statusBadge(a.effective_status))),
      h('div', { class: 'flex flex-wrap items-center gap-1.5 mt-3 pt-3 border-t border-white/[0.06]' },
        h('span', { class: 'sm:hidden flex items-center gap-2 mr-auto text-[0.6875rem] text-slate-400' }, statusBadge(a.effective_status), `${fmtDate(when)} ${fmtTime(when)}`),
        actionBtn('View', 'visibility', () => openDetails(a)),
        actionBtn('Edit', 'edit', () => openForm(a)),
        !archived && actionBtn(a.is_pinned ? 'Unpin' : 'Pin', 'push_pin', () => setPinned(a, !a.is_pinned)),
        archived
          ? actionBtn('Restore', 'unarchive', () => restore(a))
          : actionBtn('Archive', 'inventory_2', () => archive(a)),
        actionBtn('Delete', 'delete', () => remove(a), 'text-red-300 hover:bg-red-500/15')));
  }

  const actionBtn = (text, ic, onclick, extra = 'text-slate-300 hover:bg-white/10') =>
    h('button', { type: 'button', class: `inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs ${extra}`, onclick, title: text }, icon(ic, 'text-[1rem]'), text);

  // ------------------------------------------------------------ details

  function openDetails(a) {
    const row = (k, v) => v && h('div', { class: 'flex gap-3 py-1.5 border-b border-white/5 text-sm' },
      h('dt', { class: 'w-32 shrink-0 text-slate-500', text: k }), h('dd', { class: 'text-slate-200 break-words min-w-0', text: v }));
    const ts = (iso) => iso && `${fmtDate(iso)} ${fmtTime(iso)}`;
    $('#ann-detail-body').replaceChildren(
      h('div', { class: 'flex items-center gap-2 mb-2' },
        h('span', { class: `text-[0.6875rem] font-bold uppercase ${CATEGORY_STYLE[a.category]?.label}`, text: opts.categories[a.category] }),
        statusBadge(a.effective_status)),
      h('h3', { class: 'text-xl font-bold text-white break-words', text: a.title }),
      h('p', { class: 'text-sm text-slate-300 whitespace-pre-line mt-3 break-words', text: a.message }),
      h('dl', { class: 'mt-5' },
        row('Priority', opts.priorities[a.priority]),
        row('Audience', audienceText(a)),
        row('Publication', ts(a.published_at) || 'Not set'),
        row('Expires', ts(a.expires_at) || 'Never'),
        row('Published by', a.published_by_name || (a.published_by ? a.published_by : null)),
        row('Created', `${ts(a.created_at)}${a.created_by_name ? ` by ${a.created_by_name}` : ''}`),
        row('Last updated', `${ts(a.updated_at)}${a.updated_by_name ? ` by ${a.updated_by_name}` : ''}`),
        row('Publicly visible', a.effective_status === 'active' ? 'Yes' : 'No')),
      h('div', { class: 'flex justify-end gap-2 mt-4' },
        smallBtn('Edit', 'edit', () => { detailDialog.close(); openForm(a); })));
    detailDialog.showModal();
  }

  // ------------------------------------------------------------ form

  const pad = (n) => String(n).padStart(2, '0');
  function toLocalInput(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  const fromLocalInput = (v) => (v ? new Date(v).toISOString() : null);

  function syncAudienceFields() {
    const audience = F.target_audience.value;
    for (const el of form.querySelectorAll('[data-show-for]')) el.classList.toggle('hidden', el.dataset.showFor !== audience);
  }
  function syncPublishLabel() {
    const future = F.published_at.value && new Date(F.published_at.value) > new Date();
    const label = F.status.value === 'archived' ? 'Save as Archived' : future || F.status.value === 'scheduled' ? 'Schedule Announcement' : editingId ? 'Save & Publish' : 'Publish Announcement';
    form.querySelector('[data-publish-label]').textContent = label;
  }
  F.target_audience.addEventListener('change', syncAudienceFields);
  F.status.addEventListener('change', syncPublishLabel);
  F.published_at.addEventListener('input', syncPublishLabel);

  function clearErrors() {
    $('#ann-form-error').classList.add('hidden');
    for (const el of form.querySelectorAll('[data-error-for]')) { el.classList.add('hidden'); el.textContent = ''; }
    for (const el of form.querySelectorAll('[aria-invalid]')) el.removeAttribute('aria-invalid');
  }
  function showErrors(fields, message) {
    const box = $('#ann-form-error');
    box.textContent = message;
    box.classList.remove('hidden');
    let first = null;
    for (const [name, text] of Object.entries(fields || {})) {
      const p = form.querySelector(`[data-error-for="${name}"]`);
      if (p) { p.textContent = text; p.classList.remove('hidden'); }
      const input = form.elements[name];
      if (input?.setAttribute) { input.setAttribute('aria-invalid', 'true'); first ??= input; }
    }
    (first || box).focus?.();
  }

  function openForm(a = null, { schedule = false } = {}) {
    editingId = a?.id || null;
    form.reset();
    clearErrors();
    templatePicker.value = '';
    templatePicker.parentElement.classList.toggle('hidden', !!a);
    $('#ann-form-title').textContent = a ? 'Edit Announcement' : schedule ? 'Schedule Announcement' : 'Create Announcement';
    if (a) {
      F.title.value = a.title;
      F.message.value = a.message;
      F.category.value = a.category;
      F.priority.value = a.priority;
      F.target_audience.value = a.target_audience;
      F.barangay.value = a.barangay || '';
      for (const cb of form.querySelectorAll('[name=target_groups]')) cb.checked = (a.target_groups || []).includes(cb.value);
      F.published_at.value = toLocalInput(a.published_at);
      F.expires_at.value = toLocalInput(a.expires_at);
      F.status.value = a.status;
      F.is_pinned.checked = a.is_pinned;
    } else if (schedule) {
      F.status.value = 'scheduled';
      const d = new Date(Date.now() + 60 * 60 * 1000);
      d.setMinutes(0, 0, 0);
      F.published_at.value = toLocalInput(d.toISOString());
    }
    syncAudienceFields();
    syncPublishLabel();
    formDialog.showModal();
    F.title.focus();
  }

  function payload(action) {
    return {
      action,
      title: F.title.value,
      message: F.message.value,
      category: F.category.value,
      priority: F.priority.value,
      target_audience: F.target_audience.value,
      barangay: F.barangay.value,
      target_groups: [...form.querySelectorAll('[name=target_groups]:checked')].map((c) => c.value),
      published_at: fromLocalInput(F.published_at.value),
      expires_at: fromLocalInput(F.expires_at.value),
      status: F.status.value,
      is_pinned: F.is_pinned.checked,
    };
  }

  // Same rules as the server; the server re-checks everything.
  function validateLocal(p) {
    const e = {};
    if (!p.title.trim()) e.title = 'Title is required.';
    if (!p.message.trim()) e.message = 'Message is required.';
    if (!p.category) e.category = 'Choose a category.';
    if (!p.priority) e.priority = 'Choose a priority.';
    if (p.target_audience === 'barangay' && !p.barangay.trim()) e.barangay = 'Enter the barangay this announcement is for.';
    if (p.target_audience === 'selected_groups' && !p.target_groups.length) e.target_groups = 'Select at least one group.';
    if (p.action === 'publish' && p.status === 'scheduled') {
      if (!p.published_at) e.published_at = 'Scheduled announcements need a publication date and time.';
      else if (new Date(p.published_at) <= new Date()) e.published_at = 'Scheduled publication time must be in the future.';
    }
    if (p.expires_at && p.published_at && new Date(p.expires_at) <= new Date(p.published_at)) e.expires_at = 'Expiration must be after the publication time.';
    return e;
  }

  let saving = false;
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (saving) return;
    const action = ev.submitter?.value === 'draft' ? 'draft' : 'publish';
    const p = payload(action);
    clearErrors();
    const local = validateLocal(p);
    if (Object.keys(local).length) { showErrors(local, 'Please fix the highlighted fields.'); return; }

    saving = true;
    const buttons = form.querySelectorAll('button[type=submit]');
    buttons.forEach((b) => { b.disabled = true; b.classList.add('opacity-60'); });
    form.querySelector('[data-spinner]').classList.toggle('hidden', action !== 'publish');
    try {
      const url = editingId ? `${cfg.api.base}/${editingId}` : cfg.api.create;
      const { item } = await api(url, { method: editingId ? 'PUT' : 'POST', body: p });
      // Only reached when the server confirmed the database write.
      formDialog.close();
      const msg = {
        draft: 'Draft saved. It is not visible to the public.',
        scheduled: `Scheduled. It goes live ${fmtDate(item.published_at)} at ${fmtTime(item.published_at)}.`,
        active: editingId ? 'Announcement updated. The Citizen Viewer now shows the change.' : 'Announcement published. It is now live in the Citizen Announcement Viewer.',
        archived: 'Saved as archived. It is not visible to the public.',
        expired: 'Saved. It has already expired, so it is not visible to the public.',
      }[item.effective_status];
      toast(msg, 'success');
      await load({ quiet: true });
    } catch (err) {
      showErrors(err.fields, err.message);
    } finally {
      saving = false;
      buttons.forEach((b) => { b.disabled = false; b.classList.remove('opacity-60'); });
      form.querySelector('[data-spinner]').classList.add('hidden');
    }
  });

  root.querySelectorAll('dialog [data-close]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));

  // ------------------------------------------------------------ row actions

  function confirmAction(title, text, okText) {
    $('#ann-confirm-title').textContent = title;
    $('#ann-confirm-text').textContent = text;
    $('#ann-confirm-ok').textContent = okText;
    confirmDialog.returnValue = '';
    confirmDialog.showModal();
    return new Promise((resolve) => confirmDialog.addEventListener('close', () => resolve(confirmDialog.returnValue === 'ok'), { once: true }));
  }

  async function mutate(promise, success) {
    try {
      await promise;
      toast(success, 'success');
    } catch (err) {
      toast(err.message, 'error');
    }
    await load({ quiet: true });
  }

  const setPinned = (a, pinned) => mutate(api(`${cfg.api.base}/${a.id}/pin`, { method: 'POST', body: { pinned } }),
    pinned ? 'Pinned as the featured announcement.' : 'Announcement unpinned.');

  async function archive(a) {
    if (!(await confirmAction('Archive this announcement?', `"${a.title}" will be removed from the Citizen Viewer. You can restore it later.`, 'Archive'))) return;
    mutate(api(`${cfg.api.base}/${a.id}/archive`, { method: 'POST', body: {} }), 'Announcement archived and removed from public view.');
  }
  const restore = (a) => mutate(api(`${cfg.api.base}/${a.id}/restore`, { method: 'POST', body: {} }), 'Announcement restored.');

  async function remove(a) {
    if (!(await confirmAction('Delete permanently?', `"${a.title}" will be deleted from the database. This cannot be undone.`, 'Delete'))) return;
    mutate(api(`${cfg.api.base}/${a.id}`, { method: 'DELETE' }), 'Announcement deleted.');
  }

  // ------------------------------------------------------------ templates

  async function loadTemplates() {
    try {
      templates = (await api(cfg.api.templates)).items;
    } catch (err) {
      templates = [];
      toast(err.message, 'error');
    }
    templatePicker.replaceChildren(h('option', { value: '', text: templates.length ? 'Use templateâ€¦' : 'No templates' }),
      ...templates.map((t) => h('option', { value: t.id, text: t.name })));
    const list = $('#ann-templates-list');
    list.replaceChildren(...(templates.length ? templates.map((t) =>
      h('div', { class: 'flex items-start gap-3 rounded-lg bg-[#0a1020] border border-white/10 p-3' },
        icon('description', 'text-slate-400'),
        h('div', { class: 'flex-1 min-w-0' },
          h('p', { class: 'text-sm font-semibold text-white', text: t.name }),
          h('p', { class: 'text-xs text-slate-400 truncate', text: `${opts.categories[t.category]} Â· ${opts.priorities[t.priority]} Â· ${t.title || 'No title'}` })),
        actionBtn('Use', 'edit_square', () => { templatesDialog.close(); openForm(); applyTemplate(t); }),
        actionBtn('Delete', 'delete', async () => {
          if (!(await confirmAction('Delete template?', `"${t.name}" will be deleted.`, 'Delete'))) return;
          try { await api(`${cfg.api.templates}/${t.id}`, { method: 'DELETE' }); toast('Template deleted.'); } catch (err) { toast(err.message, 'error'); }
          loadTemplates();
        }, 'text-red-300 hover:bg-red-500/15')))
      : [h('p', { class: 'text-sm text-slate-500', text: 'No templates yet. Save common messages below to reuse them.' })]));
  }

  function applyTemplate(t) {
    if (t.title) F.title.value = t.title;
    if (t.message) F.message.value = t.message;
    F.category.value = t.category;
    F.priority.value = t.priority;
  }
  templatePicker.addEventListener('change', () => {
    const t = templates.find((x) => x.id === templatePicker.value);
    if (t) applyTemplate(t);
  });

  $('#ann-template-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const tf = ev.target;
    const err = tf.querySelector('[data-error-for=name]');
    err.classList.add('hidden');
    if (!tf.elements.name.value.trim()) { err.textContent = 'Template name is required.'; err.classList.remove('hidden'); tf.elements.name.focus(); return; }
    try {
      await api(cfg.api.templates, { method: 'POST', body: Object.fromEntries(new FormData(tf)) });
      toast('Template saved.');
      tf.reset();
      loadTemplates();
    } catch (e) {
      toast(e.message, 'error');
    }
  });

  // ------------------------------------------------------------ wiring

  root.querySelectorAll('[data-action]').forEach((btn) => btn.addEventListener('click', () => {
    switch (btn.dataset.action) {
      case 'create': openForm(); break;
      case 'schedule': openForm(null, { schedule: true }); break;
      case 'templates': loadTemplates(); templatesDialog.showModal(); break;
      case 'view-all':
        filters.reset();
        load();
        $('#ann-list-title').scrollIntoView({ behavior: 'smooth' });
        break;
    }
  }));
  root.querySelectorAll('[data-filter-status]').forEach((btn) => btn.addEventListener('click', () => {
    filters.reset();
    filters.elements.status.value = btn.dataset.filterStatus;
    load();
    $('#ann-list-title').scrollIntoView({ behavior: 'smooth' });
  }));

  let debounce;
  filters.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(load, 300); });
  filters.addEventListener('change', () => { clearTimeout(debounce); load(); });
  filters.addEventListener('reset', () => setTimeout(load, 0));
  filters.addEventListener('submit', (e) => e.preventDefault());

  const liveEl = $('#ann-live');
  const LIVE = {
    live:       ['bg-emerald-400', 'Live'],
    connecting: ['bg-amber-400 animate-pulse', 'Connectingâ€¦'],
    polling:    ['bg-amber-400', 'Auto-refresh'],
  };
  window.AnnouncementLive.start({
    ...cfg.realtime,
    onChange: () => load({ quiet: true }),
    onStatus: (s) => {
      const [dot, text] = LIVE[s];
      liveEl.firstElementChild.className = `w-2 h-2 rounded-full ${dot}`;
      liveEl.lastElementChild.textContent = text;
      liveEl.title = s === 'polling' ? 'Realtime unavailable; refreshing every 15 seconds.' : 'Receiving changes in real time.';
    },
  });

  // Scheduled posts go live and active ones expire on the clock, without a DB change.
  setInterval(() => load({ quiet: true }), 60000);

  load();
  loadTemplates();
})();
