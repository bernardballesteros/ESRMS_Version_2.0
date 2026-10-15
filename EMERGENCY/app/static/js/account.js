// Account settings pages: Display, Notifications, Security & MFA, My Profile, Agency Settings.
// Each page loads its data from /api/account (Flask), which reads the shared Supabase
// database, and only reports success after the server confirmed the change.
(function () {
  const cfgEl = document.getElementById('account-config');
  const bodyEl = document.getElementById('account-body');
  if (!cfgEl || !bodyEl || !window.AnnUI) return;
  const { h, icon, toast, api } = window.AnnUI;
  const cfg = JSON.parse(cfgEl.textContent);
  const $ = (id) => document.getElementById(id);
  const loadingEl = $('account-loading');
  const errorEl = $('account-error');

  // ------------------------------------------------------------ shared helpers

  function show(state, message) {
    loadingEl.classList.toggle('hidden', state !== 'loading');
    errorEl.classList.toggle('hidden', state !== 'error');
    bodyEl.classList.toggle('hidden', state !== 'ready');
    if (state === 'error') errorEl.querySelector('[data-text]').textContent = message;
  }

  async function busy(btn, fn) {
    if (!btn) return fn();
    if (btn.disabled) return undefined;   // repeated clicks while a request is in flight
    const text = btn.textContent;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = 'Please wait…';
    try { return await fn(); } finally {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
      btn.textContent = text;
    }
  }

  function clearErrors(form) {
    form.querySelectorAll('[data-form-error]').forEach((b) => { b.classList.add('hidden'); b.textContent = ''; });
    form.querySelectorAll('[data-error-for]').forEach((p) => { p.classList.add('hidden'); p.textContent = ''; });
    form.querySelectorAll('[aria-invalid]').forEach((el) => el.removeAttribute('aria-invalid'));
  }

  function showErrors(form, fields, message) {
    const box = form.querySelector('[data-form-error]');
    if (box && message) { box.textContent = message; box.classList.remove('hidden'); }
    let first = null;
    for (const [name, text] of Object.entries(fields || {})) {
      const p = form.querySelector(`[data-error-for="${name}"]`);
      if (p) { p.textContent = text; p.classList.remove('hidden'); }
      const el = form.elements[name];
      if (el && el.setAttribute) { el.setAttribute('aria-invalid', 'true'); first = first || el; }
    }
    return first || box || form;
  }

  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  const PHONE_RE = /^\+?[0-9][0-9 ()-]{2,24}$/;
  const phoneOk = (v) => PHONE_RE.test(v) && v.replace(/\D/g, '').length >= 7;
  const PHONE_HINT = 'Use at least 7 digits; spaces, dashes and parentheses are allowed, optionally starting with +.';

  const confirmEl = $('account-confirm');
  function confirmDialog(title, text, ok) {
    confirmEl.querySelector('[data-title]').textContent = title;
    confirmEl.querySelector('[data-text]').textContent = text;
    confirmEl.querySelector('[data-ok]').textContent = ok;
    confirmEl.returnValue = '';
    confirmEl.showModal();
    return new Promise((resolve) => confirmEl.addEventListener('close', () => resolve(confirmEl.returnValue === 'ok'), { once: true }));
  }

  const dateFmt = new Intl.DateTimeFormat('en-PH', { timeZone: 'Asia/Manila', dateStyle: 'medium', timeStyle: 'short' });
  const fmtDateTime = (iso) => (iso ? dateFmt.format(new Date(iso)) : '—');

  function device(ua) {
    if (!ua) return 'Unknown device';
    const os = /Windows/.test(ua) ? 'Windows' : /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS' : /Mac OS X/.test(ua) ? 'macOS' : /Linux/.test(ua) ? 'Linux' : null;
    const browser = /Edg\//.test(ua) ? 'Edge' : /OPR\//.test(ua) ? 'Opera' : /Chrome\//.test(ua) ? 'Chrome' : /Firefox\//.test(ua) ? 'Firefox' : /Safari\//.test(ua) ? 'Safari' : null;
    if (!os && !browser) return /okhttp|Dart|python|Go-http/i.test(ua) ? 'ESRMS mobile app or service' : 'Unknown device';
    return `${os || 'Unknown OS'} · ${browser || 'App'}`;
  }

  function setImage(el, url, fallback) {
    el.replaceChildren(url ? h('img', { src: url, alt: '', class: 'w-full h-full object-cover' }) : fallback);
  }

  // Image upload: multipart, with a custom header the server requires (blocks cross-site form posts).
  async function upload(url, file) {
    const body = new FormData();
    body.append('file', file);
    let res;
    try {
      res = await fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'X-ESRMS-Request': '1' }, body });
    } catch (_) {
      throw new Error('Network error. Check your connection and try again.');
    }
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (!res.ok) throw new Error(data.error || (res.status === 413 ? 'The image is too large.' : `Upload failed (${res.status}).`));
    return data;
  }

  // `url` is a function so it can follow the agency a system administrator has selected.
  function wireImage({ input, change, remove, url, label, onChange }) {
    change.addEventListener('click', () => input.click());
    input.addEventListener('change', async () => {
      const file = input.files[0];
      input.value = '';
      if (!file) return;
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) return toast('Use a PNG, JPEG or WebP image.', 'error');
      if (file.size > 2 * 1024 * 1024) return toast('The image must be 2 MB or smaller.', 'error');
      await busy(change, async () => {
        try {
          onChange((await upload(url(), file)).url);
          toast(`${label} updated.`, 'success');
        } catch (err) {
          toast(err.message, 'error');
        }
      });
    });
    remove.addEventListener('click', async () => {
      if (!(await confirmDialog(`Remove ${label.toLowerCase()}?`, 'The current image will be deleted.', 'Remove'))) return;
      await busy(remove, async () => {
        try {
          await api(url(), { method: 'DELETE' });
          onChange(null);
          toast(`${label} removed.`, 'success');
        } catch (err) {
          toast(err.message, 'error');
        }
      });
    });
  }

  // ------------------------------------------------------------ My Profile

  function initProfile() {
    const form = $('profile-form');
    const F = form.elements;
    let saved = null;

    function setAvatar(url) {
      saved.avatar_url = url;
      document.querySelectorAll('[data-avatar]').forEach((el) => setImage(el, url, saved.initials));
      $('avatar-remove').classList.toggle('hidden', !url);
    }

    function fill() {
      F.full_name.value = saved.full_name;
      F.email.value = saved.email;
      F.phone.value = saved.phone;
      $('pf-role').value = saved.role_label;
      $('pf-agency').value = saved.agency_name || 'Not assigned';
      const note = $('profile-email-note');
      note.textContent = saved.pending_email
        ? `Waiting for confirmation of ${saved.pending_email}. Your email changes once you open the link sent there.` : '';
      note.classList.toggle('hidden', !saved.pending_email);
    }

    function render(profile) {
      saved = profile;
      $('profile-name').textContent = profile.full_name || '—';
      $('profile-email').textContent = profile.email;
      $('profile-role').textContent = profile.role_label;
      $('profile-agency').textContent = profile.agency_name || '';
      const status = $('profile-status');
      status.textContent = profile.status;
      status.className = `font-semibold mt-0.5 ${profile.status === 'Active' ? 'text-emerald-400' : 'text-amber-300'}`;
      $('profile-since').textContent = profile.member_since || '—';
      document.querySelectorAll('[data-user-name]').forEach((el) => { el.textContent = profile.full_name || profile.email; });
      setAvatar(profile.avatar_url);
      fill();
    }

    form.querySelector('[data-cancel]').addEventListener('click', () => { clearErrors(form); fill(); });

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      clearErrors(form);
      const body = { full_name: F.full_name.value.trim(), email: F.email.value.trim(), phone: F.phone.value.trim() };
      const errs = {};
      if (!body.full_name) errs.full_name = 'Full name is required.';
      if (!body.email) errs.email = 'Email address is required.';
      else if (!EMAIL_RE.test(body.email)) errs.email = 'Enter a valid email address.';
      if (body.phone && !phoneOk(body.phone)) errs.phone = PHONE_HINT;
      if (Object.keys(errs).length) return showErrors(form, errs, 'Please fix the highlighted fields.').focus();

      await busy(ev.submitter, async () => {
        try {
          const data = await api(cfg.api.profile, { method: 'PUT', body });
          render(data.profile);
          toast(data.message, 'success');
        } catch (err) {
          showErrors(form, err.fields, err.message).focus();
        }
      });
    });

    wireImage({ input: $('avatar-file'), change: $('avatar-change'), remove: $('avatar-remove'), url: () => cfg.api.avatar, label: 'Profile photo', onChange: setAvatar });

    return async () => render((await api(cfg.api.profile)).profile);
  }

  // ------------------------------------------------------------ Agency Settings

  function initAgency() {
    const form = $('agency-form');
    const F = form.elements;
    const FIELDS = ['agency_name', 'agency_email', 'psap_phone', 'agency_address', 'agency_description', 'agency_type', 'jurisdiction'];
    const PANEL = { agency_type: 'config', jurisdiction: 'config', status: 'config' };
    const picker = $('agency-select');
    let saved = null;
    let canEdit = false;
    let systemAdmin = false;
    let agenciesLoaded = false;
    let agencyId = cfg.systemAdmin ? new URLSearchParams(location.search).get('agency') : null;

    // System administrators act on the agency they picked; everyone else on their own.
    const scoped = (url) => (agencyId ? `${url}${url.includes('?') ? '&' : '?'}agency=${encodeURIComponent(agencyId)}` : url);

    const tabs = document.querySelectorAll('[data-agency-tab]');
    function selectTab(key) {
      tabs.forEach((t) => {
        const on = t.dataset.agencyTab === key;
        t.setAttribute('aria-selected', String(on));
        t.tabIndex = on ? 0 : -1;
        $(`agency-panel-${t.dataset.agencyTab}`).classList.toggle('hidden', !on);
      });
      $('agency-form-footer').classList.toggle('hidden', key === 'personnel');
    }
    tabs.forEach((t) => {
      t.addEventListener('click', () => selectTab(t.dataset.agencyTab));
      t.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        const visible = [...tabs].filter((x) => !x.classList.contains('hidden'));
        const next = visible[(visible.indexOf(t) + (e.key === 'ArrowRight' ? 1 : visible.length - 1)) % visible.length];
        selectTab(next.dataset.agencyTab);
        next.focus();
      });
    });

    function setLogo(url) {
      saved.agency_logo_url = url;
      setImage($('agency-logo'), url, icon('apartment', 'text-[1.875rem]'));
      $('logo-remove').classList.toggle('hidden', !url);
    }

    function fill() {
      for (const name of FIELDS) F[name].value = saved[name] || '';
      F.ori_code.value = saved.ori_code || (systemAdmin ? '' : 'Not assigned');
      F.cad_cert.value = saved.cad_cert || 'Not recorded';
      F.status.value = saved.status || 'pending';
    }

    const STATUS = {
      verified: ['Verified', 'text-emerald-300 border-emerald-500/40 bg-emerald-500/10'],
      pending: ['Pending verification', 'text-amber-300 border-amber-500/40 bg-amber-500/10'],
      suspended: ['Suspended', 'text-red-300 border-red-500/40 bg-red-500/10'],
    };

    function renderAgency(agency) {
      saved = agency;
      $('agency-title').textContent = agency.agency_name || 'Agency name not set';
      $('agency-code').textContent = agency.ori_code || 'No agency code';
      const [text, cls] = STATUS[agency.status] || [agency.status || 'Unknown', 'text-slate-300 border-slate-600'];
      const badge = $('agency-status');
      badge.textContent = text;
      badge.className = `px-2 py-0.5 rounded text-[0.6875rem] font-semibold border ${cls}`;
      $('agency-updated').textContent = agency.updated_at ? `Last saved ${fmtDateTime(agency.updated_at)}` : '';
      setLogo(agency.agency_logo_url);
      fill();
    }

    function renderPersonnel(people) {
      const list = $('personnel-list');
      if (!people.length) {
        list.replaceChildren(h('li', { class: 'py-3 text-slate-500 text-xs', text: 'No responders have been added to this agency yet.' }));
        return;
      }
      list.replaceChildren(...people.map((p) => h('li', { class: 'py-2 flex items-center justify-between gap-3' },
        h('div', { class: 'min-w-0' },
          h('p', { class: 'text-white truncate', text: p.full_name || p.email }),
          h('p', { class: 'text-xs text-slate-400 truncate', text: [p.email, p.station].filter(Boolean).join(' · ') })),
        h('button', { type: 'button', class: 'text-xs text-red-300 underline shrink-0', onclick: (e) => removePerson(p, e.currentTarget), text: 'Remove' }))));
    }

    function render(data) {
      canEdit = data.can_edit;
      systemAdmin = data.system_admin;
      $('agency-denied').classList.add('hidden');
      $('agency-content').classList.remove('hidden');
      $('agency-readonly').classList.toggle('hidden', canEdit);
      document.querySelectorAll('[data-edit-only]').forEach((el) => el.classList.toggle('hidden', !canEdit));
      for (const el of form.elements) if (el.name && el.name !== 'cad_cert' && el.name !== 'ori_code') el.disabled = !canEdit;
      F.ori_code.readOnly = !systemAdmin;
      $('agency-status-field').classList.toggle('hidden', !systemAdmin);
      $('agency-tab-personnel').classList.toggle('hidden', !canEdit || data.personnel_ready === false);
      renderAgency(data.agency);
      renderPersonnel(data.personnel || []);
      selectTab('general');
    }

    function deny(message) {
      $('agency-content').classList.add('hidden');
      const box = $('agency-denied');
      box.querySelector('[data-text]').textContent = message;
      box.classList.remove('hidden');
    }

    form.querySelector('[data-cancel]').addEventListener('click', () => { clearErrors(form); fill(); });

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      if (!canEdit) return;
      clearErrors(form);
      const body = Object.fromEntries(FIELDS.map((name) => [name, F[name].value.trim()]));
      if (systemAdmin) Object.assign(body, { ori_code: F.ori_code.value.trim().toUpperCase(), status: F.status.value });
      const errs = {};
      if (!body.agency_name) errs.agency_name = 'Agency name is required.';
      if (!body.agency_email) errs.agency_email = 'Agency email is required.';
      else if (!EMAIL_RE.test(body.agency_email)) errs.agency_email = 'Enter a valid email address.';
      if (!body.psap_phone) errs.psap_phone = 'Contact number is required.';
      else if (!phoneOk(body.psap_phone)) errs.psap_phone = PHONE_HINT;
      if (!body.agency_address) errs.agency_address = 'Office address is required.';
      if (!body.agency_type) errs.agency_type = 'Choose an agency type.';
      if (!body.jurisdiction) errs.jurisdiction = 'Jurisdiction is required.';

      const fail = (fields, message) => {
        const firstField = Object.keys(fields || {})[0];
        if (firstField) selectTab(PANEL[firstField] || 'general');   // the error may be on the other tab
        showErrors(form, fields, message).focus();
      };
      if (Object.keys(errs).length) return fail(errs, 'Please fix the highlighted fields.');

      await busy(ev.submitter, async () => {
        try {
          const data = await api(scoped(cfg.api.agency), { method: 'PUT', body });
          renderAgency(data.agency);
          toast(data.message, 'success');
        } catch (err) {
          fail(err.fields, err.message);
        }
      });
    });

    const personnelForm = $('personnel-form');
    personnelForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      clearErrors(personnelForm);
      const email = personnelForm.elements.email.value.trim();
      if (!EMAIL_RE.test(email)) return showErrors(personnelForm, { email: 'Enter a valid email address.' }).focus();
      await busy(ev.submitter, async () => {
        try {
          const data = await api(scoped(cfg.api.personnel), { method: 'POST', body: { email } });
          personnelForm.reset();
          renderPersonnel(data.personnel);
          toast(data.message, 'success');
        } catch (err) {
          showErrors(personnelForm, err.fields || { email: err.message }).focus();
        }
      });
    });

    async function removePerson(person, btn) {
      if (!(await confirmDialog('Remove this responder?', `${person.full_name || person.email} will no longer be able to view this agency's information.`, 'Remove'))) return;
      await busy(btn, async () => {
        try {
          const data = await api(scoped(`${cfg.api.personnel}/${person.id}`), { method: 'DELETE' });
          renderPersonnel(data.personnel);
          toast(data.message, 'success');
        } catch (err) {
          toast(err.message, 'error');
        }
      });
    }

    wireImage({ input: $('logo-file'), change: $('logo-change'), remove: $('logo-remove'), url: () => scoped(cfg.api.logo), label: 'Agency logo', onChange: setLogo });

    picker.addEventListener('change', () => {
      agencyId = picker.value || null;
      const url = new URL(location.href);
      if (agencyId) url.searchParams.set('agency', agencyId); else url.searchParams.delete('agency');
      history.replaceState(null, '', url);
      load();
    });

    async function loadAgencies() {
      const { items } = await api(cfg.api.agencies);
      picker.replaceChildren(h('option', { value: '', text: items.length ? 'Select an agency…' : 'No agencies are registered yet' }),
        ...items.map((a) => h('option', { value: a.id, text: [a.agency_name, a.ori_code].filter(Boolean).join(' · ') })));
      if (agencyId && !items.some((a) => a.id === agencyId)) agencyId = null;
      picker.value = agencyId || '';
      $('agency-picker').classList.remove('hidden');
      agenciesLoaded = true;
    }

    return async () => {
      if (cfg.systemAdmin) {
        if (!agenciesLoaded) await loadAgencies();
        if (!agencyId) {
          $('agency-content').classList.add('hidden');
          $('agency-denied').classList.add('hidden');
          return;
        }
      }
      try {
        render(await api(scoped(cfg.api.agency)));
      } catch (err) {
        if ([400, 403, 404].includes(err.status)) return deny(err.message);   // not an outage: explain instead of "try again"
        throw err;
      }
    };
  }

  // ------------------------------------------------------------ Security & MFA

  function initSecurity() {
    const pwForm = $('password-form');
    const rulesEl = $('password-rules');
    const TESTS = {
      length: (v) => v.length >= cfg.minPassword,
      upper: (v) => /\p{Lu}/u.test(v),
      lower: (v) => /\p{Ll}/u.test(v),
      digit: (v) => /\d/.test(v),
      special: (v) => /[^\p{L}\p{N}\s]/u.test(v),
    };
    function checkRules() {
      const value = pwForm.elements.new_password.value;
      let all = true;
      for (const li of rulesEl.querySelectorAll('[data-rule]')) {
        const met = TESTS[li.dataset.rule](value);
        all = all && met;
        li.dataset.met = String(met);
        li.querySelector('.material-symbols-outlined').textContent = met ? 'check_circle' : 'radio_button_unchecked';
        li.querySelector('[data-rule-state]').textContent = met ? 'met' : 'not met';
      }
      return all;
    }
    pwForm.elements.new_password.addEventListener('input', checkRules);

    const seen = (data) => window.ESRMSNotify?.seen(data.event_id);

    pwForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      clearErrors(pwForm);
      const P = pwForm.elements;
      const body = { current_password: P.current_password.value, new_password: P.new_password.value, confirm_password: P.confirm_password.value };
      const errs = {};
      if (!body.current_password) errs.current_password = 'Enter your current password.';
      if (!body.new_password) errs.new_password = 'Enter a new password.';
      else if (!checkRules()) errs.new_password = 'The new password does not meet all the requirements.';
      else if (body.new_password === body.current_password) errs.new_password = 'The new password must be different from the current one.';
      if (body.new_password && body.confirm_password !== body.new_password) errs.confirm_password = 'Passwords do not match.';
      if (Object.keys(errs).length) return showErrors(pwForm, errs, 'Please fix the highlighted fields.').focus();
      if (!(await confirmDialog('Change your password?', 'Your other devices will be signed out and will need the new password.', 'Change Password'))) return;

      await busy(ev.submitter, async () => {
        try {
          const data = await api(cfg.api.password, { method: 'POST', body });
          pwForm.reset();
          checkRules();
          seen(data);
          toast(data.message, 'success');
          if (data.warning) toast(data.warning, 'error');
          load();
        } catch (err) {
          showErrors(pwForm, err.fields, err.message).focus();
        }
      });
    });

    // ---- a current authenticator code is required before sensitive MFA changes
    const codeDialog = $('code-dialog');
    const codeForm = $('code-form');
    let codeAction = null;
    function askCode(title, text, ok, action) {
      codeDialog.querySelector('[data-title]').textContent = title;
      codeDialog.querySelector('[data-text]').textContent = text;
      codeDialog.querySelector('[data-ok]').textContent = ok;
      codeAction = action;
      codeForm.reset();
      clearErrors(codeForm);
      codeDialog.showModal();
      codeForm.elements.code.focus();
    }
    codeForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      clearErrors(codeForm);
      const code = codeForm.elements.code.value.replace(/\s/g, '');
      if (!/^\d{6}$/.test(code)) return showErrors(codeForm, { code: 'Enter the 6-digit code.' }).focus();
      await busy(ev.submitter, async () => {
        try {
          const data = await codeAction(code);
          codeDialog.close();
          seen(data);
          if (data.message) toast(data.message, 'success');
          load();
        } catch (err) {
          showErrors(codeForm, err.fields, err.message).focus();
        }
      });
    });

    // ---- authenticator enrollment
    const mfaDialog = $('mfa-dialog');
    const mfaForm = $('mfa-form');
    let factorId = null;
    let hasMfa = false;

    function resetEnrollment() {
      // Don't leave the secret in the page once the dialog is gone.
      factorId = null;
      $('mfa-qr').replaceChildren('Loading…');
      $('mfa-secret').textContent = '';
      mfaForm.reset();
      clearErrors(mfaForm);
    }
    mfaDialog.addEventListener('close', resetEnrollment);
    for (const d of [mfaDialog, codeDialog]) d.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => d.close()));

    $('mfa-enable').addEventListener('click', (ev) => busy(ev.currentTarget, async () => {
      try {
        const data = await api(cfg.api.mfaEnroll, { method: 'POST', body: { name: hasMfa ? 'Backup authenticator' : 'Authenticator' } });
        factorId = data.factor_id;
        const svg = data.qr_code.replace(/^data:image\/svg\+xml;utf-8,/, '');
        $('mfa-qr').replaceChildren(h('img', { src: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`, alt: 'QR code to scan with your authenticator app', class: 'w-full h-full' }));
        $('mfa-secret').textContent = data.secret;
        mfaDialog.showModal();
        mfaForm.elements.code.focus();
      } catch (err) {
        toast(err.message, 'error');
      }
    }));

    mfaForm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      clearErrors(mfaForm);
      const code = mfaForm.elements.code.value.replace(/\s/g, '');
      if (!/^\d{6}$/.test(code)) return showErrors(mfaForm, { code: 'Enter the 6-digit code.' }).focus();
      await busy(ev.submitter, async () => {
        try {
          const data = await api(cfg.api.mfaVerify, { method: 'POST', body: { factor_id: factorId, code } });
          mfaDialog.close();
          seen(data);
          toast(data.message, 'success');
          load();
        } catch (err) {
          showErrors(mfaForm, err.fields, err.message).focus();
        }
      });
    });

    $('mfa-disable').addEventListener('click', () => askCode(
      'Disable multi-factor authentication?',
      'Every authenticator and recovery code is removed and your account is protected by your password only. Enter a current code to confirm.',
      'Disable MFA', (code) => api(cfg.api.mfaDisable, { method: 'POST', body: { code } })));

    // ---- recovery codes (shown once)
    const recoveryDialog = $('recovery-dialog');
    const recoveryList = $('recovery-codes');
    const shownCodes = () => [...recoveryList.children].map((li) => li.textContent);
    $('recovery-generate').addEventListener('click', () => askCode(
      'Generate recovery codes',
      'You get 10 new single-use codes and any earlier codes stop working. Enter a current authenticator code to continue.',
      'Generate', async (code) => {
        const data = await api(cfg.api.recoveryCodes, { method: 'POST', body: { code } });
        recoveryList.replaceChildren(...data.codes.map((c) => h('li', { text: c })));
        recoveryDialog.showModal();
        return { event_id: data.event_id };
      }));
    $('recovery-copy').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(shownCodes().join('\n')); toast('Recovery codes copied.', 'success'); } catch (_) { toast('Copy failed. Select the codes and copy them manually.', 'error'); }
    });
    $('recovery-download').addEventListener('click', () => {
      const text = `ESRMS MFA recovery codes\nGenerated ${new Date().toISOString()}\nEach code works once.\n\n${shownCodes().join('\n')}\n`;
      const a = h('a', { href: URL.createObjectURL(new Blob([text], { type: 'text/plain' })), download: 'esrms-recovery-codes.txt' });
      a.click();
      URL.revokeObjectURL(a.href);
    });
    $('recovery-done').addEventListener('click', () => recoveryDialog.close());
    recoveryDialog.addEventListener('close', () => recoveryList.replaceChildren());

    // ---- sessions
    async function sessionAction(btn, title, text, ok, run) {
      if (!(await confirmDialog(title, text, ok))) return;
      await busy(btn, async () => {
        try {
          const data = await run();
          seen(data);
          if (data.redirect) { window.location.assign(data.redirect); return; }
          toast(data.message, 'success');
          load();
        } catch (err) {
          toast(err.message, 'error');
        }
      });
    }
    $('session-signout').addEventListener('click', async () => {
      if (await confirmDialog('Sign out this device?', 'You will need your password (and authenticator code, if MFA is on) to sign in again.', 'Sign Out')) window.location.assign(cfg.api.signOut);
    });
    $('sessions-revoke').addEventListener('click', (ev) => sessionAction(ev.currentTarget, 'Sign out other sessions?',
      'Every other browser and device signed in to this account will be signed out. This one stays signed in.', 'Sign Out Others',
      () => api(cfg.api.revokeSessions, { method: 'POST', body: {} })));
    $('sessions-revoke-all').addEventListener('click', (ev) => sessionAction(ev.currentTarget, 'Sign out all devices?',
      'Every device, including this one, will be signed out of your account.', 'Sign Out Everywhere',
      () => api(cfg.api.revokeAllSessions, { method: 'POST', body: {} })));

    function renderSessions(data) {
      const list = $('session-list');
      const s = data.sessions;
      const row = (item) => h('li', { class: 'py-2.5 flex items-center justify-between gap-3' },
        h('div', { class: 'min-w-0' },
          h('p', { class: 'text-white truncate' }, device(item.user_agent),
            item.current && h('span', { class: 'ml-2 px-1.5 py-px rounded text-[0.625rem] font-semibold bg-brand-cyan/15 text-brand-cyan border border-brand-cyan/40', text: 'This device' }),
            item.mfa_verified && h('span', { class: 'ml-2 text-[0.625rem] text-emerald-400', text: 'MFA verified' })),
          h('p', { class: 'text-xs text-slate-400', text: `Last active ${fmtDateTime(item.last_active_at)} · signed in ${fmtDateTime(item.created_at)}` })),
        !item.current && h('button', {
          type: 'button', class: 'text-xs text-red-300 underline shrink-0',
          onclick: (e) => sessionAction(e.currentTarget, 'Sign out that device?', `${device(item.user_agent)} will be signed out of your account.`, 'Sign Out',
            () => api(`${cfg.api.sessions}/${item.id}/revoke`, { method: 'POST', body: {} })),
          text: 'Sign out',
        }));
      if (s.available && s.items.length) {
        list.replaceChildren(...s.items.map(row));
        $('session-note').textContent = 'Supabase Auth records each session’s browser and last activity. It does not provide a location, and IP addresses are not shown because sign-ins pass through the ESRMS server. A signed-out device loses access within about a minute.';
      } else {
        // Only what is actually known: this browser.
        list.replaceChildren(row({ user_agent: data.current_session.user_agent, current: true, created_at: data.current_session.last_sign_in_at, last_active_at: new Date().toISOString() }));
        $('session-note').textContent = s.available
          ? 'No other sessions were found.'
          : `${s.reason} Until then only this device can be shown; “Sign out other sessions” still works.`;
      }
    }

    function render(data) {
      $('security-relogin').classList.toggle('hidden', data.can_manage);
      $('sign-in-methods').textContent = data.providers.map((p) => p.label).join(' + ') || 'Unknown';
      pwForm.classList.toggle('hidden', !data.has_password);
      $('password-google').classList.toggle('hidden', data.has_password);

      const mfa = data.mfa;
      hasMfa = mfa.enabled;
      const status = $('mfa-status');
      status.textContent = mfa.enabled ? 'Enabled' : 'Not Enabled';
      status.className = mfa.enabled ? 'text-emerald-400' : 'text-red-400';
      const add = $('mfa-enable');
      add.textContent = mfa.enabled ? 'Add Backup Authenticator' : 'Enable MFA';
      add.classList.toggle('hidden', mfa.factors.length >= mfa.max_factors);
      $('mfa-disable').classList.toggle('hidden', !mfa.enabled);
      $('mfa-details').classList.toggle('hidden', !mfa.enabled);
      $('mfa-backup-hint').classList.toggle('hidden', mfa.factors.length > 1);
      for (const id of ['mfa-enable', 'mfa-disable', 'sessions-revoke', 'sessions-revoke-all', 'recovery-generate']) $(id).disabled = !data.can_manage;
      document.querySelectorAll('[data-mfa-badge]').forEach((el) => {
        el.textContent = mfa.enabled ? 'MFA on' : 'MFA off';
        el.classList.toggle('text-emerald-400', mfa.enabled);
        el.classList.toggle('text-slate-500', !mfa.enabled);
      });

      $('mfa-factors').replaceChildren(...mfa.factors.map((f) => h('li', { class: 'py-2 flex items-center justify-between gap-3' },
        h('div', { class: 'min-w-0' },
          h('p', { class: 'text-white truncate', text: f.name }),
          h('p', { class: 'text-xs text-slate-400', text: `Added ${fmtDateTime(f.created_at)}` })),
        h('button', {
          type: 'button', class: 'text-xs text-red-300 underline shrink-0 disabled:opacity-50', disabled: !data.can_manage,
          onclick: () => askCode(`Remove “${f.name}”?`,
            mfa.factors.length === 1 ? 'This is your only authenticator, so removing it turns MFA off. Enter a current code to confirm.'
              : 'Codes from this authenticator will stop working. Enter a current code from any of your authenticators to confirm.',
            'Remove', (code) => api(`${cfg.api.mfaFactors}/${f.id}`, { method: 'DELETE', body: { code } })),
          text: 'Remove',
        }))));

      const codes = mfa.recovery_codes;
      const rs = $('recovery-status');
      if (!codes.available) {
        rs.textContent = 'Recovery codes are unavailable until the database migration is applied.';
        rs.className = 'text-sm mt-2 text-amber-300';
        $('recovery-generate').disabled = true;
      } else if (!codes.generated_at) {
        rs.textContent = 'No recovery codes yet. Generate them now.';
        rs.className = 'text-sm mt-2 text-amber-300';
      } else {
        rs.textContent = `${codes.remaining} unused code${codes.remaining === 1 ? '' : 's'} · generated ${fmtDateTime(codes.generated_at)}`;
        rs.className = `text-sm mt-2 ${codes.remaining > 2 ? 'text-slate-200' : 'text-amber-300'}`;
      }
      $('recovery-generate').textContent = codes.generated_at ? 'Generate New Codes' : 'Generate Recovery Codes';

      renderSessions(data);

      const list = $('security-events');
      if (data.events_error) {
        list.replaceChildren(h('li', { class: 'py-2 text-amber-300', text: 'Security activity is unavailable right now.' }));
      } else if (!data.events.length) {
        list.replaceChildren(h('li', { class: 'py-2 text-slate-500', text: 'No security changes recorded yet.' }));
      } else {
        list.replaceChildren(...data.events.map((e) => h('li', { class: 'py-2 flex flex-wrap justify-between gap-x-4 gap-y-0.5' },
          h('span', { class: 'text-slate-200', text: cfg.securityEvents[e.event] || e.event }),
          h('span', { class: 'text-slate-500', text: `${fmtDateTime(e.created_at)}${e.user_agent ? ` · ${device(e.user_agent)}` : ''}` }))));
      }
    }

    return async () => render(await api(cfg.api.security));
  }

  // ------------------------------------------------------------ Notifications

  function initNotifications() {
    const form = $('notifications-form');
    const F = form.elements;
    const KEYS = [...Object.keys(cfg.categories), 'browser_enabled', 'email_enabled'];   // saved preferences
    const N = window.ESRMSNotify;
    const permissionEl = $('browser-permission');
    const permissionDialog = $('notify-permission');
    const pushSupported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    let saved = null;
    let delivery = null;
    let subscription = null;   // this browser's push subscription, if any

    const PERMISSION_TEXT = {
      granted: ['Notifications are allowed on this device.', 'text-emerald-400'],
      denied: ['Notifications are blocked for this site. Allow them from the lock icon in your browser’s address bar, then switch this on.', 'text-red-400'],
      default: ['Your browser will ask for permission when you switch this on.', 'text-slate-500'],
      unsupported: ['This browser does not support notifications.', 'text-amber-300'],
    };
    function showPermission() {
      const [text, cls] = PERMISSION_TEXT[N.permission()];
      permissionEl.textContent = text;
      permissionEl.className = `text-[0.6875rem] pb-2 pl-9 ${cls}`;
    }

    // Explain first, then let the browser ask. Resolves true only if permission ends up granted.
    async function ensurePermission() {
      if (N.permission() === 'default') {
        permissionDialog.returnValue = '';
        permissionDialog.showModal();
        await new Promise((resolve) => permissionDialog.addEventListener('close', resolve, { once: true }));
        if (permissionDialog.returnValue === 'ok') {
          try { await Notification.requestPermission(); } catch (_) { /* reported below */ }
        }
      }
      showPermission();
      if (N.permission() === 'granted') return true;
      if (N.permission() !== 'default') toast(PERMISSION_TEXT[N.permission()][0], 'error');
      return false;
    }

    function setStatus(el, text, cls = 'text-slate-500') {
      el.textContent = text;
      el.className = `text-[0.6875rem] pl-9 ${cls}`;
    }

    function renderDelivery() {
      const email = delivery.email;
      F.email_enabled.disabled = !email.available;
      if (!email.available) F.email_enabled.checked = false;
      setStatus($('email-status'), email.available ? `Alerts for the categories you keep on are emailed to ${delivery.address}.` : email.reason,
        email.available ? 'text-slate-500' : 'text-amber-300');
      $('email-test').disabled = !email.available;

      let reason = null;
      if (!delivery.push.available) reason = delivery.push.reason;
      else if (!window.isSecureContext) reason = 'Push needs ESRMS to be opened over HTTPS.';
      else if (!pushSupported) reason = 'This browser does not support push notifications. On iPhone and iPad, add ESRMS to the Home Screen first.';
      F.push_device.disabled = Boolean(reason);
      F.push_device.checked = !reason && Boolean(subscription);
      $('push-test').disabled = Boolean(reason) || !subscription;
      if (reason) setStatus($('push-status'), reason, 'text-amber-300');
      else if (subscription) setStatus($('push-status'), `On for this device. ${delivery.devices} device${delivery.devices === 1 ? '' : 's'} subscribed on your account.`, 'text-emerald-400');
      else setStatus($('push-status'), N.permission() === 'denied' ? PERMISSION_TEXT.denied[0] : 'Off for this device.', N.permission() === 'denied' ? 'text-red-400' : 'text-slate-500');
    }

    const STATUS = {
      sent: ['Accepted by provider', 'text-emerald-400'], pending: ['Queued', 'text-slate-400'], sending: ['Sending', 'text-slate-400'],
      failed: ['Failed, will retry', 'text-amber-300'], dead: ['Not delivered', 'text-red-400'],
    };
    function renderLog(items) {
      const list = $('delivery-log');
      if (!items.length) {
        list.replaceChildren(h('li', { class: 'py-2 text-slate-500', text: 'Nothing has been sent to you by email or push yet.' }));
        return;
      }
      list.replaceChildren(...items.map((d) => {
        const [text, cls] = STATUS[d.status] || [d.status, 'text-slate-400'];
        return h('li', { class: 'py-2 flex flex-wrap justify-between gap-x-4 gap-y-0.5' },
          h('span', { class: 'text-slate-200' }, `${d.channel === 'email' ? 'Email' : 'Push'} · ${d.title}`),
          h('span', {}, h('span', { class: cls, text }), h('span', { class: 'text-slate-500', text: ` · ${fmtDateTime(d.sent_at || d.created_at)}` }),
            d.status !== 'sent' && d.last_error && h('span', { class: 'block text-slate-500', text: d.last_error })));
      }));
    }

    function fill() {
      for (const key of KEYS) F[key].checked = saved[key];
      showPermission();
      renderDelivery();
    }

    function render(data) {
      saved = data.preferences;
      delivery = { email: data.delivery.email, push: data.delivery.push, address: data.email, devices: data.devices.length };
      N.setPreferences(saved);
      $('notifications-saved').textContent = data.saved
        ? `Last saved ${fmtDateTime(data.updated_at)}`
        : 'Using the default preferences. Nothing has been saved yet.';
      renderLog(data.recent);
      fill();
    }

    // ---- push: this browser's subscription
    const keyBytes = (b64) => Uint8Array.from(atob(b64.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(b64.length / 4) * 4, '=')), (c) => c.charCodeAt(0));

    async function findSubscription() {
      if (!pushSupported || !window.isSecureContext) return null;
      try {
        const reg = await navigator.serviceWorker.getRegistration('/');
        return reg ? await reg.pushManager.getSubscription() : null;
      } catch (_) {
        return null;
      }
    }

    async function subscribe() {
      if (!(await ensurePermission())) return false;
      const reg = await navigator.serviceWorker.register(cfg.serviceWorker, { scope: '/' });
      await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(delivery.push.public_key) });
      try {
        await api(cfg.api.pushSubscribe, { method: 'POST', body: { subscription: sub.toJSON() } });
      } catch (err) {
        await sub.unsubscribe().catch(() => {});   // don't leave a subscription the server doesn't know about
        throw err;
      }
      subscription = sub;
      N.setPushActive(true);
      return true;
    }

    async function unsubscribe() {
      const endpoint = subscription.endpoint;
      await api(cfg.api.pushUnsubscribe, { method: 'POST', body: { endpoint } });
      await subscription.unsubscribe().catch(() => {});
      subscription = null;
      N.setPushActive(false);
    }

    let pushBusy = false;
    F.push_device.addEventListener('change', async () => {
      if (pushBusy) { F.push_device.checked = !F.push_device.checked; return; }
      pushBusy = true;
      const turnOn = F.push_device.checked;
      try {
        if (turnOn) {
          if (await subscribe()) toast('Push notifications are on for this device.', 'success');
        } else {
          await unsubscribe();
          toast('Push notifications are off for this device.', 'success');
        }
        await load();
      } catch (err) {
        toast(err.name === 'NotAllowedError' ? PERMISSION_TEXT.denied[0] : `Push could not be ${turnOn ? 'turned on' : 'turned off'}: ${err.message}`, 'error');
        renderDelivery();
      } finally {
        pushBusy = false;
      }
    });

    F.browser_enabled.addEventListener('change', async () => {
      if (F.browser_enabled.checked && !(await ensurePermission())) F.browser_enabled.checked = false;
    });

    async function sendTest(btn, body) {
      await busy(btn, async () => {
        try {
          toast((await api(cfg.api.notificationTest, { method: 'POST', body })).message, 'info');
        } catch (err) {
          toast(err.message, 'error');
        }
      });
    }
    $('email-test').addEventListener('click', (ev) => sendTest(ev.currentTarget, { channel: 'email' }));
    $('push-test').addEventListener('click', (ev) => sendTest(ev.currentTarget, { channel: 'push', endpoint: subscription?.endpoint }));

    form.querySelector('[data-cancel]').addEventListener('click', () => { clearErrors(form); fill(); });

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      clearErrors(form);
      const body = Object.fromEntries(KEYS.map((key) => [key, F[key].checked]));
      await busy(ev.submitter, async () => {
        try {
          const data = await api(cfg.api.notifications, { method: 'PUT', body });
          render(data);
          toast(data.message, 'success');
        } catch (err) {
          showErrors(form, null, err.message).focus();
        }
      });
    });

    return async () => {
      const data = await api(cfg.api.notifications);
      subscription = await findSubscription();
      // Subscribed with a different server key (the VAPID key was replaced): that subscription can no longer be used.
      if (subscription && data.delivery.push.available && subscription.options.applicationServerKey
          && new Uint8Array(subscription.options.applicationServerKey).join() !== keyBytes(data.delivery.push.public_key).join()) {
        await api(cfg.api.pushUnsubscribe, { method: 'POST', body: { endpoint: subscription.endpoint } }).catch(() => {});
        await subscription.unsubscribe().catch(() => {});
        subscription = null;
        N.setPushActive(false);
      }
      // The browser is subscribed but the server has no record (e.g. another account used this browser): re-link it.
      if (subscription && data.delivery.push.available && !data.devices.some((d) => d.endpoint === subscription.endpoint)) {
        try {
          await api(cfg.api.pushSubscribe, { method: 'POST', body: { subscription: subscription.toJSON() } });
          data.devices.push({ endpoint: subscription.endpoint });
        } catch (_) {
          subscription = null;
        }
      }
      render(data);
    };
  }

  // ------------------------------------------------------------ Display

  function initDisplay() {
    const buttons = [...$('display-scales').querySelectorAll('[data-scale]')];
    const savedEl = $('display-saved');
    const root = document.documentElement;
    let saved = Number(root.dataset.displayScale);   // what the server drew this page at
    let saveSeq = 0;

    // Every size in ESRMS is in rem, so the root text size scales the whole interface. Not browser zoom.
    function apply(scale) {
      root.style.fontSize = `${scale}%`;
      root.dataset.displayScale = String(scale);
      for (const b of buttons) b.setAttribute('aria-pressed', String(Number(b.dataset.scale) === scale));
      window.dispatchEvent(new Event('resize'));   // maps and charts measure themselves again
    }

    function render(data) {
      saved = data.display_scale;
      apply(saved);
      savedEl.textContent = data.saved ? `Saved to your account ${fmtDateTime(data.updated_at)}.` : `Using the default size (${data.default}%). Nothing has been saved yet.`;
    }

    for (const btn of buttons) {
      btn.addEventListener('click', async () => {
        const scale = Number(btn.dataset.scale);
        if (btn.getAttribute('aria-pressed') === 'true' && scale === saved) return;
        const seq = ++saveSeq;
        apply(scale);   // straight away; put back below if the server does not confirm it
        savedEl.textContent = 'Saving…';
        try {
          const data = await api(cfg.api.display, { method: 'PUT', body: { display_scale: scale } });
          if (seq !== saveSeq) return;   // a later click is already being saved
          render(data);
          toast(data.message, 'success');
        } catch (err) {
          if (seq !== saveSeq) return;
          apply(saved);
          savedEl.textContent = 'Not saved.';
          toast(err.message, 'error');
        }
      });
    }

    return async () => render(await api(cfg.api.display));
  }

  // ------------------------------------------------------------ start

  const init = { display: initDisplay, profile: initProfile, agency: initAgency, security: initSecurity, notifications: initNotifications }[cfg.section];
  const fetchAndRender = init();   // listeners are attached exactly once, here

  let loadSeq = 0;
  async function load() {
    const seq = ++loadSeq;
    const first = bodyEl.classList.contains('hidden');
    if (first) show('loading');
    try {
      await fetchAndRender();
      if (seq === loadSeq) show('ready');
    } catch (err) {
      if (seq !== loadSeq) return;
      if (err.status === 401) { window.location.assign(cfg.signOutUrl); return; }
      if (first) show('error', err.message); else toast(err.message, 'error');
    }
  }
  $('account-retry').addEventListener('click', load);
  load();
})();
