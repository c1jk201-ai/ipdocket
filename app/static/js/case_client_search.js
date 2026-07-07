(function () {
 'use strict';

 const MODAL_ID = 'clientSearchModal';
 const INPUT_ID = 'clientSearchInput';
 const RESULTS_ID = 'clientSearchResults';
 const CREATE_MODAL_ID = 'clientCreateModal';
 const CREATE_FRAME_ID = 'clientCreateFrame';

 let clientModal = null;
 let clientCreateModal = null;
 let targetNameId = 'clientName';
 let targetIdId = 'clientId';

 function ensureModal() {
  const modalEl = document.getElementById(MODAL_ID);
  if (!modalEl) return null;
  if (!clientModal) {
   if (!window.bootstrap || !window.bootstrap.Modal) return null;
   clientModal = new window.bootstrap.Modal(modalEl);
  }
  return modalEl;
 }

 function ensureCreateModal() {
  const modalEl = document.getElementById(CREATE_MODAL_ID);
  if (!modalEl) return null;
  if (!clientCreateModal) {
   if (!window.bootstrap || !window.bootstrap.Modal) return null;
   clientCreateModal = new window.bootstrap.Modal(modalEl);
  }
  return modalEl;
 }

 function openClientSearch(nameId, idId) {
  targetNameId = nameId || 'clientName';
  if (typeof idId === 'undefined') {
   targetIdId = 'clientId';
  } else {
   targetIdId = idId;
  }
  const modalEl = ensureModal();
  if (!modalEl) return;
  const input = document.getElementById(INPUT_ID);
  const list = document.getElementById(RESULTS_ID);
  if (input) input.value = '';
  if (list) list.innerHTML = '';
  clientModal.show();
  window.setTimeout(() => {
   if (input) input.focus();
  }, 150);
 }

 function openClientCreateModal(nameId, idId) {
  if (nameId) {
   targetNameId = nameId;
  }
  if (typeof idId !== 'undefined') {
   targetIdId = idId;
  }
  const modalEl = ensureCreateModal();
  if (!modalEl) return;
  const frame = document.getElementById(CREATE_FRAME_ID);
  const baseUrl = (modalEl.dataset && modalEl.dataset.createUrl) || '';
  if (frame && baseUrl) {
   const joiner = baseUrl.includes('?') ? '&' : '?';
   frame.src = `${baseUrl}${joiner}ts=${Date.now()}`;
  }
  if (clientModal) clientModal.hide();
  clientCreateModal.show();
 }

 function closeClientCreateModal() {
  if (clientCreateModal) clientCreateModal.hide();
 }

 function applyClientSelection(payload) {
  const clientId =
   payload.client_id ||
   payload.clientId ||
   payload.id ||
   payload.clientID ||
   '';
  const clientName =
   payload.client_name ||
   payload.clientName ||
   payload.name ||
   payload.clientNameKo ||
   '';
  if (clientName) {
   const nameEl = targetNameId ? document.getElementById(targetNameId) : null;
   if (nameEl) nameEl.value = clientName;
  }
  if (clientId) {
   const idEl = targetIdId ? document.getElementById(targetIdId) : null;
   if (idEl) idEl.value = clientId;
  }
 }

 function handleClientCreated(payload) {
  if (!payload) return;
  applyClientSelection(payload);
  closeClientCreateModal();
 }

 function getMessage(modalEl, key, fallback) {
  if (!modalEl || !modalEl.dataset) return fallback;
  return modalEl.dataset[key] || fallback;
 }

 async function runClientSearch() {
  const modalEl = ensureModal();
  if (!modalEl) return;
  const input = document.getElementById(INPUT_ID);
  const list = document.getElementById(RESULTS_ID);
  if (!input || !list) return;
  const q = (input.value || '').trim();
  list.innerHTML = '...';
  try {
   const res = await fetch(`/api/clients/search?q=${encodeURIComponent(q)}`);
   const data = (window.AppFetch && typeof window.AppFetch.parseJsonResponse === "function")
    ? await window.AppFetch.parseJsonResponse(res)
    : await res.json();
   list.innerHTML = '';
   if (!Array.isArray(data) || data.length === 0) {
    list.textContent = getMessage(modalEl, 'noResults', 'No results');
    return;
   }
   data.forEach((item) => {
    const a = document.createElement('a');
    a.className = 'list-group-item list-group-item-action';
    a.textContent = `${item.name || ''}${item.email ? ` (${item.email})` : ''}`;
    a.addEventListener('click', () => {
     applyClientSelection({ id: item.id || '', name: item.name || '' });
     if (clientModal) clientModal.hide();
    });
    list.appendChild(a);
   });
  } catch (e) {
   list.textContent = getMessage(modalEl, 'error', 'Error');
  }
 }

 function bindEnterKey() {
  const input = document.getElementById(INPUT_ID);
  if (!input || input.dataset.bound === '1') return;
  input.dataset.bound = '1';
  input.addEventListener('keydown', (e) => {
   if (e.key === 'Enter') {
    e.preventDefault();
    runClientSearch();
   }
  });
 }

 function bindClientCreateButtons() {
  document.querySelectorAll('[data-client-create="1"]').forEach((btn) => {
   if (btn.dataset.bound === '1') return;
   btn.dataset.bound = '1';
   btn.addEventListener('click', (e) => {
    e.preventDefault();
    const scope = btn.closest('.position-relative') || btn.parentElement;
    const input = scope ? scope.querySelector('[data-client-search="1"]') : null;
    if (input && input.id) {
     openClientCreateModal(input.id, input.dataset.clientId || null);
    } else {
     openClientCreateModal();
    }
   });
  });
 }

 window.openClientSearch = openClientSearch;
 window.openClientCreateModal = openClientCreateModal;
 window.clientCreateSuccess = handleClientCreated;
 window.closeClientCreateModal = closeClientCreateModal;
 window.runClientSearch = runClientSearch;

 function bootClientSearch() {
  bindEnterKey();
  bindClientCreateButtons();
 }

 if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootClientSearch, { once: true });
 } else {
  bootClientSearch();
 }
})();
