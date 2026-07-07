(function () {
 'use strict';

 function todayIso() {
  const now = new Date();
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, '0');
  const dd = String(now.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
 }

 const DATE_INPUT_SELECTOR = 'input[type="date"], input[data-ipm-date-input="1"]';
 const FILING_DEADLINE_DATE_SELECTOR =
  'input[type="date"][name="filing_deadline"], input[data-ipm-date-input="1"][name="filing_deadline"]';

 function findInputForButton(button, selector) {
  if (!button) return null;
  const targetId = button.getAttribute('data-target-id');
  if (targetId) return document.getElementById(targetId);
  const group = button.closest('.input-group');
  if (group) {
   const inGroup = group.querySelector(selector);
   if (inGroup) return inGroup;
  }
  return document.querySelector(selector);
 }

 function cssEscape(value) {
  if (window.CSS && CSS.escape) return CSS.escape(value);
  return String(value || '').replace(/["\\]/g, '\\$&');
 }

 function ipmAlert(message, opts) {
  try {
   if (window.AppAlert) return window.AppAlert(message, opts);
  } catch (e) {}
  return Promise.resolve();
 }

 function syncNamedFieldFrom(source) {
  if (!source) return;
  const name = (source.getAttribute('data-sync-field-name') || '').trim();
  if (!name) return;
  const form = source.form || source.closest('form') || document;
  const selector = `[name="${cssEscape(name)}"]`;
  const value = (source.value || '').trim().toUpperCase();
  if (source.value !== value) source.value = value;
  form.querySelectorAll(selector).forEach((field) => {
   if (field === source) return;
   if (field.value === value) return;
   field.value = value;
   field.dispatchEvent(new Event('input', { bubbles: true }));
   field.dispatchEvent(new Event('change', { bubbles: true }));
  });
 }

 function initSyncedFields() {
  document.querySelectorAll('[data-sync-field-name]').forEach((source) => {
   if (source.dataset.syncFieldInitialized === '1') return;
   source.dataset.syncFieldInitialized = '1';
   source.addEventListener('input', () => syncNamedFieldFrom(source));
   source.addEventListener('change', () => syncNamedFieldFrom(source));
   const form = source.form || source.closest('form');
   if (form) {
    form.addEventListener('submit', () => syncNamedFieldFrom(source));
   }
   syncNamedFieldFrom(source);
  });
 }

 function staffIdFieldName(input) {
  if (!input) return '';
  const explicit = (input.getAttribute('data-staff-id-name') || '').trim();
  if (explicit) return explicit;
  const name = (input.getAttribute('name') || '').trim();
  if (!name) return '';
  return `${name}_id`;
 }

 function ensureStaffIdField(input) {
  const name = staffIdFieldName(input);
  if (!name) return null;
  const form = input.form || input.closest('form');
  if (!form) return null;
  const selector = `input[type="hidden"][name="${cssEscape(name)}"]`;
  let field = form.querySelector(selector);
  if (!field) {
   field = document.createElement('input');
   field.type = 'hidden';
   field.name = name;
   form.appendChild(field);
  }
  return field;
 }

 function setStaffIdForInput(input, staffId) {
  if (!input) return;
  const field = ensureStaffIdField(input);
  if (!field) return;
  field.value = staffId ? String(staffId) : '';
 }

 async function fetchJson(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (window.AppFetch && typeof window.AppFetch.parseJsonResponse === "function") {
   return window.AppFetch.parseJsonResponse(res);
  }
  let data = null;
  try {
   data = await res.json();
  } catch (e) {
   data = null;
  }
  if (!res.ok) {
   const msg = (data && (data.message || data.error)) || `HTTP ${res.status}`;
   throw new Error(msg);
  }
  return data;
 }

 async function handleAutoOurRef(button) {
  const division =
   (button.getAttribute('data-division') || '').trim() ||
   (document.querySelector('input[name="division"]') || {}).value ||
   '';
  const type =
   (button.getAttribute('data-type') || '').trim() ||
   (document.querySelector('input[name="type"]') || {}).value ||
   '';

  let country = (button.getAttribute('data-country') || '').trim();
  if (!country) {
    const cid = button.getAttribute('data-country-id');
    if (cid) {
      const el = document.getElementById(cid);
      if (el) {
       syncNamedFieldFrom(el);
       country = (el.value || '').trim();
      }
    }
  }

  const typeUpper = type.toUpperCase();
  const countryOptionalTypes = new Set(['PCT', 'MADRID', 'HAGUE', 'COPYRIGHT', 'LITIGATION', 'MISC']);

  // Overseas validation - only regular OUT matters require country.
  if (!country && division.toUpperCase() === 'OUT' && !countryOptionalTypes.has(typeUpper)) {
     await ipmAlert('Enter the country code (Example: US, JP).', { title: "Confirm" });
     if (button.getAttribute('data-country-id')) {
       document.getElementById(button.getAttribute('data-country-id'))?.focus();
     }
     return;
  }
  // Default to US for domestic, empty for INC (optional)
  if (!country && division.toUpperCase() !== 'INC') country = 'US';

  const url =
   `/case/api/next_our_ref?division=${encodeURIComponent(division)}&type=${encodeURIComponent(type)}&country=${encodeURIComponent(country)}`;
  const data = await fetchJson(url);
  const nextRef = (data && data.our_ref) || '';
  if (!nextRef) throw new Error('Auto-numbering returned an empty result.');

  const input = findInputForButton(button, 'input[name="our_ref"]');
  if (!input) throw new Error('Our Ref input field not found.');
  if ((input.value || '').trim()) {
   const ok = await window.AppConfirm(`Our Ref. '${nextRef}' New`);
   if (!ok) return;
  }
  input.value = nextRef;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  input.focus();

  // Trigger split input update if applicable
  const yy = document.getElementById('ourRefYY');
  if (yy && input.id === 'ourRefInput') {
    input.dispatchEvent(new Event('input'));
  }
 }

 function handleSetToday(button) {
  const input = findInputForButton(button, DATE_INPUT_SELECTOR);
  if (!input) return;
  const value = todayIso();
  if (input._flatpickr && typeof input._flatpickr.setDate === 'function') {
   input._flatpickr.setDate(value, true, 'Y-m-d');
  } else {
   input.value = value;
   input.dispatchEvent(new Event('input', { bubbles: true }));
   input.dispatchEvent(new Event('change', { bubbles: true }));
  }
  input.focus();
 }

 function normalizeToken(value) {
  return String(value || '').trim().toLowerCase();
 }

 function isYesToken(value) {
  const t = normalizeToken(value);
  return t === 'y' || t === 'yes' || t === 'true' || t === '1' || t === 't';
 }

 function isNoToken(value) {
  const t = normalizeToken(value);
  return t === 'n' || t === 'no' || t === 'false' || t === '0' || t === 'f';
 }

 function normalizeDeadlineType(value) {
  const token = normalizeToken(value);
  if (!token) return '';
  if (
   token === 'internal' ||
   token === 'inner' ||
   token === 'inhouse' ||
   token === 'i' ||
   token === 'in' ||
   token === 'Internal' ||
   token === 'Internal deadline' ||
   token === ''
  ) {
   return 'INTERNAL';
  }
  if (
   token === 'legal' ||
   token === 'law' ||
   token === 'statutory' ||
   token === 'l' ||
   token === 'Statutory' ||
   token === 'Statutory deadline'
  ) {
   return 'LEGAL';
  }
  return '';
 }

 function buildDeadlineTypeSelect() {
  const select = document.createElement('select');
  select.name = 'filing_deadline_type';
  select.className = 'form-select form-select-sm';
  select.style.maxWidth = '150px';

  const options = [
   { value: '', label: 'Select type' },
   { value: 'INTERNAL', label: 'Internal deadline' },
   { value: 'LEGAL', label: 'Statutory deadline' },
  ];
  options.forEach((item) => {
   const opt = document.createElement('option');
   opt.value = item.value;
   opt.textContent = item.label;
   select.appendChild(opt);
  });
  return select;
 }

 function initFilingDeadlineTypeSelectors() {
  document.querySelectorAll('form').forEach((form) => {
   const prefillType = normalizeDeadlineType(form.getAttribute('data-filing-deadline-type-prefill'));
   form.querySelectorAll(FILING_DEADLINE_DATE_SELECTOR).forEach((input) => {
    let select = null;
    const group = input.closest('.input-group');

    if (group) {
     select = group.querySelector('select[name="filing_deadline_type"]');
    }

    if (!select) {
     const scope = input.closest('td, .col-md-3, .col-md-4, .col-md-6, .col-12, .row') || form;
     select = scope.querySelector('select[name="filing_deadline_type"]');
    }

    if (!select) {
     select = buildDeadlineTypeSelect();
     select.dataset.injectedDeadlineType = '1';
     if (group) {
      const todayBtn = group.querySelector('[data-set-today="1"]');
      if (todayBtn && todayBtn.parentElement === group) {
       group.insertBefore(select, todayBtn);
      } else {
       group.appendChild(select);
      }
     } else {
      select.classList.add('mt-1');
      input.insertAdjacentElement('afterend', select);
     }
    }

    const normalizedCurrent = normalizeDeadlineType(select.value);
    if (normalizedCurrent) {
     select.value = normalizedCurrent;
     return;
    }

    if (prefillType) {
     select.value = prefillType;
     return;
    }

    if (select.dataset.injectedDeadlineType === '1') {
     select.value = 'INTERNAL';
    }
   });
  });
 }

 function getNamedControlValue(form, name) {
  if (!form || !name) return '';
  const escaped = cssEscape(name);
  const checkedRadio = form.querySelector(`input[type="radio"][name="${escaped}"]:checked`);
  if (checkedRadio) {
   return checkedRadio.value || '';
  }
  const el = form.querySelector(`[name="${escaped}"]`);
  if (!el) return '';
  if (el instanceof HTMLInputElement && el.type === 'checkbox') {
   return el.checked ? '1' : '';
  }
  return el.value || '';
 }

 function emitInputChange(element) {
  if (!element) return;
  element.dispatchEvent(new Event('input', { bubbles: true }));
  element.dispatchEvent(new Event('change', { bubbles: true }));
 }

 function ensureCaseFormUxStyle() {
  if (document.getElementById('case-form-ux-style')) return;
  const style = document.createElement('style');
  style.id = 'case-form-ux-style';
  style.textContent = `
   .case-form-toolbar {
    border: 1px solid #dee2e6;
    border-radius: 10px;
    background: #f8f9fa;
    padding: 8px 10px;
   }
   .case-form-toolbar .toolbar-title {
    font-size: 0.85rem;
    color: #6c757d;
    white-space: nowrap;
   }
   .case-form-row-pending> th,
   .case-form-row-pending> td {
    box-shadow: inset 0 0 0 9999px rgba(255, 243, 205, 0.38);
   }
   .case-form-section-collapsed> .table-responsive {
    display: none !important;
   }
   .case-form-section-toggle.btn {
    padding-top: 0.1rem;
    padding-bottom: 0.1rem;
   }
  `;
  document.head.appendChild(style);
 }

 function renderClientResultItem(client, onPick) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'list-group-item list-group-item-action py-2';
  const email = client.email ? ` · ${client.email}` : '';
  const reg = client.registration_number ? ` · ${client.registration_number}` : '';
  btn.textContent = `${client.name || ''}${email}${reg}`;
  btn.addEventListener('mousedown', (e) => {
   e.preventDefault();
   onPick(client);
  });
  return btn;
 }

 function initClientSearch() {
  document.querySelectorAll('[data-client-search="1"]').forEach((input) => {
   const hiddenId = document.getElementById(input.getAttribute('data-client-id'));
   const menu = document.getElementById(input.getAttribute('data-client-menu'));
   if (!menu) return;

   let lastAbort = null;
   let hideTimer = null;
   let suppressInput = false;

   const clearBtn = (() => {
    const container = input.closest('.position-relative');
    let btn = container ? container.querySelector('[data-client-clear="1"]') : null;

    if (!btn) {
     const actionRow = container ? container.querySelector('.d-flex.gap-2.mt-1') : null;
     btn = document.createElement('button');
     btn.type = 'button';
     btn.className = 'btn btn-sm btn-outline-secondary';
     btn.textContent = '';
     btn.setAttribute('data-client-clear', '1');
     if (actionRow) {
      actionRow.insertBefore(btn, actionRow.firstChild);
     } else if (container) {
      const row = document.createElement('div');
      row.className = 'd-flex gap-2 mt-1';
      row.appendChild(btn);
      container.appendChild(row);
     } else {
      input.insertAdjacentElement('afterend', btn);
     }
    }
    return btn;
   })();

   function setLocked(locked) {
    input.readOnly = locked;
    if (clearBtn) {
     clearBtn.style.display = locked ? '' : 'none';
    }
   }

   function hideMenuSoon() {
    if (hideTimer) window.clearTimeout(hideTimer);
    hideTimer = window.setTimeout(() => {
     menu.style.display = 'none';
    }, 120);
   }

   function hideOtherMenus() {
    document.querySelectorAll('[data-client-search="1"]').forEach((other) => {
     if (!(other instanceof HTMLInputElement)) return;
     if (other === input) return;
     const otherMenuId = other.getAttribute('data-client-menu');
     const otherMenu = otherMenuId ? document.getElementById(otherMenuId) : null;
     if (otherMenu) otherMenu.style.display = 'none';
    });
   }

   function showMenu() {
    hideOtherMenus();
    menu.style.display = 'block';
   }

   function setPicked(client) {
    if (lastAbort) {
     try {
      lastAbort.abort();
     } catch (e) {}
     lastAbort = null;
    }
    if (debounce) window.clearTimeout(debounce);
    suppressInput = true;
    input.value = client.name || '';
    if (hiddenId) hiddenId.value = String(client.id || '');
    menu.style.display = 'none';
    setLocked(true);
    clearInvalid(input);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    suppressInput = false;
   }

   async function search(q) {
    if (lastAbort) lastAbort.abort();
    const controller = new AbortController();
    lastAbort = controller;

    const url = `/case/api/clients/search?q=${encodeURIComponent(q)}`;
    let res = null;
    try {
     res = await fetch(url, {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
     });
    } catch (e) {
     if (e && (e.name === 'AbortError' || e.code === 20)) return [];
     return [];
    }
    if (window.AppFetch && typeof window.AppFetch.parseJsonResponse === "function") {
     try {
      const data = await window.AppFetch.parseJsonResponse(res);
      return Array.isArray(data) ? data : [];
     } catch (e) {
      return [];
     }
    }
    if (!res.ok) return [];
    try {
     const data = await res.json();
     return Array.isArray(data) ? data : [];
    } catch (e) {
     return [];
    }
   }

   let debounce = null;
   input.addEventListener('input', () => {
    if (suppressInput) return;
    if (input.readOnly) {
     if (debounce) window.clearTimeout(debounce);
     if (lastAbort) {
      try {
       lastAbort.abort();
      } catch (e) {}
      lastAbort = null;
     }
     menu.innerHTML = '';
     menu.style.display = 'none';
     return;
    }
    const q = (input.value || '').trim();
    if (hiddenId) hiddenId.value = '';
    setLocked(false);
    clearInvalid(input);
    if (debounce) window.clearTimeout(debounce);
    if (q.length < 2) {
     menu.innerHTML = '';
     menu.style.display = 'none';
     return;
    }
    debounce = window.setTimeout(async () => {
     const items = await search(q);
     menu.innerHTML = '';
     if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'list-group-item text-muted small';
      empty.textContent = 'No search results.';
      menu.appendChild(empty);
      showMenu();
      return;
     }
     items.slice(0, 15).forEach((c) => menu.appendChild(renderClientResultItem(c, setPicked)));
     showMenu();
    }, 200);
   });

   if (clearBtn) {
    clearBtn.addEventListener('click', (e) => {
     e.preventDefault();
     input.value = '';
     if (hiddenId) hiddenId.value = '';
     setLocked(false);
     clearInvalid(input);
     menu.innerHTML = '';
     menu.style.display = 'none';
     input.focus();
    });
   }

   if (hiddenId && hiddenId.value && (input.value || '').trim()) {
    setLocked(true);
   } else {
    setLocked(false);
   }

   input.addEventListener('focus', () => {
    if (input.readOnly) {
     menu.style.display = 'none';
     return;
    }
    if (menu.childElementCount) showMenu();
   });
   input.addEventListener('blur', hideMenuSoon);
   menu.addEventListener('mouseenter', () => {
    if (hideTimer) window.clearTimeout(hideTimer);
   });
   menu.addEventListener('mouseleave', hideMenuSoon);
  });
 }

 function renderStaffResultItem(item, onPick) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'list-group-item list-group-item-action py-2';
  const title = document.createElement('div');
  title.textContent = item.label || item.value || '';
  btn.appendChild(title);
  const metaParts = [];
  if (item.email) metaParts.push(item.email);
  if (item.dept) metaParts.push(item.dept);
  if (metaParts.length) {
   const meta = document.createElement('div');
   meta.className = 'small text-muted';
   meta.textContent = metaParts.join(' · ');
   btn.appendChild(meta);
  }
  btn.addEventListener('mousedown', (e) => {
   e.preventDefault();
   onPick(item);
  });
  return btn;
 }

 function initStaffSearch() {
  document.querySelectorAll('[data-staff-search="1"]').forEach((input) => {
   const menuId = input.getAttribute('data-staff-menu');
   const menu = menuId ? document.getElementById(menuId) : null;
   if (!menu) return;

   let items = [];
   try {
    items = JSON.parse(input.getAttribute('data-staff-items') || '[]');
   } catch (e) {
    items = [];
   }
   if (!Array.isArray(items)) items = [];

   let hideTimer = null;
   let suppressInput = false;

   function normalize(v) {
    return (v || '').toString().toLowerCase();
   }

   function hideMenuSoon() {
    if (hideTimer) window.clearTimeout(hideTimer);
    hideTimer = window.setTimeout(() => {
     menu.style.display = 'none';
    }, 120);
   }

   function showMenu() {
    menu.style.display = 'block';
   }

   function parseStaffInput(raw) {
    const parts = String(raw || '').split(/[;,]+/);
    const trimmed = parts.map((part) => part.trim());
    const query = trimmed.length ? trimmed[trimmed.length - 1] : '';
    const tokens = trimmed.slice(0, -1).filter(Boolean);
    return { tokens, query };
   }

   function appendValue(value) {
    const rawItem = value && typeof value === 'object' ? value : null;
    const rawValue = rawItem ? rawItem.value || rawItem.label || '' : value;
    const v = (rawValue || '').toString().trim();
    if (!v) return;
    const parsed = parseStaffInput(input.value);
    const existing = parsed.tokens.slice();
    const vLower = v.toLowerCase();
    const hasValue = existing.some((item) => item.toLowerCase() === vLower);
    if (!hasValue) {
     existing.push(v);
    }
    suppressInput = true;
    input.value = existing.join('; ');
    menu.style.display = 'none';
    if (existing.length === 1 && rawItem && rawItem.id) {
     setStaffIdForInput(input, rawItem.id);
    } else {
     setStaffIdForInput(input, '');
    }
    clearInvalid(input);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
   }

   input.addEventListener('input', () => {
    if (suppressInput) {
     suppressInput = false;
     return;
    }
    const parsed = parseStaffInput(input.value);
    const q = parsed.query || '';
    setStaffIdForInput(input, '');
    clearInvalid(input);
    if (q.length < 1) {
     menu.innerHTML = '';
     menu.style.display = 'none';
     return;
    }
    const nq = normalize(q);
    const results = items.filter((item) => {
     return (
      normalize(item.label).includes(nq) ||
      normalize(item.value).includes(nq) ||
      normalize(item.email).includes(nq) ||
      normalize(item.dept).includes(nq)
     );
    });
    menu.innerHTML = '';
    if (!results.length) {
     const empty = document.createElement('div');
     empty.className = 'list-group-item text-muted small';
     empty.textContent = 'No search results.';
     menu.appendChild(empty);
     showMenu();
     return;
    }
    results.slice(0, 15).forEach((item) => {
     menu.appendChild(renderStaffResultItem(item, (picked) => appendValue(picked)));
    });
    showMenu();
   });

   input.addEventListener('focus', () => {
    if (menu.childElementCount) showMenu();
   });
   input.addEventListener('blur', hideMenuSoon);
   menu.addEventListener('mouseenter', () => {
    if (hideTimer) window.clearTimeout(hideTimer);
   });
   menu.addEventListener('mouseleave', hideMenuSoon);
  });
 }

 if (!window.AppStaff) {
  window.AppStaff = {};
 }
 window.AppStaff.setStaffIdForInput = setStaffIdForInput;

 function getInputHost(element) {
  if (!element) return null;
  const group = element.closest('.input-group');
  if (group && group.parentElement) return group.parentElement;
  return element.parentElement;
 }

 function ensureFeedback(host, key, message) {
  if (!host || !key) return;
  const selector = `[data-invalid-for="${cssEscape(key)}"]`;
  if (host.querySelector(selector)) return;
  const div = document.createElement('div');
  div.className = 'invalid-feedback d-block';
  div.setAttribute('data-invalid-for', key);
  div.textContent = message;
  host.appendChild(div);
 }

 function clearInvalid(input) {
  if (!input) return;
  input.classList.remove('is-invalid');
  input.removeAttribute('aria-invalid');
  const key = input.name;
  if (!key) return;
  const host = getInputHost(input);
  if (!host) return;
  const msg = host.querySelector(`[data-invalid-for="${cssEscape(key)}"]`);
  if (msg) msg.remove();
 }

 function normalizedMetaOptions(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  const seen = new Set();
  raw.forEach((item) => {
   let value = '';
   let label = '';
   if (item && typeof item === 'object' && !Array.isArray(item)) {
    value = String(item.value || '').trim();
    label = String(item.label || value).trim();
   } else if (Array.isArray(item) && item.length) {
    value = String(item[0] || '').trim();
    label = String(item.length > 1 ? item[1] || '' : item[0] || '').trim();
   } else if (item != null) {
    value = String(item).trim();
    label = value;
   }
   if (!value || seen.has(value)) return;
   out.push({ value, label: label || value });
   seen.add(value);
  });
  return out;
 }

 function copyControlAttributes(source, target) {
  Array.from(source.attributes || []).forEach((attr) => {
   if (attr.name === 'type' || attr.name === 'value' || attr.name === 'class') return;
   target.setAttribute(attr.name, attr.value);
  });
  Array.from(source.classList || []).forEach((className) => {
   if (['form-control', 'form-control-sm', 'form-select', 'form-select-sm'].includes(className)) return;
   target.classList.add(className);
  });
  target.classList.add('form-select');
  if (source.classList.contains('form-control-sm')) {
   target.classList.add('form-select-sm');
  }
 }

 function buildMetaSelect(source, info, options) {
  const select = document.createElement('select');
  copyControlAttributes(source, select);
  select.classList.add('form-select');
  if (source.classList.contains('form-control-sm')) select.classList.add('form-select-sm');
  const current = String(source.value || '').trim();
  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = 'Select';
  select.appendChild(empty);
  let hasCurrent = !current;
  options.forEach((item) => {
   const option = document.createElement('option');
   option.value = item.value;
   option.textContent = item.label;
   if (current === item.value) {
    option.selected = true;
    hasCurrent = true;
   }
   select.appendChild(option);
  });
  if (current && !hasCurrent) {
   const option = document.createElement('option');
   option.value = current;
   option.textContent = `${current} (current)`;
   option.selected = true;
   select.appendChild(option);
  }
  if (info && info.required) select.required = true;
  return select;
 }

 function copyTextControlAttributes(source, target) {
  Array.from(source.attributes || []).forEach((attr) => {
   if (attr.name === 'type' || attr.name === 'value' || attr.name === 'class') return;
   target.setAttribute(attr.name, attr.value);
  });
  Array.from(source.classList || []).forEach((className) => {
   if (['form-control', 'form-control-sm', 'form-select', 'form-select-sm'].includes(className)) return;
   target.classList.add(className);
  });
 }

 function buildMetaTextarea(source, info) {
  const textarea = document.createElement('textarea');
  copyTextControlAttributes(source, textarea);
  textarea.classList.add('form-control');
  if (source.classList.contains('form-control-sm')) textarea.classList.add('form-control-sm');
  textarea.rows = Number(info && info.rows) || 2;
  textarea.value = source.value || '';
  if (info && info.required) textarea.required = true;
  return textarea;
 }

 function buildMetaInput(source, info, type) {
  const input = document.createElement('input');
  copyTextControlAttributes(source, input);
  input.type = type || 'text';
  input.classList.add('form-control');
  if (source.classList.contains('form-control-sm') || source.classList.contains('form-select-sm')) {
   input.classList.add('form-control-sm');
  }
  input.value = source.value || '';
  if (info && info.required) input.required = true;
  return input;
 }

 function normalizeBoolToken(value) {
  const token = String(value || '').trim().toLowerCase();
  if (['y', 'yes', 'true', '1', 'on', 't'].includes(token)) return 'yes';
  if (['n', 'no', 'false', '0', 'off', 'f'].includes(token)) return 'no';
  return '';
 }

 function buildMetaYesNo(source, info) {
  const current = normalizeBoolToken(source.value || '');
  const name = (source.getAttribute('name') || '').trim();
  const sourceId = (source.getAttribute('id') || name || 'field').replace(/[^A-Za-z0-9_-]/g, '-');
  const wrap = document.createElement('div');
  wrap.className = 'd-flex flex-wrap gap-3';
  wrap.dataset.parameterizedControlUpgraded = '1';
  [
   { suffix: 'empty', value: '', label: 'Select', checked: !current },
   { suffix: 'yes', value: 'Yes', label: 'Yes', checked: current === 'yes' },
   { suffix: 'no', value: 'No', label: 'No', checked: current === 'no' },
  ].forEach((item) => {
   const holder = document.createElement('div');
   holder.className = 'form-check form-check-inline mb-0';
   const input = document.createElement('input');
   input.className = 'form-check-input';
   input.type = 'radio';
   input.name = name;
   input.id = `${sourceId}-${item.suffix}`;
   input.value = item.value;
   input.checked = item.checked;
   if (info && info.required && item.value) input.required = true;
   const label = document.createElement('label');
   label.className = 'form-check-label';
   label.setAttribute('for', input.id);
   label.textContent = item.label;
   holder.appendChild(input);
   holder.appendChild(label);
   wrap.appendChild(holder);
  });
  return wrap;
 }

 function ensureMetaDateControl(input, info) {
  if (!(input instanceof HTMLInputElement)) return;
  if (input.type !== 'date') input.type = 'date';
  input.setAttribute('data-ipm-date-input', '1');
  if (info && info.required) input.required = true;

  let group = input.closest('.input-group');
  if (!group) {
   group = document.createElement('div');
   group.className = 'input-group input-group-sm';
   input.parentElement?.insertBefore(group, input);
   group.appendChild(input);
  }

  if (!group.querySelector('[data-set-today="1"]')) {
   const button = document.createElement('button');
   button.type = 'button';
   button.className = 'btn btn-outline-secondary';
   button.setAttribute('data-set-today', '1');
   button.setAttribute('title', 'Set to today');
   button.textContent = 'Today';
   group.appendChild(button);
  }

  if (window.AppDate && typeof window.AppDate.initDateInputs === 'function') {
   window.AppDate.initDateInputs(group || input.parentElement || document);
  }
 }

 function ensureMetaNumberControl(input, info) {
  if (!(input instanceof HTMLInputElement)) return;
  if (input.type !== 'number') input.type = 'number';
  if (info && info.required) input.required = true;
  input.setAttribute('inputmode', 'numeric');
 }

 function ensureMetaClientSearchControl(input, info, key) {
  if (!(input instanceof HTMLInputElement)) return;
  const name = input.getAttribute('name') || key || '';
  if (!input.id) {
   input.id = `client-search-${name.replace(/[^A-Za-z0-9_-]/g, '-')}`;
  }
  const hiddenName = name === 'client_name' ? 'client_id' : `${name}_id`;
  let hidden = null;
  const form = input.form || input.closest('form') || document;
  if (hiddenName) {
   hidden = form.querySelector(`input[type="hidden"][name="${cssEscape(hiddenName)}"]`);
  }
  if (!hidden) {
   hidden = document.createElement('input');
   hidden.type = 'hidden';
   hidden.name = hiddenName;
   input.insertAdjacentElement('afterend', hidden);
  }
  if (!hidden.id) hidden.id = `${input.id}-id`;

  let menuId = input.getAttribute('data-client-menu');
  let menu = menuId ? document.getElementById(menuId) : null;
  if (!menu) {
   menuId = `${input.id}-menu`;
   menu = document.createElement('div');
   menu.id = menuId;
   menu.className = 'list-group position-absolute w-100 shadow';
   menu.style.zIndex = '2000';
   menu.style.display = 'none';
   menu.style.maxHeight = '240px';
   menu.style.overflow = 'auto';
   input.insertAdjacentElement('afterend', menu);
  }

  input.setAttribute('autocomplete', 'off');
  input.setAttribute('data-client-search', '1');
  input.setAttribute('data-client-id', hidden.id);
  input.setAttribute('data-client-menu', menu.id);
  if (info && info.required) input.required = true;

  const container = input.closest('.position-relative');
  if (container && !container.querySelector('[data-client-create="1"]')) {
   const row = document.createElement('div');
   row.className = 'd-flex gap-2 mt-1';
   const link = document.createElement('a');
   link.className = 'btn btn-sm btn-outline-secondary';
   link.href = '#';
   link.setAttribute('data-client-create', '1');
   link.textContent = 'Create contact';
   const hint = document.createElement('span');
   hint.className = 'text-muted small';
   hint.textContent = 'Search by name, email, or registration number';
   row.appendChild(link);
   row.appendChild(hint);
   container.appendChild(row);
  }
 }

 function effectiveMetaInputType(key, info) {
  const inputType = String((info && info.input_type) || '').trim();
  const serializer = String((info && info.serializer) || '').trim();
  const fieldKey = String(key || '').trim();
  if (
   (!inputType || inputType === 'text') &&
   (serializer === 'date' || /(?:_date|_deadline)$/.test(fieldKey))
  ) {
   return 'date';
  }
  return inputType || 'text';
 }

 function upgradeParameterizedFieldControls() {
  const meta = window.CASE_FORM_FIELD_META;
  if (!meta || typeof meta !== 'object') return;

  Object.keys(meta).forEach((key) => {
   const info = meta[key] || {};
   const inputType = effectiveMetaInputType(key, info);
   const options = normalizedMetaOptions(info.options);
   const selector = `[name="${cssEscape(key)}"]`;
   const controls = Array.from(document.querySelectorAll(selector)).filter((control) => {
    if (!(control instanceof HTMLElement)) return false;
    if (control.dataset.parameterizedControlUpgraded === '1') return false;
    if (control instanceof HTMLInputElement) {
     const type = (control.type || '').toLowerCase();
     return !['hidden', 'file', 'radio', 'checkbox', 'button', 'submit'].includes(type);
    }
    return control instanceof HTMLSelectElement || control instanceof HTMLTextAreaElement;
   });

   controls.forEach((control) => {
    if (inputType === 'select' && options.length) {
     if (control instanceof HTMLSelectElement) {
     if (!control.children.length || control.dataset.dynamicOptions === '1') {
       const current = control.value || '';
       const replacement = buildMetaSelect(control, info, options);
       control.innerHTML = replacement.innerHTML;
       control.value = current;
      }
      control.dataset.parameterizedControlUpgraded = '1';
      return;
     }
     const replacement = buildMetaSelect(control, info, options);
     replacement.dataset.parameterizedControlUpgraded = '1';
     control.replaceWith(replacement);
     return;
    }
    if (inputType === 'select_yn') {
     if (control instanceof HTMLInputElement || control instanceof HTMLSelectElement || control instanceof HTMLTextAreaElement) {
      const replacement = buildMetaYesNo(control, info);
      control.replaceWith(replacement);
     } else {
      control.dataset.parameterizedControlUpgraded = '1';
     }
     return;
    }
    if (inputType === 'date') {
     if (control instanceof HTMLInputElement) {
      ensureMetaDateControl(control, info);
      control.dataset.parameterizedControlUpgraded = '1';
     } else if (control instanceof HTMLSelectElement || control instanceof HTMLTextAreaElement) {
      const replacement = buildMetaInput(control, info, 'date');
      replacement.dataset.parameterizedControlUpgraded = '1';
      control.replaceWith(replacement);
      ensureMetaDateControl(replacement, info);
     }
     return;
    }
    if (inputType === 'number') {
     if (control instanceof HTMLInputElement) {
      ensureMetaNumberControl(control, info);
      control.dataset.parameterizedControlUpgraded = '1';
     } else if (control instanceof HTMLSelectElement || control instanceof HTMLTextAreaElement) {
      const replacement = buildMetaInput(control, info, 'number');
      replacement.dataset.parameterizedControlUpgraded = '1';
      control.replaceWith(replacement);
      ensureMetaNumberControl(replacement, info);
     }
     return;
    }
    if (inputType === 'textarea' && !(control instanceof HTMLTextAreaElement)) {
     const replacement = buildMetaTextarea(control, info);
     replacement.dataset.parameterizedControlUpgraded = '1';
     control.replaceWith(replacement);
     return;
    }
    if (inputType === 'client_search') {
     if (control instanceof HTMLInputElement) {
      ensureMetaClientSearchControl(control, info, key);
      control.dataset.parameterizedControlUpgraded = '1';
     } else if (control instanceof HTMLSelectElement || control instanceof HTMLTextAreaElement) {
      const replacement = buildMetaInput(control, info, 'text');
      replacement.dataset.parameterizedControlUpgraded = '1';
      control.replaceWith(replacement);
      ensureMetaClientSearchControl(replacement, info, key);
     }
     return;
    }
    if (inputType === 'textarea' && control instanceof HTMLTextAreaElement && info.required) {
     control.required = true;
    }
    control.dataset.parameterizedControlUpgraded = '1';
   });
  });
 }

 function applyMissingFieldErrors() {
  const missing = window.CASE_FORM_MISSING_FIELDS;
  if (!Array.isArray(missing) || !missing.length) return;

  let firstEl = null;
  missing.forEach((item) => {
   const key = item && (item.key || item.field || item.name);
   if (!key) return;
   const label = (item && item.label) || key;
   const selector = `[name="${cssEscape(key)}"]`;
   const nodes = Array.from(document.querySelectorAll(selector)).filter((node) => {
    return !(node instanceof HTMLInputElement && node.type === 'hidden');
   });
   if (!nodes.length) return;

   nodes.forEach((node) => {
    node.classList.add('is-invalid');
    node.setAttribute('aria-invalid', 'true');
   });

   if (!firstEl) firstEl = nodes[0];

   let host = null;
   if (nodes.length> 1) {
    const flex = nodes[0].closest('.d-flex');
    host = (flex && flex.parentElement) || nodes[0].closest('.form-check')?.parentElement || getInputHost(nodes[0]);
   } else {
    host = getInputHost(nodes[0]);
   }

   ensureFeedback(host, key, `${label} item required.`);
  });

  if (firstEl) {
   firstEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
   try {
    firstEl.focus({ preventScroll: true });
   } catch (e) {
    firstEl.focus();
   }
  }
 }

 function applyFieldHelpText() {
  const meta = window.CASE_FORM_FIELD_META;
  if (!meta || typeof meta !== 'object') return;
  Object.keys(meta).forEach((key) => {
   const info = meta[key] || {};
   const helpText = (info.help_text || '').trim();
   const selector = `[name="${cssEscape(key)}"]`;
   const element = document.querySelector(selector);
   if (!element || (element instanceof HTMLInputElement && element.type === 'hidden')) return;
   if (info.options_source) {
    element.setAttribute('data-options-source', String(info.options_source));
   }
   if (!helpText) return;
   const host = getInputHost(element);
   if (!host || host.querySelector(`[data-help-for="${cssEscape(key)}"]`)) return;
   const div = document.createElement('div');
   div.className = 'form-text';
   div.setAttribute('data-help-for', key);
   div.textContent = helpText;
   host.appendChild(div);
  });
 }

 function initClientIdGuard() {
  const forms = Array.from(document.querySelectorAll('form[data-client-id-guard="1"]'));
  if (!forms.length) return;
  forms.forEach((form) => {
   form.addEventListener('submit', (e) => {
    const inputs = Array.from(form.querySelectorAll('[data-client-search="1"]'));
    let firstInvalid = null;

    inputs.forEach((input) => {
     // Only enforce CRM-selection for required client-search fields.
     // Optional searchable fields (e.g. litigation claimant/respondent) may be free text.
     const requiresClientPick =
      input.hasAttribute('required') || input.getAttribute('data-require-client-id') === '1';
     if (!requiresClientPick) return;

     const hiddenId = document.getElementById(input.getAttribute('data-client-id'));
     const hiddenName = hiddenId ? (hiddenId.getAttribute('name') || '') : '';
     if (!hiddenName) return;
     const value = (input.value || '').trim();
     const idValue = hiddenId ? (hiddenId.value || '').trim() : '';

     if (!value || idValue) return;

     input.classList.add('is-invalid');
     input.setAttribute('aria-invalid', 'true');
     const host = getInputHost(input);
     const label =
      (window.CASE_FORM_FIELD_META && window.CASE_FORM_FIELD_META[input.name] && window.CASE_FORM_FIELD_META[input.name].label) ||
      input.name ||
      'Client';
     ensureFeedback(host, input.name, `Select ${label} from the list.`);
     if (!firstInvalid) firstInvalid = input;
    });

    if (firstInvalid) {
     e.preventDefault();
     firstInvalid.scrollIntoView({ behavior: 'smooth', block: 'center' });
     firstInvalid.focus();
     ipmAlert('search listfrom Client select.', { title: "Confirm" });
    }
   });
  });
 }

 document.addEventListener('click', async (e) => {
  const t = e.target;
  if (!(t instanceof Element)) return;
  const auto = t.closest('[data-auto-our-ref="1"]');
  if (auto) {
   e.preventDefault();
   try {
    await handleAutoOurRef(auto);
   } catch (err) {
    await ipmAlert(`Auto-numbering failed: ${err && err.message ? err.message : String(err)}`, { title: "Error" });
   }
   return;
  }
  const today = t.closest('[data-set-today="1"]');
  if (today) {
   e.preventDefault();
   handleSetToday(today);
  }
 });

 function bootCaseForm() {
  function initOurRefSegments() {
   const yy = document.getElementById('ourRefYY');
   const num = document.getElementById('ourRefNum');
   const full = document.getElementById('ourRefInput');
   if (!yy || !num || !full) return;

   // Infer structure: YY [MID] Num [SUFFIX]
   const midSpan = yy.nextElementSibling;
   const sufSpan = num.nextElementSibling;
   const midCode = (midSpan && midSpan.classList.contains('input-group-text')) ? midSpan.textContent.trim() : 'PD';
   const suffix = (sufSpan && sufSpan.classList.contains('input-group-text')) ? sufSpan.textContent.trim() : 'US';

   function digitsOnly(v) {
    return String(v || '').replace(/[^0-9]/g, '');
   }

   function parseFull(v) {
    // Regex: (2 digits) + midCode + (4 digits) + suffix
    // Escape special chars in midCode/suffix just in case
    const escapeRegExp = (string) => string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = new RegExp(`^(\\d{2})${escapeRegExp(midCode)}(\\d{4})${escapeRegExp(suffix)}$`, 'i');

    const m = String(v || '').trim().match(pattern);
    if (!m) return null;
    return { yy: m[1], num: m[2] };
   }

   function setSegmentsFromFull() {
    const p = parseFull(full.value);
    if (!p) return;
    yy.value = p.yy;
    num.value = p.num;
   }

   function setFullFromSegments() {
    const yyv = digitsOnly(yy.value).slice(0, 2);
    const numv = digitsOnly(num.value).slice(0, 4);

    yy.value = yyv;
    num.value = numv;

    if (yyv.length !== 2 || !numv) return;
    const padded = numv.padStart(4, '0');
    full.value = `${yyv}${midCode}${padded}${suffix}`;
    full.dispatchEvent(new Event('input', { bubbles: true }));
    full.dispatchEvent(new Event('change', { bubbles: true }));
   }

   yy.addEventListener('input', setFullFromSegments);
   num.addEventListener('input', setFullFromSegments);
   full.addEventListener('input', () => {
    const p = parseFull(full.value);
    if (!p) {
     yy.value = '';
     num.value = '';
     return;
    }
    yy.value = p.yy;
    num.value = p.num;
   });

   setSegmentsFromFull();
  }

  function initImageDropzones() {
   document.querySelectorAll('[data-image-dropzone="1"]').forEach((zone) => {
    const fileInput = zone.querySelector('[data-image-file="1"]');
    const preview = zone.querySelector('[data-image-preview="1"]');
    const hiddenInput = zone.querySelector('[data-image-value="1"]');
    const selectBtn = zone.querySelector('[data-image-select="1"]');
    const clearBtn = zone.querySelector('[data-image-clear="1"]');
    if (!fileInput || !preview) return;

    let currentUrl = null;

    function clearObjectUrl() {
     if (currentUrl) {
      URL.revokeObjectURL(currentUrl);
      currentUrl = null;
     }
    }

    function renderPlaceholder(message) {
     preview.innerHTML = '';
     if (!message) return;
     const div = document.createElement('div');
     div.className = 'text-muted small';
     div.textContent = message;
     preview.appendChild(div);
    }

    function renderFile(file) {
     clearObjectUrl();
     preview.innerHTML = '';
     if (!file) {
      renderPlaceholder('Drag and drop here or click');
      return;
     }
     const isImage = (file.type || '').toLowerCase().startsWith('image/');
     if (!isImage) {
      fileInput.value = '';
      renderPlaceholder('Only image files can be uploaded.');
      return;
     }
     currentUrl = URL.createObjectURL(file);
     const img = document.createElement('img');
     img.src = currentUrl;
     img.alt = '/Image';
     img.className = 'img-fluid rounded border';
     img.style.maxHeight = '180px';
     preview.appendChild(img);

     const name = document.createElement('div');
     name.className = 'text-muted small mt-1';
     name.textContent = file.name || '';
     preview.appendChild(name);
    }

    function setInputFiles(files) {
     try {
      const dt = new DataTransfer();
      Array.from(files).forEach((f) => dt.items.add(f));
      fileInput.files = dt.files;
      return true;
     } catch (e) {
      try {
       fileInput.files = files;
       return true;
      } catch (err) {
       return false;
      }
     }
    }

    function pickFiles(files) {
     if (!files || !files.length) return;
     setInputFiles(files);
     if (hiddenInput) hiddenInput.value = '';
     renderFile(files[0]);
    }

    if (selectBtn) {
     selectBtn.addEventListener('click', (e) => {
      e.preventDefault();
      fileInput.click();
     });
    }

    if (clearBtn) {
     clearBtn.addEventListener('click', (e) => {
      e.preventDefault();
      fileInput.value = '';
      if (hiddenInput) hiddenInput.value = '';
      renderPlaceholder('Drag and drop here or click');
     });
    }

    zone.addEventListener('click', (e) => {
     if (e.target.closest('button') || e.target === fileInput) return;
     fileInput.click();
    });

    fileInput.addEventListener('change', () => {
     const files = fileInput.files;
     if (!files || !files.length) return;
     if (hiddenInput) hiddenInput.value = '';
     renderFile(files[0]);
    });

    zone.addEventListener('dragover', (e) => {
     e.preventDefault();
     zone.classList.add('border-primary', 'bg-light');
    });
    zone.addEventListener('dragleave', () => {
     zone.classList.remove('border-primary', 'bg-light');
    });
    zone.addEventListener('drop', (e) => {
     e.preventDefault();
     zone.classList.remove('border-primary', 'bg-light');
     const files = e.dataTransfer ? e.dataTransfer.files : null;
     pickFiles(files);
    });
   });
  }

  function initSameClientSync() {
   const clientInput = document.querySelector('input[name="client_name"]');
   document.querySelectorAll('[data-same-client="1"]').forEach((checkbox) => {
    if (!(checkbox instanceof HTMLInputElement)) return;
    const form = checkbox.form || document.querySelector('form');
    let hidden = form ? form.querySelector('input[name="applicant_same_as_client"]') : null;
    if (form && !hidden) {
     hidden = document.createElement('input');
     hidden.type = 'hidden';
     hidden.name = 'applicant_same_as_client';
     form.appendChild(hidden);
    }
    const targetId = checkbox.getAttribute('data-target-id');
    const target =
     (targetId && document.getElementById(targetId)) ||
     checkbox.closest('td')?.querySelector('input[name="applicant_name"]');
    if (!target) return;

    function syncFromClient() {
     if (!clientInput) return;
     target.value = clientInput.value || '';
     target.dispatchEvent(new Event('input', { bubbles: true }));
     target.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function setReadOnlyState() {
     target.readOnly = checkbox.checked;
    }

    function syncHidden() {
     if (hidden) hidden.value = checkbox.checked ? '1' : '0';
    }

    checkbox.addEventListener('change', () => {
     setReadOnlyState();
     syncHidden();
     if (checkbox.checked) {
      syncFromClient();
     }
    });

    if (clientInput) {
     const onClientChange = () => {
      if (checkbox.checked) syncFromClient();
     };
     clientInput.addEventListener('input', onClientChange);
     clientInput.addEventListener('change', onClientChange);
    }

    setReadOnlyState();
    syncHidden();
    if (checkbox.checked) {
     syncFromClient();
    }
   });
  }

  function initFamilyCheck() {
   const ourRefInput = document.getElementById('ourRefInput');
   if (!ourRefInput) return;
   const titleInput =
    ourRefInput.form &&
    ourRefInput.form.querySelector &&
    ourRefInput.form.querySelector('input[name="right_name"], textarea[name="right_name"]');
   const divisionInput =
    ourRefInput.form &&
    ourRefInput.form.querySelector &&
    ourRefInput.form.querySelector('input[name="division"], input[name="in_out_type"]');
   const typeInput =
    ourRefInput.form &&
    ourRefInput.form.querySelector &&
    ourRefInput.form.querySelector('input[name="case_type"], input[name="type"], input[name="category"]');

   // Create a hidden input for family linking if not exists
   let familyInput = document.getElementById('familyLinkTargetId');
   if (!familyInput) {
    familyInput = document.createElement('input');
    familyInput.type = 'hidden';
    familyInput.id = 'familyLinkTargetId';
    familyInput.name = 'family_link_target_id';
    // Append to form
    ourRefInput.form.appendChild(familyInput);
   }

   ourRefInput.addEventListener('blur', async () => {
    const val = (ourRefInput.value || '').trim();
    if (!val || val.length < 8) return; // minimal length check

    // Don't check if we already have a link set (unless user wants to resetNew simpler for now)
    if (familyInput.value) return;

    try {
      let url = `/case/api/check_family_candidate?our_ref=${encodeURIComponent(val)}`;
      const division = (divisionInput && divisionInput.value ? String(divisionInput.value) : '').trim();
      const caseType = (typeInput && typeInput.value ? String(typeInput.value) : '').trim();
      if (division) {
        url += `&division=${encodeURIComponent(division)}`;
      }
      if (caseType) {
        url += `&type=${encodeURIComponent(caseType)}`;
      }
      const titleHint = (titleInput && titleInput.value ? String(titleInput.value) : '').trim();
      if (titleHint) {
        url += `&title=${encodeURIComponent(titleHint)}`;
      }
      const data = await fetchJson(url);

      if (data && data.matter_id && data.our_ref) {
        // Check if the suffix is actually different. The API checks base, but let's be sure we aren't linking to self (if editing)
        // But API takes ignore_id. Here we assume creating new.
        if (val === data.our_ref) {
          // Same refNew Might be error or duplicate. Let server handle unique constraint.
          return;
        }

        const msg = `Input Ref(${val}) Existing Matter found.\n\n[${data.our_ref}] ${data.title}\n\n Matter Matter(Family)to Link ? `;
        const ok = await window.AppConfirm(msg);
        if (ok) {
          familyInput.value = data.matter_id;
        }
      }
    } catch (e) {
      console.error(e);
    }
   });
  }

  function initExamRequestUx() {
   document.querySelectorAll('form').forEach((form) => {
    const examDateInput = form.querySelector('input[name="exam_request_date"]');
    if (!(examDateInput instanceof HTMLInputElement)) return;
    const appDateInput = form.querySelector('input[name="application_date"]');
    const examRequestedControls = Array.from(form.querySelectorAll('[name="exam_requested"]'));

    const host = getInputHost(examDateInput);
    let hint = host ? host.querySelector('[data-exam-request-hint="1"]') : null;
    if (!hint && host) {
     hint = document.createElement('div');
     hint.className = 'form-text';
     hint.setAttribute('data-exam-request-hint', '1');
     host.appendChild(hint);
    }

    const group = examDateInput.closest('.input-group');
    let copyBtn = group ? group.querySelector('[data-copy-application-date="1"]') : null;
    if (!copyBtn && group && appDateInput instanceof HTMLInputElement) {
     copyBtn = document.createElement('button');
     copyBtn.type = 'button';
     copyBtn.className = 'btn btn-outline-secondary';
     copyBtn.textContent = 'Copy filing date';
     copyBtn.title = 'Copy filing date to examination request date';
     copyBtn.setAttribute('data-copy-application-date', '1');
     group.appendChild(copyBtn);
    }

    let suppressExamInput = false;

    function setExamDateAuto(nextValue, sourceValue) {
     const value = (nextValue || '').trim();
     if (!value) return false;
     const current = (examDateInput.value || '').trim();
     if (current === value) {
      examDateInput.setAttribute('data-exam-auto', '1');
      examDateInput.setAttribute('data-exam-auto-source', sourceValue || value);
      return false;
     }
     suppressExamInput = true;
     examDateInput.value = value;
     examDateInput.setAttribute('data-exam-auto', '1');
     examDateInput.setAttribute('data-exam-auto-source', sourceValue || value);
     emitInputChange(examDateInput);
     suppressExamInput = false;
     return true;
    }

    function markExamDateManual() {
     if (suppressExamInput) return;
     examDateInput.setAttribute('data-exam-auto', '0');
     examDateInput.setAttribute('data-exam-auto-source', '');
    }

    function renderHint(hasExamRequested, yesSelected, noSelected, appDate, examDate) {
     if (!hint) return;
     let msg = '';
     hint.classList.remove('text-warning', 'text-muted');
     hint.classList.add('text-muted');

     if (!hasExamRequested) {
      msg = '';
     } else if (yesSelected) {
      if (!appDate) {
       msg = 'The examination request date will default to the filing date.';
      } else if (!examDate) {
       msg = 'Examination request is set to Yes; the default examination request date is the filing date.';
      } else if (examDate === appDate) {
       msg = 'The examination request date matches the filing date.';
      } else {
       msg = 'The examination request date has been edited.';
      }
     } else if (noSelected) {
      if (examDate) {
       msg = 'An examination request date is entered while examination request is No. Review if needed.';
       hint.classList.remove('text-muted');
       hint.classList.add('text-warning');
      } else {
       msg = 'Examination request No Examination request date Input is locked.';
      }
     } else {
      msg = 'Examination request Yes Select Examination request date Filing date Default Input.';
     }

     hint.textContent = msg;
    }

    function refreshExamRequestState() {
     const hasExamRequested = examRequestedControls.length> 0;
     const requestedValue = getNamedControlValue(form, 'exam_requested');
     const yesSelected = hasExamRequested && isYesToken(requestedValue);
     const noSelected = hasExamRequested && isNoToken(requestedValue);
     const appDate = appDateInput instanceof HTMLInputElement ? (appDateInput.value || '').trim() : '';
     const examDate = (examDateInput.value || '').trim();
     const autoSource = (examDateInput.getAttribute('data-exam-auto-source') || '').trim();
     const isAuto = examDateInput.getAttribute('data-exam-auto') === '1';

     if (yesSelected && appDate) {
      if (!examDate) {
       setExamDateAuto(appDate, appDate);
      } else if (isAuto && autoSource && examDate === autoSource && autoSource !== appDate) {
       setExamDateAuto(appDate, appDate);
      } else if (examDate === appDate) {
       examDateInput.setAttribute('data-exam-auto', '1');
       examDateInput.setAttribute('data-exam-auto-source', appDate);
      }
     }

     if (hasExamRequested) {
      const lockExamDate = noSelected && !examDate;
      examDateInput.readOnly = lockExamDate;
      examDateInput.classList.toggle('bg-light', lockExamDate);
     }

     const latestExamDate = (examDateInput.value || '').trim();
     if (copyBtn instanceof HTMLButtonElement) {
      copyBtn.disabled = !appDate;
      copyBtn.classList.toggle('disabled', !appDate);
     }
     renderHint(hasExamRequested, yesSelected, noSelected, appDate, latestExamDate);
    }

    if (appDateInput instanceof HTMLInputElement) {
     appDateInput.addEventListener('input', refreshExamRequestState);
     appDateInput.addEventListener('change', refreshExamRequestState);
    }
    examRequestedControls.forEach((control) => {
     control.addEventListener('change', refreshExamRequestState);
     control.addEventListener('input', refreshExamRequestState);
    });
    examDateInput.addEventListener('input', () => {
     markExamDateManual();
     refreshExamRequestState();
    });
    examDateInput.addEventListener('change', () => {
     markExamDateManual();
     refreshExamRequestState();
    });
    if (copyBtn instanceof HTMLButtonElement) {
     copyBtn.addEventListener('click', (e) => {
      e.preventDefault();
      const appDate = appDateInput instanceof HTMLInputElement ? (appDateInput.value || '').trim() : '';
      if (!appDate) return;
      setExamDateAuto(appDate, appDate);
      refreshExamRequestState();
     });
    }

    const requestedValue = getNamedControlValue(form, 'exam_requested');
    const appDate = appDateInput instanceof HTMLInputElement ? (appDateInput.value || '').trim() : '';
    const examDate = (examDateInput.value || '').trim();
    if (isYesToken(requestedValue) && appDate && examDate && examDate === appDate) {
     examDateInput.setAttribute('data-exam-auto', '1');
     examDateInput.setAttribute('data-exam-auto-source', appDate);
    }

    refreshExamRequestState();
   });
  }

  function initFormSafetyUx() {
   const forms = Array.from(document.querySelectorAll('form[data-disable-submit="1"]'));
   if (!forms.length) return;

   function isTrackableControl(control) {
    if (!(control instanceof HTMLInputElement || control instanceof HTMLSelectElement || control instanceof HTMLTextAreaElement)) {
     return false;
    }
    if (control.disabled) return false;
    if (control instanceof HTMLInputElement) {
     const type = (control.type || '').toLowerCase();
     if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'reset' || type === 'image' || type === 'file') {
      return false;
     }
    }
    return true;
   }

   function controlState(control) {
    if (control instanceof HTMLInputElement) {
     const type = (control.type || '').toLowerCase();
     if (type === 'checkbox' || type === 'radio') {
      return control.checked ? '1' : '0';
     }
     return control.value || '';
    }
    if (control instanceof HTMLSelectElement && control.multiple) {
     return Array.from(control.selectedOptions).map((opt) => opt.value).join('|');
    }
    return control.value || '';
   }

   function ensureSaveHint(form) {
    const row =
     form.querySelector('.d-flex.justify-content-end.gap-2.mb-3') ||
     form.querySelector('.d-flex.justify-content-end.gap-2.mt-3') ||
     form.querySelector('.d-flex.justify-content-end.gap-2') ||
     form.querySelector('.intake-footer');
    if (!row) return null;

    let hint = row.querySelector('[data-form-shortcut-hint="1"]');
    if (!hint) {
     hint = document.createElement('span');
     hint.className = 'small text-muted me-auto';
     hint.setAttribute('data-form-shortcut-hint', '1');
     hint.textContent = 'Ctrl+S Save · warn before leaving with changes';
     row.insertBefore(hint, row.firstChild);
    }

    let badge = row.querySelector('[data-form-dirty-indicator="1"]');
    if (!badge) {
     badge = document.createElement('span');
     badge.className = 'badge bg-warning text-dark';
     badge.style.display = 'none';
     badge.textContent = 'Unsaved changes';
     badge.setAttribute('data-form-dirty-indicator', '1');
     if (hint.nextSibling) {
      row.insertBefore(badge, hint.nextSibling);
     } else {
      row.appendChild(badge);
     }
    }
    return badge;
   }

   const stateList = forms.map((form) => {
    const controls = Array.from(form.querySelectorAll('input, select, textarea')).filter(isTrackableControl);
    const initialState = new Map();
    controls.forEach((control) => {
     initialState.set(control, controlState(control));
    });
    const dirtyBadge = ensureSaveHint(form);
    return { form, controls, initialState, dirtyBadge };
   });

   function refreshDirty(state) {
    const dirty = state.controls.some((control) => controlState(control) !== state.initialState.get(control));
    state.form.dataset.dirty = dirty ? '1' : '0';
    if (state.dirtyBadge) {
     state.dirtyBadge.style.display = dirty ? '' : 'none';
    }
   }

   stateList.forEach((state) => {
    state.controls.forEach((control) => {
     control.addEventListener('input', () => refreshDirty(state));
     control.addEventListener('change', () => refreshDirty(state));
    });
    state.form.addEventListener('submit', () => {
     state.form.dataset.submitting = '1';
     state.form.dataset.dirty = '0';
     if (state.dirtyBadge) state.dirtyBadge.style.display = 'none';
    });
    refreshDirty(state);
   });

   window.addEventListener('beforeunload', (event) => {
    const hasDirty = stateList.some((state) => {
     return state.form.dataset.submitting !== '1' && state.form.dataset.dirty === '1';
    });
    if (!hasDirty) return;
    event.preventDefault();
    event.returnValue = '';
   });

   document.addEventListener('keydown', (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.shiftKey || event.altKey) return;
    if (String(event.key || '').toLowerCase() !== 's') return;
    const active = document.activeElement;
    let target = stateList.find((state) => active && state.form.contains(active));
    if (!target) {
     target = stateList[0];
    }
    if (!target || target.form.dataset.submitting === '1') return;
    event.preventDefault();
    if (typeof target.form.requestSubmit === 'function') {
     target.form.requestSubmit();
    } else {
     target.form.submit();
    }
   });
  }

  function initStructuredCaseTableUx() {
   ensureCaseFormUxStyle();
   const forms = Array.from(document.querySelectorAll('form[data-disable-submit="1"]'));
   if (!forms.length) return;

   forms.forEach((form) => {
    if (form.dataset.caseStructuredUxInitialized === '1') return;
    const headingRows = Array.from(
     form.querySelectorAll('.d-flex.justify-content-between.align-items-center.mt-3.mb-2')
    );
    const sections = [];
    const seenTableWraps = new Set();

    function resolveTitleElement(headerRow) {
     if (!(headerRow instanceof Element)) return null;
     return (
      headerRow.querySelector('.fw-bold, h5, h6, .h5, .h6') ||
      headerRow.firstElementChild ||
      null
     );
    }

    function addSection(tableWrap, headerRow, titleEl) {
     if (!(tableWrap instanceof Element) || seenTableWraps.has(tableWrap)) return;
     const rows = Array.from(tableWrap.querySelectorAll('tbody tr'));
     if (!rows.length) return;

     let effectiveHeaderRow = headerRow instanceof Element ? headerRow : null;
     let effectiveTitleEl = titleEl instanceof Element ? titleEl : null;

     if (!effectiveHeaderRow) {
      effectiveHeaderRow = document.createElement('div');
      effectiveHeaderRow.className =
       'd-flex justify-content-between align-items-center mt-3 mb-2';
      tableWrap.insertAdjacentElement('beforebegin', effectiveHeaderRow);
     }

     if (!effectiveTitleEl) {
      effectiveTitleEl = resolveTitleElement(effectiveHeaderRow);
     }
     if (!effectiveTitleEl) {
      effectiveTitleEl = document.createElement('div');
      effectiveTitleEl.className = 'fw-bold';
      effectiveHeaderRow.insertAdjacentElement('afterbegin', effectiveTitleEl);
     }
     const lastChild = effectiveHeaderRow.lastElementChild;
     if (!(lastChild instanceof Element) || lastChild === effectiveTitleEl) {
      const actionHost = document.createElement('div');
      effectiveHeaderRow.appendChild(actionHost);
     }

     const sectionNo = sections.length + 1;
     const fallbackTitle = `Section ${sectionNo}`;
     const title = (effectiveTitleEl.textContent || '').trim() || fallbackTitle;
     if (!(effectiveTitleEl.textContent || '').trim()) {
      effectiveTitleEl.textContent = fallbackTitle;
     }

     sections.push({
      id: `sec-${sectionNo}`,
      title,
      headerRow: effectiveHeaderRow,
      titleEl: effectiveTitleEl,
      tableWrap,
      rows,
      toggleBtn: null,
      countBadge: null,
     });
     seenTableWraps.add(tableWrap);
    }

    headingRows.forEach((row) => {
     const tableWrap = row.nextElementSibling;
     if (!(tableWrap instanceof Element) || !tableWrap.classList.contains('table-responsive')) {
      return;
     }
     addSection(tableWrap, row, resolveTitleElement(row));
    });

    Array.from(form.querySelectorAll('.table-responsive')).forEach((tableWrap) => {
     if (!(tableWrap instanceof Element) || seenTableWraps.has(tableWrap)) return;
     const prev = tableWrap.previousElementSibling;
     if (
      prev instanceof Element &&
      prev.matches('.d-flex.justify-content-between.align-items-center.mt-3.mb-2')
     ) {
      addSection(tableWrap, prev, resolveTitleElement(prev));
      return;
     }
     addSection(tableWrap, null, null);
    });

    if (!sections.length) return;

    const storageKey = `app.case.form.sections:${window.location.pathname}:${form.getAttribute('action') || ''}`;
    let collapsedState = {};
    try {
     const raw = window.localStorage.getItem(storageKey);
     collapsedState = raw ? JSON.parse(raw) : {};
    } catch (e) {
     collapsedState = {};
    }
    if (!collapsedState || typeof collapsedState !== 'object') {
     collapsedState = {};
    }

    function saveCollapsedState() {
     try {
      window.localStorage.setItem(storageKey, JSON.stringify(collapsedState));
     } catch (e) {}
    }

    function setSectionCollapsed(section, collapsed, persist) {
     const shouldCollapse = !!collapsed;
     section.headerRow.classList.toggle('case-form-section-collapsed', shouldCollapse);
     section.tableWrap.style.display = shouldCollapse ? 'none' : '';
     if (section.toggleBtn) {
      section.toggleBtn.textContent = shouldCollapse ? 'Expand' : 'Collapse';
      section.toggleBtn.setAttribute('aria-expanded', shouldCollapse ? 'false' : 'true');
     }
     if (persist) {
      collapsedState[section.id] = shouldCollapse;
      saveCollapsedState();
     }
    }

    const fieldMeta =
     window.CASE_FORM_FIELD_META && typeof window.CASE_FORM_FIELD_META === 'object'
      ? window.CASE_FORM_FIELD_META
      : {};
    const requiredFieldNames = new Set(
     Object.keys(fieldMeta).filter((key) => {
      const info = fieldMeta[key];
      return !!(info && info.required);
     })
    );

    function isEffectivelyHidden(el) {
     if (!(el instanceof Element)) return false;
     if (el.hidden) return true;
     if (el.closest('.d-none, [hidden]')) return true;
     let node = el;
     while (node && node !== form && node instanceof Element) {
      const style = window.getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return true;
      node = node.parentElement;
     }
     return false;
    }

    function isRequiredControl(el, row) {
     if (!(el instanceof Element)) return false;
     if (el.hasAttribute('required')) return true;
     const name = (el.getAttribute('name') || '').trim();
     if (!name) return false;
     if (requiredFieldNames.has(name)) return true;
     if (el instanceof HTMLInputElement && el.type === 'radio') {
      const escaped = cssEscape(name);
      return !!row.querySelector(`input[type="radio"][name="${escaped}"][required]`);
     }
     return false;
    }

    function evaluateRowState(row) {
     const controls = Array.from(row.querySelectorAll('input, select, textarea')).filter((el) => {
      if (el.disabled) return false;
      if (isEffectivelyHidden(el)) return false;
      if (el instanceof HTMLInputElement) {
       const t = (el.type || '').toLowerCase();
       if (t === 'hidden' || t === 'button' || t === 'submit' || t === 'reset' || t === 'file') {
        return false;
       }
      }
      return true;
     });
     if (!controls.length) return 'done';

     const requiredControls = controls.filter((el) => isRequiredControl(el, row));
     const targetControls = requiredControls.length ? requiredControls : controls;

     let total = 0;
     let filled = 0;
     const seenRadioNames = new Set();

     targetControls.forEach((el) => {
      if (el instanceof HTMLInputElement && el.type === 'radio') {
       const name = (el.name || '').trim();
       if (!name || seenRadioNames.has(name)) return;
       seenRadioNames.add(name);
       total += 1;
       const selected = row.querySelector(`input[type="radio"][name="${cssEscape(name)}"]:checked`);
       const value = selected ? String(selected.value || '').trim() : '';
       if (value) filled += 1;
       return;
      }
      total += 1;
      if (el instanceof HTMLInputElement && el.type === 'checkbox') {
       if (el.checked) filled += 1;
       return;
      }
      if (String(el.value || '').trim()) {
       filled += 1;
      }
     });

     if (total === 0) return 'done';
     if (filled === 0) return 'empty';
     if (filled === total) return 'done';
     return 'partial';
    }

    let showPendingOnly = false;
    let pendingCountBadge = null;
    let onlyPendingBtn = null;

    function refreshRowStates() {
     let totalPending = 0;
     sections.forEach((section) => {
      let sectionPending = 0;
      section.rows.forEach((row) => {
       const state = evaluateRowState(row);
       row.dataset.caseFormRowState = state;
       const isPending = state !== 'done';
       row.classList.toggle('case-form-row-pending', isPending);
       if (showPendingOnly && !isPending) {
        row.style.display = 'none';
       } else {
        row.style.display = '';
       }
       if (isPending) sectionPending += 1;
      });
      if (section.countBadge) {
       section.countBadge.textContent = `Missing ${sectionPending}`;
       section.countBadge.style.display = sectionPending> 0 ? '' : 'none';
      }
      totalPending += sectionPending;
     });
     if (pendingCountBadge) {
      pendingCountBadge.textContent = `Remaining required fields ${totalPending}`;
     }
     if (onlyPendingBtn) {
      onlyPendingBtn.classList.toggle('btn-warning', showPendingOnly);
      onlyPendingBtn.classList.toggle('btn-outline-secondary', !showPendingOnly);
      onlyPendingBtn.textContent = showPendingOnly ? 'Show all' : 'Missing only';
     }
    }

    function buildToolbar() {
     const toolbar = document.createElement('div');
     toolbar.className = 'case-form-toolbar d-flex flex-wrap align-items-center gap-2 mb-3';

     const title = document.createElement('span');
     title.className = 'toolbar-title';
     title.textContent = 'Input helper';
     toolbar.appendChild(title);

     onlyPendingBtn = document.createElement('button');
     onlyPendingBtn.type = 'button';
     onlyPendingBtn.className = 'btn btn-sm btn-outline-secondary';
     onlyPendingBtn.textContent = 'Missing only';
     onlyPendingBtn.addEventListener('click', () => {
      showPendingOnly = !showPendingOnly;
      refreshRowStates();
     });
     toolbar.appendChild(onlyPendingBtn);

     const expandBtn = document.createElement('button');
     expandBtn.type = 'button';
     expandBtn.className = 'btn btn-sm btn-outline-secondary';
     expandBtn.textContent = 'Expand all';
     expandBtn.addEventListener('click', () => {
      sections.forEach((section) => setSectionCollapsed(section, false, true));
     });
     toolbar.appendChild(expandBtn);

     const collapseBtn = document.createElement('button');
     collapseBtn.type = 'button';
     collapseBtn.className = 'btn btn-sm btn-outline-secondary';
     collapseBtn.textContent = 'Collapse all';
     collapseBtn.addEventListener('click', () => {
      sections.forEach((section) => setSectionCollapsed(section, true, true));
     });
     toolbar.appendChild(collapseBtn);

     const select = document.createElement('select');
     select.className = 'form-select form-select-sm';
     select.style.maxWidth = '280px';
     const initialOpt = document.createElement('option');
     initialOpt.value = '';
     initialOpt.textContent = 'Quick section jump';
     select.appendChild(initialOpt);
     sections.forEach((section, idx) => {
      const opt = document.createElement('option');
      opt.value = section.id;
      opt.textContent = `${idx + 1}. ${section.title}`;
      select.appendChild(opt);
     });
     select.addEventListener('change', () => {
      const id = select.value;
      if (!id) return;
      const section = sections.find((s) => s.id === id);
      if (!section) return;
      setSectionCollapsed(section, false, true);
      section.headerRow.scrollIntoView({ behavior: 'smooth', block: 'start' });
      window.setTimeout(() => {
       select.value = '';
      }, 250);
     });
     toolbar.appendChild(select);

     pendingCountBadge = document.createElement('span');
     pendingCountBadge.className = 'badge bg-warning text-dark ms-auto';
     pendingCountBadge.textContent = 'Remaining required fields 0';
     toolbar.appendChild(pendingCountBadge);

     const anchor =
      form.querySelector('.d-flex.justify-content-end.gap-2.mb-3') ||
      form.querySelector('.d-flex.justify-content-end.gap-2.mt-3') ||
      sections[0].headerRow;
     if (anchor && anchor.parentElement) {
      anchor.insertAdjacentElement('afterend', toolbar);
     }
    }

    sections.forEach((section) => {
     const actionHost = section.headerRow.lastElementChild || section.headerRow;
     actionHost.classList.add('d-flex', 'align-items-center', 'gap-2');

     const countBadge = document.createElement('span');
     countBadge.className = 'badge text-bg-warning';
     countBadge.style.display = 'none';
     countBadge.textContent = 'Missing 0';
     actionHost.appendChild(countBadge);
     section.countBadge = countBadge;

     const toggleBtn = document.createElement('button');
     toggleBtn.type = 'button';
     toggleBtn.className = 'case-form-section-toggle btn btn-sm btn-outline-secondary';
     toggleBtn.textContent = 'Collapse';
     toggleBtn.addEventListener('click', () => {
      const currentlyCollapsed = section.tableWrap.style.display === 'none';
      setSectionCollapsed(section, !currentlyCollapsed, true);
     });
     actionHost.appendChild(toggleBtn);
     section.toggleBtn = toggleBtn;
    });

    buildToolbar();

    sections.forEach((section) => {
     const shouldCollapse = !!collapsedState[section.id];
     setSectionCollapsed(section, shouldCollapse, false);
    });

    form.addEventListener('input', (event) => {
     if (!(event.target instanceof Element)) return;
     if (!event.target.closest('table')) return;
     refreshRowStates();
    });
    form.addEventListener('change', (event) => {
     if (!(event.target instanceof Element)) return;
     if (!event.target.closest('table')) return;
     refreshRowStates();
    });

    refreshRowStates();
    form.dataset.caseStructuredUxInitialized = '1';
   });
  }

  function initSubmitOnce() {
   function restoreSubmitState(form) {
    delete form.dataset.submitOnce;
    delete form.dataset.submitting;
    form
     .querySelectorAll('button[type="submit"], input[type="submit"]')
     .forEach((btn) => {
      btn.disabled = false;
      if (btn.tagName === 'BUTTON' && btn.dataset.originalText !== undefined) {
       btn.textContent = btn.dataset.originalText;
      }
     });
   }

   document.querySelectorAll('form[data-disable-submit="1"]').forEach((form) => {
    form.addEventListener('submit', (e) => {
     if (e.defaultPrevented) return;
     if (form.dataset.submitOnce === '1') {
      e.preventDefault();
      return;
     }
     form.dataset.submitOnce = '1';
     // Keep this flag for dirty-form/beforeunload guards.
     form.dataset.submitting = '1';
     form
      .querySelectorAll('button[type="submit"], input[type="submit"]')
      .forEach((btn) => {
       if (btn.dataset.originalText === undefined && btn.tagName === 'BUTTON') {
        btn.dataset.originalText = btn.textContent || '';
        btn.textContent = 'Processing...';
       }
       btn.disabled = true;
      });
     const rollbackIfPrevented = () => {
      if (!e.defaultPrevented) return;
      restoreSubmitState(form);
     };
     if (typeof queueMicrotask === 'function') {
      queueMicrotask(rollbackIfPrevented);
     } else {
      Promise.resolve().then(rollbackIfPrevented);
     }
    });
   });
  }

  function safeInit(label, fn) {
   try {
    fn();
   } catch (err) {
    console.error(`[case_form] ${label} failed`, err);
   }
  }

  safeInit('upgradeParameterizedFieldControls', upgradeParameterizedFieldControls);
  safeInit('initClientSearch', initClientSearch);
  safeInit('initStaffSearch', initStaffSearch);
  safeInit('initSameClientSync', initSameClientSync);
  safeInit('initSyncedFields', initSyncedFields);
  safeInit('initOurRefSegments', initOurRefSegments);
  safeInit('initFilingDeadlineTypeSelectors', initFilingDeadlineTypeSelectors);
  safeInit('initImageDropzones', initImageDropzones);
  safeInit('initFamilyCheck', initFamilyCheck);
  safeInit('initExamRequestUx', initExamRequestUx);
  safeInit('initStructuredCaseTableUx', initStructuredCaseTableUx);
  safeInit('applyFieldHelpText', applyFieldHelpText);
  safeInit('applyMissingFieldErrors', applyMissingFieldErrors);
  safeInit('initClientIdGuard', initClientIdGuard);
  safeInit('initFormSafetyUx', initFormSafetyUx);
  safeInit('initSubmitOnce', initSubmitOnce);

  // Retry once after initial paint to handle late DOM changes safely.
  window.setTimeout(() => {
   safeInit('initStructuredCaseTableUx(retry)', initStructuredCaseTableUx);
  }, 80);
 }

 if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootCaseForm, { once: true });
 } else {
  bootCaseForm();
 }
})();
