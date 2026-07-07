import {
 escapeAttr,
 escapeHtml,
 safeUrl,
 parseIsoDate,
 formatDateTime,
 formatRelativeTime,
 formatMoney,
 formatDDay,
 isPreviewableFilename,
 truncateText,
 stringifyAuditValue,
 formatAuditValue,
} from "./case_matter_view/utils.js";
import {
 DOC_TYPE_LABELS,
 AUDIT_ACTION_LABELS,
 AUDIT_ACTION_ORDER,
 AUDIT_FIELD_LABELS,
 CUSTOM_TEXT_LABELS,
} from "./case_matter_view/constants.js";
import {
 CASE_VIEW,
 CASE_VIEW_CONFIG,
 STAFF_USER_MAP,
 PreferencesManager,
 apiForm,
 apiJson,
 bindElementEventOnce,
 confirmAction,
 getCurrentHashAnchor,
 getStoredCasePref,
 getTodayIsoDate,
 ipmAlert,
 ipmConfirm,
 ipmPrompt,
 refreshCaseSection,
 setInputValueIfEmpty,
 setStoredCasePref,
 showToast,
 showUndoToast,
} from "./case_matter_view/runtime.js";

 function setupStaticFormBindings() {
  const today = getTodayIsoDate();
  setInputValueIfEmpty('#request_start_date_input', today);
  setupTerminalStatusPresets();
  setupTerminalStatusConfirm();
  setupWorkflowCategoryManualBindings();
  setupWorkflowAssignCategoryBindings();
  const initialAssignMode = document.getElementById('wfAssignModeInput')?.value || 'distribution';
  setWorkflowAssignMode(initialAssignMode);

  const wfFilter = document.getElementById('wfFilterType');
  bindElementEventOnce(wfFilter, "filter", "change", applyWorkflowFilter);
  setupClosedWorkflowCards();
  applyWorkflowFilter();

  setupFinanceSubmitDelegation();
  setupFinanceFormBindings();
 }

 function isTerminalMatterStatus(value) {
  const compact = String(value || "").trim().replace(/\s+/g, "").toLowerCase();
  if (!compact) return false;
  if (compact === "Done" || compact === "complete" || compact === "completed") return true;
  return [
   "Matter closed",
   "Term expired",
   "Abandoned",
   "Withdrawn",
   "Transferred",
   "",
   "",
   "abandon",
   "withdraw",
   "transfer",
   "closed",
   "expired",
   "giveup",
   "forfeit",
  ].some((token) => compact.includes(token));
 }

 function setupTerminalStatusConfirm(root) {
  const scope = root || document;
  scope.querySelectorAll('form[data-terminal-status-confirm="1"]').forEach((form) => {
   bindElementEventOnce(form, "terminal-status-confirm", "submit", async (event) => {
    const statusInput = form.querySelector('[name="new_status"]');
    if (!isTerminalMatterStatus(statusInput ? statusInput.value : "")) return;
    event.preventDefault();
    const ok = await ipmConfirm(
     " Status Save Open tasks Deadline Task Abandoned Process  . ?",
     { title: "Matter Process" },
    );
    if (!ok) return;
    form.submit();
   });
  });
 }

 function setupTerminalStatusPresets(root) {
  const scope = root || document;
  scope.querySelectorAll("[data-terminal-status-preset]").forEach((btn) => {
   bindElementEventOnce(btn, "terminal-status-preset", "click", (event) => {
    event.preventDefault();
    const form = btn.closest('form[data-terminal-status-confirm="1"]');
    if (!form) return;
    const statusInput = form.querySelector('[name="new_status"]');
    const dateInput = form.querySelector('[name="status_date"]');
    const noteInput = form.querySelector('[name="status_note"]');
    const value = btn.getAttribute("data-terminal-status-preset") || "";
    if (statusInput) statusInput.value = value;
    if (dateInput && !dateInput.value) dateInput.value = getTodayIsoDate();
    if (noteInput) noteInput.focus();
   });
  });
 }

 function setupFinanceFormBindings() {
  const invoiceLinkForm = document.getElementById('invoiceLinkForm');
  bindElementEventOnce(invoiceLinkForm, "invoice-submit", "submit", handleInvoiceLinkSubmit);

  const payableForm = document.getElementById('payableCreateForm');
  bindElementEventOnce(payableForm, "payable-submit", "submit", handlePayableCreate);
 }

 function setupFinanceSubmitDelegation() {
  if (document.documentElement.getAttribute("data-case-view-finance-submit-delegated") === "1") return;
  document.documentElement.setAttribute("data-case-view-finance-submit-delegated", "1");
  document.addEventListener("submit", (event) => {
   const form = event.target;
   if (!(form instanceof HTMLFormElement)) return;
   if (
    form.id === "invoiceLinkForm" &&
    form.getAttribute("data-case-view-invoice-submit-bound") !== "1"
   ) {
    handleInvoiceLinkSubmit(event);
    return;
   }
   if (
    form.id === "payableCreateForm" &&
    form.getAttribute("data-case-view-payable-submit-bound") !== "1"
   ) {
    handlePayableCreate(event);
   }
  });
 }

 function toggleBlock(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const showValue = (el.tagName === 'TR') ? 'table-row' : 'block';
  el.style.display = (el.style.display === 'none' || !el.style.display) ? showValue : 'none';
 }
 function toggleAll(tableId, checked) {
  const table = document.getElementById(tableId);
  if (!table) return;
  table.querySelectorAll('tbody input[type="checkbox"][name]').forEach(cb => cb.checked = checked);
 }
 function toggleAnnuityAll() {
  const rows = document.querySelectorAll('#annuityTable tbody tr.annuity-extra');
  if (!rows.length) return;
  const anyHidden = Array.from(rows).some(r => r.style.display === 'none' || !r.style.display);
  rows.forEach(r => r.style.display = anyHidden ? '' : 'none');
  const toggle = document.querySelector('[data-annuity-toggle-all="1"] span');
  if (toggle) toggle.textContent = anyHidden ? 'Collapse' : 'Show all';
 }
 function loadWorkflowClosedExpandedState() {
  const raw = getStoredCasePref("workflowClosedExpanded");
  if (!raw) return {};
  try {
   const parsed = JSON.parse(raw);
   return parsed && typeof parsed === "object" ? parsed : {};
  } catch (e) {
   return {};
  }
 }
 function saveWorkflowClosedExpandedState() {
  try {
   setStoredCasePref("workflowClosedExpanded", JSON.stringify(CASE_WORKSPACE_STATE.workflowClosedExpanded || {}));
  } catch (e) {}
 }
 function getClosedWorkflowRows(visibleOnly = false) {
  return Array.from(document.querySelectorAll('#sec-workflow tbody tr[data-workflow-closed="1"]')).filter((row) => {
   if (!visibleOnly) return true;
   return row.style.display !== 'none';
  });
 }
 function updateClosedWorkflowRowToggle(row) {
  if (!row) return;
  const expanded = row.dataset.workflowClosedExpanded === "1";
  row.querySelectorAll("[data-workflow-toggle-closed-details]").forEach((btn) => {
   btn.textContent = expanded ? "Collapse" : "Details";
   btn.setAttribute("aria-expanded", expanded ? "true" : "false");
   btn.classList.toggle("btn-outline-secondary", !expanded);
   btn.classList.toggle("btn-outline-primary", expanded);
  });
 }
 function setClosedWorkflowRowExpanded(row, expanded, persist = true) {
  if (!row || row.dataset.workflowClosed !== "1") return;
  const isExpanded = !!expanded;
  const workflowId = (row.dataset.workflowId || "").trim();
  const card = row.querySelector(".workflow-card");
  const body = row.querySelector("[data-workflow-closed-body]");
  const summary = row.querySelector("[data-workflow-closed-summary]");
  row.dataset.workflowClosedExpanded = isExpanded ? "1" : "0";
  if (card) card.classList.toggle("is-collapsed", !isExpanded);
  if (body) body.hidden = !isExpanded;
  if (summary) summary.classList.toggle("is-expanded", isExpanded);
  if (workflowId) {
   CASE_WORKSPACE_STATE.workflowClosedExpanded[workflowId] = isExpanded;
   if (persist) saveWorkflowClosedExpandedState();
  }
  updateClosedWorkflowRowToggle(row);
 }
 function updateWorkflowClosedToggleAll() {
  const btn = document.getElementById("wfClosedToggleAll");
  if (!btn) return;
  const visibleRows = getClosedWorkflowRows(true);
  const targetRows = visibleRows;
  const count = targetRows.length;
  btn.hidden = count === 0;
  btn.disabled = count === 0;
  if (!count) {
   btn.textContent = "ClosedTask Expand";
   btn.setAttribute("aria-pressed", "false");
   return;
  }
  const allExpanded = targetRows.every((row) => row.dataset.workflowClosedExpanded === "1");
  btn.setAttribute("aria-pressed", allExpanded ? "true" : "false");
  btn.textContent = allExpanded ? `Closed ${count}items Collapse` : `Closed ${count}items Expand`;
 }
 function setupClosedWorkflowCards() {
  CASE_WORKSPACE_STATE.workflowClosedExpanded = loadWorkflowClosedExpandedState();
  getClosedWorkflowRows(false).forEach((row) => {
   const workflowId = (row.dataset.workflowId || "").trim();
   const expanded = !!CASE_WORKSPACE_STATE.workflowClosedExpanded[workflowId];
   setClosedWorkflowRowExpanded(row, expanded, false);
   row.querySelectorAll("[data-workflow-toggle-closed-details]").forEach((btn) => {
    if (btn.dataset.closedToggleBound === "1") return;
    btn.dataset.closedToggleBound = "1";
    btn.addEventListener("click", () => {
     const next = row.dataset.workflowClosedExpanded !== "1";
     setClosedWorkflowRowExpanded(row, next, true);
     updateWorkflowClosedToggleAll();
    });
   });
  });
  const toggleAllBtn = document.getElementById("wfClosedToggleAll");
  if (toggleAllBtn && toggleAllBtn.dataset.closedToggleAllBound !== "1") {
   toggleAllBtn.dataset.closedToggleAllBound = "1";
   toggleAllBtn.addEventListener("click", () => {
    const rows = getClosedWorkflowRows(true);
    if (!rows.length) return;
    const shouldExpand = rows.some((row) => row.dataset.workflowClosedExpanded !== "1");
    rows.forEach((row) => setClosedWorkflowRowExpanded(row, shouldExpand, false));
    saveWorkflowClosedExpandedState();
    updateWorkflowClosedToggleAll();
   });
  }
  updateWorkflowClosedToggleAll();
 }
 function applyWorkflowFilter() {
  const sel = document.getElementById('wfFilterType');
  if (!sel) return;
  const value = (sel.value || 'all').toLowerCase();
  const rows = document.querySelectorAll('#sec-workflow tbody tr[data-workflow-type]');
  rows.forEach(row => {
   const t = (row.getAttribute('data-workflow-type') || 'work').toLowerCase();
   const normalizedType = (
    t === 'mgmt_work' || t === 'work_mgmt' || t === 'hybrid'
     ? 'hybrid'
     : (t === 'mgmt' ? 'mgmt' : 'work')
   );
   const visible = value === 'all' || value === normalizedType;
   row.style.display = visible ? '' : 'none';
  });
  updateWorkflowClosedToggleAll();
 }
 const FINANCE_TAB_MAP = {
  ledger: 'costTabLedger',
  invoice: 'costTabInv',
  payable: 'costTabExp',
 };
 const BOTTOM_PANEL_SECTION_IDS = [
  "sec-workflow",
  "sec-cost",
  "sec-annuity",
  "sec-deadlines",
  "sec-memo",
  "sec-audits",
 ];

 function showFinanceTab(tab) {
  const normalizedTab = Object.prototype.hasOwnProperty.call(FINANCE_TAB_MAP, tab) ? tab : 'ledger';
  const costSection = document.getElementById("sec-cost");
  if (costSection && costSection.hasAttribute("hx-get") && !document.getElementById("costTabLedger")) {
   return loadLazyPanelSection(costSection, { force: true }).then((loaded) => {
    return loaded ? showFinanceTab(normalizedTab) : false;
   });
  }
  const targetId = FINANCE_TAB_MAP[normalizedTab] || FINANCE_TAB_MAP.ledger;
  Object.values(FINANCE_TAB_MAP).forEach(id => {
   const el = document.getElementById(id);
   if (el) {
    el.style.display = (id === targetId) ? 'block' : 'none';
   }
  });
  document.querySelectorAll('[data-finance-tab]').forEach(btn => {
   btn.classList.toggle('active', (btn.dataset.financeTab || '') === normalizedTab);
  });
  return Promise.resolve(true);
 }
 function showCostTab(tab) {
  return showFinanceTab(tab);
 }

 let noticeSendPromptTriggered = false;

 async function maybeRunNoticeSendSemiClosePrompt() {
  if (noticeSendPromptTriggered) return;
  const prompt = CASE_VIEW_CONFIG.noticeSendSemiClosePrompt;
  if (!prompt || typeof prompt !== "object") return;

  const docketId = String(prompt.docket_id || "").trim();
  if (!docketId) return;

  const question = String(prompt.question || "").trim() || "Task Done ? ";
  const ackUrl = String(CASE_VIEW_CONFIG.noticeSendSemiCloseAckUrl || "").trim();
  noticeSendPromptTriggered = true;

  window.setTimeout(async () => {
   const confirmed = await ipmConfirm(question);
   if (confirmed) {
    try {
     await apiJson(`/worklog/api/tasks/${encodeURIComponent(docketId)}/complete`, "POST", {
      evidence_type: "memo",
      description: "Matter view Auto Confirm from Done Process",
     });
     if (ackUrl) {
      try {
       await apiJson(ackUrl, "POST", { docket_id: docketId, decision: "yes" });
      } catch (e) {}
     }
     showToast("Task Done Process.", "success");
     window.setTimeout(() => window.location.reload(), 120);
    } catch (err) {
     noticeSendPromptTriggered = false;
     await ipmAlert(err.message || "Task Done Process Error .", { title: "Error" });
    }
    return;
   }

   if (!ackUrl) return;
   try {
    await apiJson(ackUrl, "POST", { docket_id: docketId, decision: "no" });
    showToast("completed status .", "warning");
   } catch (err) {
    noticeSendPromptTriggered = false;
    await ipmAlert(err.message || "Confirm Status Save Error .", { title: "Error" });
   }
  }, 350);
 }

 async function runFinanceMutation(action, fallbackErrorMessage) {
  try {
   await action();
   window.location.reload();
  } catch (err) {
   await ipmAlert(err.message || fallbackErrorMessage, { title: "Error" });
  }
 }

 async function handleInvoiceLinkSubmit(event) {
  event.preventDefault();
  const input = document.getElementById('invoiceLinkRef');
  const ref = (input?.value || '').trim();
  if (!ref) {
   await ipmAlert('Invoice ID  enter.', { title: "Confirm" });
   return;
  }
  await runFinanceMutation(async () => {
   await apiJson(`/api/cases/${CASE_VIEW.caseId}/external-invoice-links`, 'POST', { external_invoice_ref: ref });
  }, 'Link ');
 }

 async function handlePayableCreate(event) {
  event.preventDefault();
  const form = event.target;
  const payload = {};
  new FormData(form).forEach((value, key) => {
   const trimmed = String(value || '').trim();
   if (trimmed) {
    payload[key] = trimmed;
   }
  });
  if (!payload.requested_total) {
   await ipmAlert(' enter.', { title: "Confirm" });
   return;
  }
  await runFinanceMutation(async () => {
   await apiJson(`/api/cases/${CASE_VIEW.caseId}/payables`, 'POST', payload);
  }, ' Registration ');
 }

 async function registerPayablePayment(expenseId, defaultAmount) {
  const amount = await ipmPrompt('', defaultAmount || '');
  if (amount === null) return;
  const sentDate = await ipmPrompt('days (YYYY-MM-DD)', '');
  const fxRate = await ipmPrompt('Exchange rate', '');
  await runFinanceMutation(async () => {
   await apiJson(`/api/payables/${expenseId}/payments`, 'POST', {
     sent_amount: amount,
     sent_date: (sentDate === null ? '' : sentDate) || '',
     fx_rate: (fxRate === null ? '' : fxRate) || '',
    });
  }, ' Registration ');
 }

 async function linkPayableInvoice(expenseId) {
  const invoiceId = await ipmPrompt('Invoice ID', '');
  if (!invoiceId) return;
  const lineItemId = await ipmPrompt('Line items ID (if none )', '');
  const amountMinor = await ipmPrompt('Invoice amount (minor, Select)', '');
  const currency = await ipmPrompt('Currency (default: USD)', '');
  const payload = { billing_invoice_id: invoiceId };
  if (lineItemId) payload.billing_line_item_id = lineItemId;
  if (amountMinor) payload.amount_minor = amountMinor;
  if (currency) payload.currency = currency;
  await runFinanceMutation(async () => {
   await apiJson(`/api/payables/${expenseId}/links/invoice`, 'POST', payload);
  }, 'Link ');
 }

 async function unlinkExpenseInvoice(expenseId, invoiceId, lineItemId) {
  const ok = await confirmAction('Invoice link ?');
  if (!ok) return;
  const query = (lineItemId !== null && lineItemId !== undefined)
   ? `?line_item_id=${lineItemId}`
   : '';
  await runFinanceMutation(async () => {
   await apiJson(`/api/payables/${expenseId}/links/invoice/${invoiceId}${query}`, 'DELETE');
  }, ' ');
 }

 async function editPayable(expenseId, description, requestedTotal, dnDate) {
  const nextDescription = await ipmPrompt('Content', description || '');
  if (nextDescription === null) return;
  const nextRequested = await ipmPrompt('', requestedTotal || '');
  if (nextRequested === null) return;
  const nextDate = await ipmPrompt('days (YYYY-MM-DD)', dnDate || '');
  if (nextDate === null) return;
  await runFinanceMutation(async () => {
   await apiJson(`/api/payables/${expenseId}`, 'PATCH', {
     description: nextDescription,
     requested_total: nextRequested,
     dn_date: nextDate,
    });
  }, 'Edit ');
 }

 async function deletePayable(expenseId) {
  const ok = await confirmAction(' Delete?');
  if (!ok) return;
  await runFinanceMutation(async () => {
   await apiJson(`/api/payables/${expenseId}`, 'DELETE');
  }, 'Delete ');
 }

 function setEditMode(enabled) {
  document.body.classList.toggle('edit-mode', !!enabled);
  const badge = document.getElementById('editModeBadge');
  const btn = document.getElementById('toggleEditMode');
  const btnWorkflow = document.getElementById('toggleEditModeWorkflow');
  if (badge) badge.textContent = enabled ? 'EDIT' : 'VIEW';
  if (btn) btn.textContent = enabled ? ' Closed' : '';
  if (btnWorkflow) btnWorkflow.textContent = enabled ? ' Closed' : '';

  const editableScopes = ['.custom-text-section', '#sec-workflow', '#sec-cost', '#sec-annuity', '#sec-memo'];
  editableScopes.forEach(sel => {
   document.querySelectorAll(sel).forEach(scope => {
    scope.querySelectorAll('input, select, textarea, button[type=\"submit\"], button[form]').forEach(el => {
     if (el.closest('[data-editmode-skip="1"]')) return;
     el.disabled = !enabled;
    });
   });
  });

  if (!enabled) {
   ['wfAssign', 'annuityAdd', 'memoAdd', 'invAdd', 'expAdd'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
   });
  }
 }

 function normalizeWorkflowAssignMode(mode) {
  return String(mode || '').trim().toLowerCase() === 'self' ? 'self' : 'distribution';
 }

 function normalizeWorkflowAssignCategory(value) {
  const normalized = String(value || '').trim().toUpperCase();
  if (normalized === 'MGMT') return 'MGMT';
  if (normalized === 'HYBRID' || normalized === 'MGMT_WORK' || normalized === 'WORK_MGMT') {
   return 'MGMT_WORK';
  }
  return 'WORK';
 }

 function workflowCategoryManualInput(selectEl) {
  if (!selectEl) return null;
  const formId = String(selectEl.getAttribute('form') || '').trim();
  const form = formId ? document.getElementById(formId) : selectEl.form;
  if (!form) return null;
  return form.querySelector('input[name="category_manual"]');
 }

 function setWorkflowCategoryManual(selectEl, isManual) {
  const manualInput = workflowCategoryManualInput(selectEl);
  if (manualInput) manualInput.value = isManual ? '1' : '0';
 }

 function setupWorkflowCategoryManualBindings() {
  document.querySelectorAll('[data-workflow-category-select="1"]').forEach((select) => {
   if (!select || select.dataset.workflowCategoryManualBound === '1') return;
   select.dataset.workflowCategoryManualBound = '1';
   if (!select.dataset.userTouched) select.dataset.userTouched = '0';
   setWorkflowCategoryManual(select, select.dataset.userTouched === '1');
   select.addEventListener('change', () => {
    select.dataset.userTouched = '1';
    setWorkflowCategoryManual(select, true);
    select.value = normalizeWorkflowAssignCategory(select.value);
   });
  });
 }

 function inferWorkflowAssignCategory() {
  const mode = normalizeWorkflowAssignMode(document.getElementById('wfAssignModeInput')?.value || 'distribution');
  if (mode === 'self') return 'WORK';

  const assigneeValue = (document.getElementById('wfAssignAssignee')?.value || '').trim();
  const attorneyValue = (document.getElementById('wfAssignAttorney')?.value || '').trim();
  const inspectorValue = (document.getElementById('wfAssignInspector')?.value || '').trim();

  if (inspectorValue && (assigneeValue || attorneyValue)) return 'MGMT_WORK';
  if (inspectorValue) return 'MGMT';
  return 'WORK';
 }

 function syncWorkflowAssignCategory(force = false) {
  const categorySelect = document.getElementById('wfAssignCategory');
  if (!categorySelect) return;
  if (!force && categorySelect.dataset.userTouched === '1') return;
  categorySelect.value = normalizeWorkflowAssignCategory(inferWorkflowAssignCategory());
  setWorkflowCategoryManual(categorySelect, false);
 }

 function setupWorkflowAssignCategoryBindings() {
  const categorySelect = document.getElementById('wfAssignCategory');
  if (categorySelect && !categorySelect.dataset.userTouched) categorySelect.dataset.userTouched = '0';

  ['wfAssignAssignee', 'wfAssignAttorney', 'wfAssignInspector'].forEach((id) => {
   const select = document.getElementById(id);
   if (!select || select.dataset.workflowCategoryBound === '1') return;
   select.dataset.workflowCategoryBound = '1';
   select.addEventListener('change', () => syncWorkflowAssignCategory(false));
  });
 }

 function setWorkflowAssignMode(mode) {
  const normalized = normalizeWorkflowAssignMode(mode);
  const modeInput = document.getElementById('wfAssignModeInput');
  if (modeInput) modeInput.value = normalized;

  const title = document.getElementById('wfAssignTitleLabel');
  if (title) title.textContent = normalized === 'self' ? ' TaskRegistration' : 'Matter Registration';

  const selfBtn = document.getElementById('wfAssignModeSelfBtn');
  const distributionBtn = document.getElementById('wfAssignModeDistributionBtn');
  if (selfBtn) {
   selfBtn.classList.toggle('btn-primary', normalized === 'self');
   selfBtn.classList.toggle('btn-outline-primary', normalized !== 'self');
   selfBtn.classList.toggle('active', normalized === 'self');
  }
  if (distributionBtn) {
   distributionBtn.classList.toggle('btn-primary', normalized !== 'self');
   distributionBtn.classList.toggle('btn-outline-secondary', normalized === 'self');
   distributionBtn.classList.toggle('active', normalized !== 'self');
  }

  const hintSelf = document.getElementById('wfAssignModeHintSelf');
  const hintDistribution = document.getElementById('wfAssignModeHintDistribution');
  if (hintSelf) hintSelf.style.display = normalized === 'self' ? 'block' : 'none';
  if (hintDistribution) hintDistribution.style.display = normalized === 'self' ? 'none' : 'block';

  document.querySelectorAll('#wfAssign .wf-assign-distribution-only').forEach(el => {
   el.style.display = normalized === 'self' ? 'none' : '';
  });
  document.querySelectorAll('#wfAssign .wf-assign-self-only').forEach(el => {
   el.style.display = normalized === 'self' ? '' : 'none';
  });

  const assigneeSelect = document.getElementById('wfAssignAssignee');
  const attorneySelect = document.getElementById('wfAssignAttorney');
  const inspectorSelect = document.getElementById('wfAssignInspector');

  if (assigneeSelect) {
   assigneeSelect.classList.toggle('bg-light', normalized === 'self');
   assigneeSelect.style.pointerEvents = normalized === 'self' ? 'none' : '';
   if (normalized === 'self') {
    assigneeSelect.setAttribute('tabindex', '-1');
   } else {
    assigneeSelect.removeAttribute('tabindex');
   }
  }

  if (normalized === 'self') {
   const currentUserId = ((document.getElementById('wfAssignCurrentUserId')?.value) || '').trim();
   if (assigneeSelect && currentUserId) assigneeSelect.value = currentUserId;
   if (attorneySelect) attorneySelect.value = '';
   if (inspectorSelect) inspectorSelect.value = '';
   ['wfAssignAssigneeMore', 'wfAssignAttorneyMore', 'wfAssignInspectorMore'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
   });
  }

  syncWorkflowAssignCategory(false);
 }

 function openWorkflowAssign(mode = 'distribution') {
  const root = document.getElementById('caseViewRoot');
  const canAssign = ((root?.dataset.canAssignStaff) || '').trim();
  const canEdit = ((root?.dataset.canEditCase) || '').trim();
  const assignAllowed = canAssign === '1' || canAssign.toLowerCase() === 'true';
  const editAllowed = canEdit === '1' || canEdit.toLowerCase() === 'true';
  if (!assignAllowed && !editAllowed) {
   ipmAlert('You do not have permission to create or assign tasks.', { title: "Permissions" });
   return;
  }
  setEditMode(true);
  const assign = document.getElementById('wfAssign');
  if (assign) assign.style.display = 'block';
  setWorkflowAssignMode(mode);
 }

 function openCostAdd(id) {
  setEditMode(true);
  const inv = document.getElementById('invAdd');
  const exp = document.getElementById('expAdd');
  if (inv) inv.style.display = 'none';
  if (exp) exp.style.display = 'none';
  const target = document.getElementById(id);
  if (target) target.style.display = 'block';
 }

 function openAnnuityAdd() {
  setEditMode(true);
  const el = document.getElementById('annuityAdd');
  if (el) el.style.display = 'block';
 }

 function openAnnuityEdit(btn) {
  openAnnuityAdd();
  const wrap = document.getElementById('annuityAdd');
  const form = wrap ? wrap.querySelector('form') : null;
  if (!form || !btn || !btn.dataset) return;

  const setVal = (name, value) => {
   const el = form.querySelector(`[name="${name}"]`);
   if (!el) return;
   el.value = (value ?? "").toString();
  };

  setVal("cycle_no", btn.dataset.cycleNo || "");
  setVal("due_date", btn.dataset.dueDate || "");
  setVal("extended_due_date", btn.dataset.extendedDueDate || "");
  setVal("internal_due_date", btn.dataset.internalDueDate || "");
  setVal("paid_date", btn.dataset.paidDate || "");
  setVal("paid_amount", btn.dataset.paidAmount || "");
  setVal("official_fee", btn.dataset.officialFee || "");
  setVal("discount_rate", btn.dataset.discountRate || "");

  const st = (btn.dataset.annuityStatus || "").toString();
  const stSel = form.querySelector('[name="annuity_status"]');
  if (stSel) {
   stSel.value = st;
   // If the stored value is unknown, fall back to "(Auto)".
   if (st && stSel.value !== st) stSel.value = "";
  }

  const memoRaw = (btn.dataset.memo || "").toString();
  setVal("memo", memoRaw.replace(/\\n/g, "\n"));

  const ob = form.querySelector('[name="overwrite_blanks"]');
  if (ob) ob.checked = false;

  try {
   if (typeof scrollToId === "function") scrollToId("annuityAdd");
  } catch (e) {}
 }

 function openMemoAdd() {
  setEditMode(true);
  const el = document.getElementById('memoAdd');
  if (el) el.style.display = 'block';
  setupMemoAttachmentDropzones();
 }

 async function submitAnnuityDelete() {
  setEditMode(true);
  const form = document.getElementById('annuityDel');
  if (!form) return;
  const checked = document.querySelectorAll('#sec-annuity input[name=\"annuity_ids\"]:checked');
  if (!checked.length) {
   await ipmAlert('Delete Select.', { title: "Confirm" });
   return;
  }
  const ok = await ipmConfirm(' Delete ? ');
  if (!ok) return;
  form.submit();
 }

 function copySelectValue(targetSelectId, selectEl) {
  const target = document.getElementById(targetSelectId);
  if (!target || !selectEl) return;
  const v = (selectEl.value || '').toString();
  if (!v) return;
  target.value = v;
  selectEl.selectedIndex = 0;
 }

 function inferSectionKeyFromHxGet(target) {
  if (!target || !target.hasAttribute("hx-get")) return "";
  const raw = (target.getAttribute("hx-get") || "").trim();
  if (!raw) return "";
  try {
   const parsed = new URL(raw, window.location.origin);
   const match = parsed.pathname.match(/\/section\/([^/]+)/i);
   return match ? decodeURIComponent(match[1] || "").trim().toLowerCase() : "";
  } catch (e) {
   const match = raw.match(/\/section\/([^/?#]+)/i);
   return match ? decodeURIComponent(match[1] || "").trim().toLowerCase() : "";
  }
 }

 function isLazyPanelPlaceholder(target) {
  if (!target) return false;
  const text = (target.textContent || "").trim();
  return !!target.querySelector(".spinner-border") && text.includes("Loading");
 }

 async function loadLazyPanelSection(target, opts = {}) {
  const force = !!opts.force;
  if (!target || !target.hasAttribute("hx-get")) return false;
  if (target.dataset.lazyLoaded === "1" && !force) return true;
  if (target.dataset.lazyLoading === "1") return false;
  const sectionKey = inferSectionKeyFromHxGet(target);
  if (!sectionKey) return false;

  target.dataset.lazyLoading = "1";
  target.setAttribute("aria-busy", "true");
  try {
   await refreshCaseSection(sectionKey, target.id);
   target.dataset.lazyLoaded = "1";
   target.dataset.lazyLoadError = "0";
   return true;
  } catch (err) {
   target.dataset.lazyLoadError = "1";
   if (isLazyPanelPlaceholder(target) || !(target.innerHTML || "").trim()) {
    target.innerHTML = `
     <div class="alert alert-warning py-2 px-3 small mb-0 d-flex flex-wrap align-items-center gap-2">
      <span>Section could not load.</span>
      <button type="button" class="btn btn-sm btn-outline-secondary" data-retry-lazy-section="${target.id}">Retry</button>
     </div>
    `;
   }
   return false;
  } finally {
   target.dataset.lazyLoading = "0";
   target.removeAttribute("aria-busy");
  }
 }

 function scheduleLazyPanelFallback(target, attempt = 0) {
  if (!target || !target.hasAttribute("hx-get")) return;
  window.setTimeout(() => {
   if (!document.body.contains(target)) return;
   if (target.dataset.lazyLoaded === "1") return;
   if (target.dataset.lazyLoading === "1") return;

   const htmxPending = target.classList.contains("htmx-request");
   if (htmxPending && attempt < 6) {
    scheduleLazyPanelFallback(target, attempt + 1);
    return;
   }

   if (!isLazyPanelPlaceholder(target) && target.dataset.lazyLoadError !== "1") return;
   loadLazyPanelSection(target, { force: true });
  }, 700);
 }

 function isElementNearViewport(target, margin = 180) {
  if (!target || !document.body.contains(target)) return false;
  const rect = target.getBoundingClientRect();
  if (!rect.width && !rect.height) return false;
  try {
   const style = window.getComputedStyle(target);
   if (style.display === "none" || style.visibility === "hidden") return false;
  } catch (e) {}
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
  return rect.bottom>= -margin && rect.top <= viewportHeight + margin;
 }

 function shouldFallbackLoadLazySection(target) {
  if (!target || !target.hasAttribute("hx-get")) return false;
  if (target.dataset.lazyLoaded === "1") return false;
  if (target.dataset.lazyLoading === "1") return false;
  if (target.classList.contains("htmx-request")) return false;
  if (!isLazyPanelPlaceholder(target) && target.dataset.lazyLoadError !== "1") return false;
  return isElementNearViewport(target);
 }

 function bindLazySectionViewportFallback(target) {
  if (!target || target.dataset.lazyViewportFallbackBound === "1") return;
  if (!inferSectionKeyFromHxGet(target)) return;

  target.dataset.lazyViewportFallbackBound = "1";
  let observer = null;
  let scheduled = false;

  const cleanup = () => {
   if (observer) observer.disconnect();
   window.removeEventListener("scroll", schedule);
   window.removeEventListener("resize", schedule);
  };
  const tryLoad = () => {
   scheduled = false;
   if (!document.body.contains(target)) {
    cleanup();
    return;
   }
   if (!shouldFallbackLoadLazySection(target)) return;
   cleanup();
   loadLazyPanelSection(target, { force: true });
  };
  function schedule() {
   if (scheduled) return;
   scheduled = true;
   if (window.requestAnimationFrame) {
    window.requestAnimationFrame(tryLoad);
   } else {
    window.setTimeout(tryLoad, 50);
   }
  }

  if (window.IntersectionObserver) {
   observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) schedule();
   }, { rootMargin: "180px 0px" });
   observer.observe(target);
  }
  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule);
  window.setTimeout(schedule, 900);
 }

 function setupLazySectionViewportFallbacks(root = document) {
  const targets = [];
  if (root instanceof HTMLElement && root.hasAttribute("hx-get")) {
   targets.push(root);
  }
  const scope = root && typeof root.querySelectorAll === "function" ? root : document;
  scope.querySelectorAll("[hx-get]").forEach((target) => targets.push(target));
  targets.forEach((target) => {
   if (!(target instanceof HTMLElement)) return;
   if (!isLazyPanelPlaceholder(target) && target.dataset.lazyLoadError !== "1") return;
   bindLazySectionViewportFallback(target);
  });
 }

 function showTop(panelId, opts = {}) {
  document.querySelectorAll('.top-panel').forEach(p => p.classList.remove('is-active'));
  const target = document.getElementById(panelId);
  if (target) {
   target.classList.add('is-active');
   setStoredCasePref('topPanel', panelId);
   // Some sections are lazy-loaded via HTMX. Because top-panels start as display:none,
   // `revealed` may not fire reliably until explicit activation on some browsers.
   if (target.hasAttribute("hx-get")) {
    if (window.htmx) {
     try { window.htmx.trigger(target, "case-panel-show"); } catch (e) {}
    }
    scheduleLazyPanelFallback(target);
   }
  }

  document.querySelectorAll('[data-top]').forEach(el => {
   el.classList.toggle('active', (el.dataset.top || '') === panelId);
  });
  document.querySelectorAll('.legacy-tab').forEach(el => {
   el.classList.toggle('active', (el.dataset.top || '') === panelId);
  });

  if (!opts.skipHash) {
   try { window.location.hash = panelId; } catch (e) {}
  }
 }

 function scrollToId(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const stickyOffset = 110;
  const y = Math.max(0, window.scrollY + el.getBoundingClientRect().top - stickyOffset);
  window.scrollTo({ top: y, behavior: 'smooth' });
 }

 function confirmByTypingText(title, expectedText, helpText) {
  return new Promise(function(resolve) {
   try {
    const safeExpected = (expectedText === null || expectedText === undefined)
     ? ''
     : String(expectedText);
    const safeTitle = escapeHtml(String(title ?? ""));
    const safeHelp = escapeHtml(String(helpText ?? ""));
    const safeExpectedHtml = escapeHtml(safeExpected);

    const existing = document.getElementById('caseConfirmOverlay');
    if (existing) {
     existing.remove();
    }

    const overlay = document.createElement('div');
    overlay.id = 'caseConfirmOverlay';
    overlay.style.position = 'fixed';
    overlay.style.inset = '0';
    overlay.style.background = 'rgba(0,0,0,0.35)';
    overlay.style.zIndex = '9999';
    overlay.style.display = 'flex';
    overlay.style.alignItems = 'center';
    overlay.style.justifyContent = 'center';

    const panel = document.createElement('div');
    panel.style.background = '#ffffff';
    panel.style.borderRadius = '12px';
    panel.style.padding = '16px';
    panel.style.maxWidth = '520px';
    panel.style.width = '92%';
    panel.style.boxShadow = '0 10px 30px rgba(0,0,0,0.2)';
    panel.style.fontSize = '14px';

    panel.innerHTML = `
     <h3 style="margin:0 0 8px 0; font-size:16px;">${safeTitle}</h3>
     <p style="margin:0 0 10px 0; color:#b91c1c; font-weight:600;"> Actions  none.</p>
     <p style="margin:0 0 8px 0; color:#4b5563;">${safeHelp}</p>
     <div style="border:1px solid #e5e7eb; border-radius:6px; padding:8px; background:#f9fafb; margin-bottom:8px; white-space:pre-wrap; font-family:monospace; font-size:13px;">${safeExpectedHtml}</div>
     <input type="text" id="caseConfirmInput" style="width:100%; box-sizing:border-box; margin-bottom:6px; padding:6px 8px; border-radius:8px; border:1px solid #d1d5db;" placeholder="  Input/Paste" />
     <div id="caseConfirmError" style="display:none; margin:0 0 10px 0; color:#b91c1c; font-size:13px;"></div>
     <div style="display:flex; justify-content:flex-end; gap:8px;">
      <button type="button" id="caseConfirmCancelBtn" class="btn btn-sm btn-outline-secondary">Cancel</button>
      <button type="button" id="caseConfirmOkBtn" class="btn btn-sm btn-danger">Delete</button>
     </div>
    `;

    overlay.appendChild(panel);
    document.body.appendChild(overlay);

    const input = document.getElementById('caseConfirmInput');
    if (input) {
     input.value = '';
     input.focus();
     input.addEventListener('input', function () {
      const errEl = document.getElementById('caseConfirmError');
      if (errEl) errEl.style.display = 'none';
     });
    }

    const cleanup = function(result) {
     try {
      overlay.remove();
     } catch (e) {}
     resolve(result);
    };

    document.getElementById('caseConfirmCancelBtn').addEventListener('click', function(ev) {
     ev.preventDefault();
     cleanup(false);
    });
    document.getElementById('caseConfirmOkBtn').addEventListener('click', function(ev) {
     ev.preventDefault();
     const v = (input && input.value ? input.value : '').trim();
     if (v !== safeExpected) {
      const errEl = document.getElementById('caseConfirmError');
      if (errEl) {
       errEl.textContent = 'The entered value does not match. Paste or type the expected value.';
       errEl.style.display = 'block';
      }
      if (input) {
       input.focus();
       input.select();
      }
      return;
     }
     cleanup(true);
    });

    // Close only via explicit buttons to avoid instant dismiss on the same click event.
   } catch (e) {
    resolve(false);
   }
  });
 }

 async function confirmDeleteMatter(ref) {
  const ok = await confirmByTypingText(
   '⚠️ Matter Delete ( )',
   ref,
   'Delete Open below Matter reference Input( /Paste).'
  );
  if (!ok) return;

  const form = document.createElement('form');
  form.method = 'POST';
  form.action = CASE_VIEW_CONFIG.deleteMatterUrl || form.action;

  // Add CSRF token
  const csrfInput = document.createElement('input');
  csrfInput.type = 'hidden';
  csrfInput.name = 'csrf_token';
  csrfInput.value = CASE_VIEW_CONFIG.csrfToken || "";
  form.appendChild(csrfInput);

  document.body.appendChild(form);
  form.submit();
 }

 async function confirmDeleteHistory(type, docName, formElement) {
  if (!docName) docName = '(Document name )';

  const typeLabel = type === 'notice' ? '' : '';
  const ok = await confirmByTypingText(
   `⚠️ ${typeLabel} Delete ( )`,
   docName,
   `Delete Open below Document name Input( /Paste).`
  );

  if (ok && formElement) {
   formElement.submit();
  }
 }

 async function promptLink(ipmInvId) {
  const val = await ipmPrompt("Link Invoice ID() Invoice number(INV-...) enter:", "");
  if (!val) return;
  const raw = val.trim();
  if (!raw) return;
  const input = document.getElementById("ext-id-input-" + ipmInvId);
  const form = document.getElementById("link-form-" + ipmInvId);
  if (input && form) {
   input.value = raw;
   form.submit();
  }
 }

 const AUDIT_STATE = {
  items: [],
  filtered: [],
  itemMap: {},
  lastLoadedAt: null,
 };

 const CASE_WORKSPACE_STATE = {
  sections: [],
  collapsed: {},
  workflowClosedExpanded: {},
 };

 function setupRegistryImageDropzones() {
  if (!CASE_VIEW.canEditCase) return;
  document.querySelectorAll('[data-registry-image-dropzone="1"]').forEach((zone) => {
   if (zone.dataset.registryImageBound === "1") return;
   const fileInput = zone.querySelector('[data-registry-image-file="1"]');
   const preview = zone.querySelector('[data-registry-image-preview="1"]');
   const selectBtn = zone.querySelector('[data-registry-image-select="1"]');
   const clearBtn = zone.querySelector('[data-registry-image-clear="1"]');
   if (!fileInput || !preview) return;

   zone.dataset.registryImageBound = "1";
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

   function renderExisting() {
    clearObjectUrl();
    preview.innerHTML = '';
    const url = (zone.dataset.currentUrl || '').trim();
    const dl = (zone.dataset.currentDownload || '').trim();
    const name = (zone.dataset.currentName || '').trim();
    if (!url && !name) {
     renderPlaceholder('Drag and drop here or click');
     return;
    }
    if (url) {
     const link = document.createElement('a');
     const href = safeUrl(dl || url);
     link.href = href || "#";
     link.target = '_blank';
     link.rel = 'noopener';
     link.textContent = name || 'Image';
     preview.appendChild(link);
     const img = document.createElement('img');
     const imgSrc = safeUrl(url);
     if (!imgSrc) return;
     img.src = imgSrc;
     img.alt = '/Image';
     img.className = 'img-fluid rounded border';
     img.style.maxHeight = '220px';
     preview.appendChild(img);
    } else {
     renderPlaceholder(`Current value: ${name}`);
    }
   }

   function renderFile(file) {
    clearObjectUrl();
    preview.innerHTML = '';
    if (!file) {
     renderExisting();
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
    img.style.maxHeight = '220px';
    preview.appendChild(img);
    const name = document.createElement('div');
    name.className = 'text-muted small mt-1';
    name.textContent = file.name || '';
    preview.appendChild(name);
   }

   async function uploadFile(file) {
    if (!file) return;
    const formData = new FormData();
    formData.append('image_file', file);
    try {
     const data = await apiForm(`/api/cases/${CASE_VIEW.caseId}/registry-image`, formData, 'POST');
     zone.dataset.currentId = data.image || '';
     zone.dataset.currentUrl = data.preview_url || '';
     zone.dataset.currentDownload = data.download_url || '';
     zone.dataset.currentName = data.original_name || '';
     renderExisting();
     showToast('Image Saved', 'success');
    } catch (err) {
     renderExisting();
     showToast(err.message || 'Image Upload failed', 'danger');
    }
   }

   async function clearImage() {
    const formData = new FormData();
    formData.append('clear', '1');
    try {
     await apiForm(`/api/cases/${CASE_VIEW.caseId}/registry-image`, formData, 'POST');
     zone.dataset.currentId = '';
     zone.dataset.currentUrl = '';
     zone.dataset.currentDownload = '';
     zone.dataset.currentName = '';
     renderPlaceholder('Drag and drop here or click');
     showToast('Image Delete', 'success');
    } catch (err) {
     showToast(err.message || 'Image Delete ', 'danger');
     renderExisting();
    }
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
    renderFile(files[0]);
    uploadFile(files[0]);
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
     clearImage();
    });
   }

   zone.addEventListener('click', (e) => {
    if (e.target.closest('button') || e.target === fileInput) return;
    fileInput.click();
   });

   fileInput.addEventListener('change', () => {
    const files = fileInput.files;
    if (!files || !files.length) return;
    pickFiles(files);
   });

   zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('is-dragover');
   });
   zone.addEventListener('dragleave', () => {
    zone.classList.remove('is-dragover');
   });
   zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('is-dragover');
    const files = e.dataTransfer ? e.dataTransfer.files : null;
    pickFiles(files);
   });
  });
 }

 function setupMemoAttachmentDropzones() {
  if (!CASE_VIEW.canEditCase) return;
  document.querySelectorAll('[data-memo-attachment-dropzone="1"]').forEach((zone) => {
   if (zone.dataset.memoAttachmentBound === "1") return;

   const input = zone.querySelector('[data-memo-attachment-input="1"]');
   const surface = zone.querySelector('[data-memo-attachment-surface="1"]');
   const filesEl = zone.querySelector('[data-memo-attachment-files="1"]');
   const clearBtn = zone.querySelector('[data-memo-attachment-clear="1"]');
   if (!input || !surface) return;

   zone.dataset.memoAttachmentBound = "1";
   let dragDepth = 0;

   const getFiles = (fileList) => Array.from(fileList || []).filter(Boolean);
   const fileKey = (file) => [
    file.name || "",
    file.size || 0,
    file.lastModified || 0,
   ].join("\u0000");

   const formatBytes = (bytes) => {
    const size = Number(bytes || 0);
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
    return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
   };

   const renderFiles = () => {
    if (!filesEl) return;
    const files = getFiles(input.files);
    filesEl.innerHTML = "";
    filesEl.classList.toggle("text-muted", files.length === 0);
    if (clearBtn) clearBtn.hidden = files.length === 0;

    if (!files.length) {
     filesEl.textContent = "Select File ";
     return;
    }

    const list = document.createElement("div");
    list.className = "case-memo-attachment-dropzone__file-list";
    files.slice(0, 8).forEach((file) => {
     const item = document.createElement("span");
     item.className = "case-memo-attachment-dropzone__file-pill";
     item.textContent = `${file.name || "File"} (${formatBytes(file.size)})`;
     list.appendChild(item);
    });
    if (files.length> 8) {
     const more = document.createElement("span");
     more.className = "case-memo-attachment-dropzone__file-more";
     more.textContent = ` ${files.length - 8}items`;
     list.appendChild(more);
    }
    filesEl.appendChild(list);
   };

   const setInputFiles = (files, { append = false } = {}) => {
    const incoming = getFiles(files);
    if (!incoming.length) return false;
    const existing = append ? getFiles(input.files) : [];
    try {
     const dt = new DataTransfer();
     const seen = new Set();
     existing.concat(incoming).forEach((file) => {
      const key = fileKey(file);
      if (seen.has(key)) return;
      seen.add(key);
      dt.items.add(file);
     });
     input.files = dt.files;
     renderFiles();
     return true;
    } catch (err) {
     try {
      input.files = files;
      renderFiles();
      return true;
     } catch (fallbackErr) {
      showToast(" File  none. Select file  .", "danger");
      return false;
     }
    }
   };

   const activate = () => {
    zone.classList.add("is-dragover");
   };
   const deactivate = () => {
    dragDepth = 0;
    zone.classList.remove("is-dragover");
   };

   surface.addEventListener("click", () => {
    input.click();
   });

   surface.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    input.click();
   });

   input.addEventListener("change", renderFiles);

   if (clearBtn) {
    clearBtn.addEventListener("click", () => {
     input.value = "";
     renderFiles();
    });
   }

   ["dragenter", "dragover", "dragleave", "drop"].forEach((eventName) => {
    zone.addEventListener(eventName, (e) => {
     e.preventDefault();
     e.stopPropagation();
    });
   });

   zone.addEventListener("dragenter", () => {
    dragDepth += 1;
    activate();
   });

   zone.addEventListener("dragover", (e) => {
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    activate();
   });

   zone.addEventListener("dragleave", () => {
    dragDepth -= 1;
    if (dragDepth <= 0) deactivate();
   });

   zone.addEventListener("drop", (e) => {
    deactivate();
    const droppedFiles = e.dataTransfer ? e.dataTransfer.files : null;
    setInputFiles(droppedFiles, { append: getFiles(input.files).length> 0 });
   });

   renderFiles();
  });
 }

 function updateSummaryUI(summary) {
  if (!summary) return;
  const titleEl = document.getElementById("summaryTitleText");
  if (titleEl) titleEl.textContent = summary.title || " ";
  const divBadge = document.getElementById("summaryDivisionBadge");
  if (divBadge) divBadge.textContent = summary.display_division || summary.division || "DOM";
  const typeBadge = document.getElementById("summaryTypeBadge");
  if (typeBadge) typeBadge.textContent = summary.display_type || summary.type || "PATENT";
  const statusBadge = document.getElementById("summaryStatusBadge");
  if (statusBadge) statusBadge.textContent = summary.status || "";
  const focusValue = document.getElementById("summaryFocusValue");
  if (focusValue && (focusValue.dataset.summaryFocusMode || "") === "status") {
   focusValue.textContent = summary.status || "Internal status ";
  }

  const nextEl = document.getElementById("summaryNextDeadline");
  const nextCal = document.getElementById("summaryNextDeadlineCalendar");
  if (nextEl) {
   if (summary.next_deadline && summary.next_deadline.date) {
    const dday = formatDDay(summary.next_deadline.d_day);
    const label = summary.next_deadline.label || "";
    const parts = [escapeHtml(summary.next_deadline.date)];
    if (dday) parts.push(`<span class="sub">${escapeHtml(dday)}</span>`);
    if (label) parts.push(`<span class="sub">${escapeHtml(label)}</span>`);
    nextEl.innerHTML = parts.join(" ");

    if (summary.next_deadline.url) {
     nextEl.href = summary.next_deadline.url;
     nextEl.removeAttribute("aria-disabled");
    } else {
     nextEl.href = "#";
     nextEl.setAttribute("aria-disabled", "true");
    }
   } else {
    nextEl.textContent = " ";
    nextEl.href = "#";
    nextEl.setAttribute("aria-disabled", "true");
   }
  }

  if (nextCal) {
   const calUrl =
    summary.next_deadline && (summary.next_deadline.calendar_url || summary.next_deadline.date)
     ? (summary.next_deadline.calendar_url || `/deadline/calendar/month?date=${encodeURIComponent(summary.next_deadline.date)}`)
     : "";
   if (calUrl) {
    nextCal.href = calUrl;
    nextCal.style.display = "";
    nextCal.removeAttribute("aria-disabled");
   } else {
    nextCal.href = "#";
    nextCal.style.display = "none";
    nextCal.setAttribute("aria-disabled", "true");
   }
  }
  const openCnt = document.getElementById("summaryOpenDeadlineCount");
  if (openCnt) openCnt.textContent = summary.open_deadline_count || 0;

  const outstanding = document.getElementById("summaryOutstanding");
  if (outstanding && summary.invoice) {
   outstanding.textContent = formatMoney(summary.invoice.outstanding || 0, summary.invoice.currency || "USD");
  }

  const lastEl = document.getElementById("summaryLastActivity");
  if (lastEl) lastEl.textContent = formatDateTime(summary.last_activity_at);
 }

 async function refreshSummary() {
  if (!CASE_VIEW.caseId) return;
  try {
   const summary = await apiJson(`/api/cases/${CASE_VIEW.caseId}/summary`);
   updateSummaryUI(summary);
  } catch (e) {
   showToast(e.message || " Updated ", "danger");
  }
 }

 function auditActionLabel(action) {
  const key = (action || "").toUpperCase();
  return AUDIT_ACTION_LABELS[key] || key || "-";
 }

 function auditActionClass(action) {
  const key = (action || "").toUpperCase();
  return AUDIT_ACTION_LABELS[key] ? `audit-action-${key.toLowerCase()}` : "audit-action-default";
 }

 function auditFieldLabel(field) {
  if (!field) return "-";
  if (AUDIT_FIELD_LABELS[field]) return AUDIT_FIELD_LABELS[field];
  if (field.startsWith("custom_text.")) {
   const ns = field.split(".")[1] || "";
   const label = CUSTOM_TEXT_LABELS[ns] || ns || " ";
   return `${label} Edit`;
  }
  if (field.startsWith("memo.")) return "Notes";
  if (field.startsWith("progress.")) return "Progress";
  if (field.startsWith("history.notice.")) return "Office correspondence";
  if (field.startsWith("fm.")) return "File management";
  return field;
 }

 function displayStaffName(id) {
  const key = String(id ?? "");
  if (key && STAFF_USER_MAP[key]) return STAFF_USER_MAP[key];
  if (!key) return "-";
  return `User #${key}`;
 }

 function displayClientName(id) {
  if (!id) return "-";
  return `Client #${id}`;
 }

 function resolveInlineDisplayValue(field, rawValue, displayOverride) {
  if (displayOverride !== null && typeof displayOverride !== "undefined" && displayOverride !== "") {
   return displayOverride;
  }
  if (field === "assignee_id") return displayStaffName(rawValue);
  if (field === "client_id") return displayClientName(rawValue);
  return rawValue === null || typeof rawValue === "undefined" ? "" : String(rawValue);
 }

 function formatAuditLines(lines) {
  const safeLines = (lines || [])
   .map((line) => (line === null || typeof line === "undefined" ? "" : String(line)))
   .map((line) => line.trim())
   .filter((line) => line.length> 0);
  if (!safeLines.length) return '<span class="text-muted">-</span>';
  return safeLines.map((line) => `<div>${escapeHtml(line)}</div>`).join("");
 }

 function formatAuditChange(oldVal, newVal) {
  const oldText = formatAuditValue(oldVal);
  const newText = formatAuditValue(newVal);
  if (oldText === "-" && newText === "-") return '<span class="text-muted">-</span>';
  if (oldText === "-" && newText !== "-") {
   return formatAuditLines([`Change: ${newText}`]);
  }
  if (newText === "-" && oldText !== "-") {
   return formatAuditLines([`Previous: ${oldText}`]);
  }
  return `
   <div class="audit-change"><span class="audit-change-label">Previous</span>${escapeHtml(oldText)}</div>
   <div class="audit-change"><span class="audit-change-label">Change</span>${escapeHtml(newText)}</div>
  `;
 }

 function buildAuditDetail(row) {
  const field = row.field || "";
  const oldDisplay = row.old_display;
  const newDisplay = row.new_display;
  const oldVal = resolveInlineDisplayValue(field, row.old_value, oldDisplay);
  const newVal = resolveInlineDisplayValue(field, row.new_value, newDisplay);

  if (["title", "status", "our_ref", "your_ref", "assignee_id", "client_id"].includes(field)) {
   return formatAuditChange(oldVal, newVal);
  }

  if (field === "status.inhouse_status") {
   const oldStatus = row.old_value && row.old_value.inhouse_status ? row.old_value.inhouse_status : row.old_value;
   const newStatus = row.new_value && row.new_value.inhouse_status ? row.new_value.inhouse_status : row.new_value;
   return formatAuditChange(oldStatus, newStatus);
  }

  if (field.startsWith("memo.")) {
   const preview = (row.new_value && row.new_value.preview) || (row.old_value && row.old_value.preview) || "";
   const len = row.new_value && row.new_value.len ? row.new_value.len : null;
   const lines = [];
   if (preview) lines.push(`Content: ${preview}`);
   if (len) lines.push(`Length: ${len}`);
   if (!lines.length) lines.push(formatAuditValue(row.new_value || row.old_value));
   return formatAuditLines(lines);
  }

  if (field.startsWith("progress.")) {
   const preview = (row.new_value && row.new_value.preview) || (row.old_value && row.old_value.preview) || "";
   const lines = [];
   if (preview) lines.push(`Content: ${preview}`);
   if (!lines.length) lines.push(formatAuditValue(row.new_value || row.old_value));
   return formatAuditLines(lines);
  }

  if (field.startsWith("custom_text.")) {
   const oldText = row.old_value && row.old_value.text ? row.old_value.text : row.old_value;
   const newText = row.new_value && row.new_value.text ? row.new_value.text : row.new_value;
   return formatAuditChange(oldText, newText);
  }

  if (field.startsWith("history.notice.")) {
   const payload = row.new_value || row.old_value || {};
   const lines = [];
   if (payload.doc_name) lines.push(`Document name: ${payload.doc_name}`);
   if (payload.due_date) lines.push(`Due date: ${payload.due_date}`);
   if (Array.isArray(payload.add_files) && payload.add_files.length) {
    lines.push(`Added files: ${payload.add_files.filter(Boolean).join(", ")}`);
   }
   if (Array.isArray(payload.remove_files) && payload.remove_files.length) {
    lines.push(`Removed files: ${payload.remove_files.filter(Boolean).join(", ")}`);
   }
   if (Array.isArray(payload.files) && payload.files.length) {
    lines.push(`File: ${payload.files.filter(Boolean).join(", ")}`);
   }
   if (!lines.length) lines.push(formatAuditValue(payload));
   return formatAuditLines(lines);
  }

  if (field === "deadline.add") {
   const payload = row.new_value || {};
   const assigneeName = payload.assignee_id ? displayStaffName(payload.assignee_id) : "-";
   const lines = [];
   if (payload.label || payload.due_date) {
    lines.push(`${payload.label || "Deadline"} / ${payload.due_date || "-"}`);
   }
   lines.push(`Responsible: ${assigneeName}`);
   if (payload.priority) lines.push(`Priority: ${payload.priority}`);
   return formatAuditLines(lines);
  }

  if (field === "file.doc_type") {
   const oldDoc = DOC_TYPE_LABELS[row.old_value] || row.old_value;
   const newDoc = DOC_TYPE_LABELS[row.new_value] || row.new_value;
   return formatAuditChange(oldDoc, newDoc);
  }

  if (field.startsWith("fm.")) {
   const payload = row.new_value || row.old_value || {};
   const lines = [];
   if (field === "fm.upload") {
    if (payload.filename) lines.push(`File: ${payload.filename}`);
    const docLabel = DOC_TYPE_LABELS[payload.doc_type] || payload.doc_type;
    if (docLabel) lines.push(`Type: ${docLabel}`);
    if (payload.category) lines.push(`Type: ${payload.category}`);
    if (payload.parent_id) lines.push(`Folder: ${payload.parent_id}`);
   } else if (field === "fm.folder.create") {
    if (payload.folder_name) lines.push(`Folder: ${payload.folder_name}`);
    if (payload.parent_id) lines.push(`Parent folder: ${payload.parent_id}`);
   } else if (field === "fm.move") {
    const oldParent = row.old_value && row.old_value.parent_id ? row.old_value.parent_id : "-";
    const newParent = row.new_value && row.new_value.parent_id ? row.new_value.parent_id : "-";
    const itemId = payload.item_id || (row.old_value && row.old_value.item_id) || "-";
    lines.push(`Item: ${itemId}`);
    lines.push(`Location: ${oldParent} → ${newParent}`);
   } else if (field === "fm.delete") {
    if (payload.role) lines.push(`Type: ${payload.role}`);
    if (payload.file_asset_id) lines.push(`File ID: ${payload.file_asset_id}`);
    if (payload.parent_id) lines.push(`Folder: ${payload.parent_id}`);
   }
   if (!lines.length) lines.push(formatAuditValue(payload));
   return formatAuditLines(lines);
  }

  if (field === "registry_image") {
   return formatAuditChange(row.old_value, row.new_value);
  }

  if (field === "matter.delete") {
   const payload = row.old_value || {};
   const lines = [];
   if (payload.our_ref) lines.push(`Matter reference: ${payload.our_ref}`);
   if (payload.matter_type) lines.push(`: ${payload.matter_type}`);
   lines.push("Deleted");
   return formatAuditLines(lines);
  }

  if (field === "uspto_upload") {
   const payload = row.new_value || {};
   const lines = [];
   if (payload.doc_name) lines.push(`Document name: ${payload.doc_name}`);
   if (payload.due_date) lines.push(`Due date: ${payload.due_date}`);
   if (payload.file_asset_path) lines.push(`File: ${payload.file_asset_path}`);
   if (!lines.length) lines.push(formatAuditValue(payload));
   return formatAuditLines(lines);
  }

  return formatAuditChange(row.old_value, row.new_value);
 }

 function buildAuditSearchText(row) {
  const parts = [
   row.actor_name,
   row.actor,
   row.action,
   auditActionLabel(row.action),
   row.field,
   auditFieldLabel(row.field),
   row.old_display,
   row.new_display,
   stringifyAuditValue(row.old_value),
   stringifyAuditValue(row.new_value),
   row.request_id,
  ];
  return parts.filter(Boolean).join(" ").toLowerCase();
 }

 function renderAuditRows(items) {
  const tbody = document.getElementById("caseAuditList");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!items.length) {
   tbody.innerHTML = '<tr data-empty="1"><td colspan="5" class="text-center py-4">Change history none.</td></tr>';
   return;
  }
  items.forEach((row) => {
   const tr = document.createElement("tr");
   const actionKey = String(row.action || "").toLowerCase();
   tr.className = actionKey ? `audit-row audit-row-${actionKey}` : "audit-row";
  if (row.created_at) {
   const created = parseIsoDate(row.created_at);
   if (created && !Number.isNaN(created.getTime())) {
    const diffMs = Date.now() - created.getTime();
    if (diffMs>= 0 && diffMs <= 5 * 60 * 1000) {
     tr.classList.add("audit-row-recent");
    }
   }
  }
   const actionLabel = auditActionLabel(row.action);
   const actionClass = auditActionClass(row.action);
   const fieldLabel = auditFieldLabel(row.field);
   const fieldMeta =
    row.field && fieldLabel && fieldLabel !== row.field
     ? `<span class="audit-field-meta">${escapeHtml(row.field)}</span>`
     : "";
   const typeHtml = `
    <div class="audit-type">
     <span class="badge audit-action ${actionClass}">${escapeHtml(actionLabel)}</span>
     <span class="audit-field">${escapeHtml(fieldLabel || "-")}</span>
     ${fieldMeta}
    </div>
   `;
   const actorLabel = row.actor_name || row.actor || "-";
   const detailHtml = buildAuditDetail(row);
   const requestHtml = row.request_id
    ? `<div class="audit-request text-muted">req: ${escapeHtml(row.request_id)}</div>`
    : "";
   const undoHtml = row.undo_allowed
    ? `
     <button type="button" class="btn btn-sm btn-outline-secondary" data-audit-undo="${row.id}">Undo</button>
     ${
      row.undo_expires_in
       ? `<div class="audit-undo-meta text-muted">${row.undo_expires_in}s</div>`
       : ""
     }
    `
    : '<span class="text-muted small">-</span>';
   const timeLabel = formatDateTime(row.created_at);
   const relativeLabel = formatRelativeTime(row.created_at);
   tr.innerHTML = `
    <td align="center">
     <div class="audit-time">${escapeHtml(timeLabel)}</div>
     ${relativeLabel ? `<div class="audit-time-rel">${escapeHtml(relativeLabel)}</div>` : ""}
    </td>
    <td align="center">
     <div class="audit-actor">${escapeHtml(actorLabel)}</div>
     ${row.actor_type ? `<div class="audit-actor-type">${escapeHtml(row.actor_type)}</div>` : ""}
    </td>
    <td>${typeHtml}</td>
    <td class="audit-detail">${detailHtml}${requestHtml}</td>
    <td align="center">${undoHtml}</td>
   `;
   tbody.appendChild(tr);
  });
 }

 function updateAuditMeta(totalCount, visibleCount, query) {
  const countEl = document.getElementById("caseAuditCount");
  if (countEl) {
   if (totalCount === visibleCount) {
    countEl.textContent = `Recent ${totalCount}items`;
   } else {
    countEl.textContent = `Recent ${totalCount}items / Display ${visibleCount}items`;
   }
  }
  const metaEl = document.getElementById("caseAuditMeta");
  if (metaEl) {
   const parts = [];
   if (AUDIT_STATE.lastLoadedAt) {
    parts.push(`Updated: ${formatDateTime(AUDIT_STATE.lastLoadedAt)}`);
   }
   if (query) parts.push(`Search: "${query}"`);
   metaEl.textContent = parts.join(" · ");
  }
 }

 function updateAuditFilters(items) {
  const actionEl = document.getElementById("caseAuditActionFilter");
  const fieldEl = document.getElementById("caseAuditFieldFilter");
  if (!actionEl && !fieldEl) return;

  const selectedAction = actionEl ? actionEl.value : "all";
  const selectedField = fieldEl ? fieldEl.value : "all";

  const actions = new Set();
  const fields = new Set();
  items.forEach((item) => {
   if (item.action) actions.add(String(item.action).toUpperCase());
   if (item.field) fields.add(String(item.field));
  });

  if (actionEl) {
   const ordered = [];
   AUDIT_ACTION_ORDER.forEach((act) => {
    if (actions.has(act)) {
     ordered.push(act);
     actions.delete(act);
    }
   });
   Array.from(actions)
    .sort()
    .forEach((act) => ordered.push(act));
   actionEl.innerHTML = "";
   const allOpt = document.createElement("option");
   allOpt.value = "all";
   allOpt.textContent = "All Actions";
   actionEl.appendChild(allOpt);
   ordered.forEach((act) => {
    const opt = document.createElement("option");
    opt.value = act;
    opt.textContent = auditActionLabel(act);
    actionEl.appendChild(opt);
   });
   if (ordered.includes(selectedAction)) {
    actionEl.value = selectedAction;
   }
  }

  if (fieldEl) {
   const sortedFields = Array.from(fields).sort((a, b) => {
    const aLabel = auditFieldLabel(a);
    const bLabel = auditFieldLabel(b);
    return aLabel.localeCompare(bLabel);
   });
   fieldEl.innerHTML = "";
   const allOpt = document.createElement("option");
   allOpt.value = "all";
   allOpt.textContent = "All Item";
   fieldEl.appendChild(allOpt);
   sortedFields.forEach((field) => {
    const opt = document.createElement("option");
    const label = auditFieldLabel(field);
    opt.value = field;
    opt.textContent = label && label !== field ? `${label} (${field})` : field;
    fieldEl.appendChild(opt);
   });
   if (sortedFields.includes(selectedField)) {
    fieldEl.value = selectedField;
   }
  }
 }

 function updateAuditChips(items, selectedAction) {
  const host = document.getElementById("caseAuditChips");
  if (!host) return;
  const counts = {};
  (items || []).forEach((item) => {
   const act = String(item.action || "").toUpperCase() || "ETC";
   counts[act] = (counts[act] || 0) + 1;
  });
  const ordered = [];
  AUDIT_ACTION_ORDER.forEach((act) => {
   if (counts[act]) ordered.push(act);
  });
  Object.keys(counts)
   .filter((act) => !ordered.includes(act))
   .sort()
   .forEach((act) => ordered.push(act));

  host.innerHTML = "";
  const total = (items || []).length;
  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = "audit-chip";
  allBtn.dataset.auditChip = "all";
  allBtn.textContent = `All ${total}`;
  if (!selectedAction || selectedAction === "ALL") allBtn.classList.add("active");
  host.appendChild(allBtn);

  ordered.forEach((act) => {
   const btn = document.createElement("button");
   btn.type = "button";
   btn.className = `audit-chip ${auditActionClass(act)}`;
   btn.dataset.auditChip = act;
   btn.textContent = `${auditActionLabel(act)} ${counts[act]}`;
   if (selectedAction === act) btn.classList.add("active");
   host.appendChild(btn);
  });
 }

 function applyAuditFilters() {
  const actionEl = document.getElementById("caseAuditActionFilter");
  const fieldEl = document.getElementById("caseAuditFieldFilter");
  const searchEl = document.getElementById("caseAuditSearch");
  const actionFilter = (actionEl?.value || "all").toUpperCase();
  const fieldFilter = fieldEl?.value || "all";
  const query = (searchEl?.value || "").trim().toLowerCase();

  updateAuditChips(AUDIT_STATE.items, actionFilter);

  const filtered = (AUDIT_STATE.items || []).filter((item) => {
   const itemAction = (item.action || "").toUpperCase();
   if (actionFilter !== "ALL" && itemAction !== actionFilter) return false;
   if (fieldFilter !== "all" && item.field !== fieldFilter) return false;
   if (query) {
    const text = buildAuditSearchText(item);
    if (!text.includes(query)) return false;
   }
   return true;
  });

  AUDIT_STATE.filtered = filtered;
  renderAuditRows(filtered);
  updateAuditMeta(AUDIT_STATE.items.length, filtered.length, query);
 }

 function getAuditLimit() {
  const limitEl = document.getElementById("caseAuditLimit");
  const raw = limitEl ? parseInt(limitEl.value, 10) : 5;
  if (!raw || Number.isNaN(raw)) return 5;
  return Math.max(1, Math.min(raw, 200));
 }

 async function refreshAuditLog() {
  if (!CASE_VIEW.caseId) return;
  const tbody = document.getElementById("caseAuditList");
  if (!tbody) return;
  const limit = getAuditLimit();
  try {
   const data = await apiJson(`/api/cases/${CASE_VIEW.caseId}/audits?limit=${limit}`);
   const items = Array.isArray(data.items) ? data.items : [];
   AUDIT_STATE.items = items;
   AUDIT_STATE.itemMap = {};
   items.forEach((item) => {
    if (item && item.id !== null && typeof item.id !== "undefined") {
     AUDIT_STATE.itemMap[String(item.id)] = item;
    }
   });
   AUDIT_STATE.lastLoadedAt = new Date().toISOString();
   updateAuditFilters(items);
   applyAuditFilters();
  } catch (e) {
   tbody.innerHTML = '<tr data-empty="1"><td colspan="5" class="text-center py-4 text-danger">Change history </td></tr>';
   const metaEl = document.getElementById("caseAuditMeta");
   if (metaEl) metaEl.textContent = "Change history ";
  }
 }

 function setupAuditFilters() {
  const auditRoot = document.getElementById("caseAuditSection") || document.getElementById("sec-audits");
  if (auditRoot && auditRoot.dataset.auditFiltersBound === "1") return;
  const actionEl = document.getElementById("caseAuditActionFilter");
  const fieldEl = document.getElementById("caseAuditFieldFilter");
  const searchEl = document.getElementById("caseAuditSearch");
  const limitEl = document.getElementById("caseAuditLimit");
  const resetBtn = document.getElementById("resetAuditFiltersBtn");
  const chipsHost = document.getElementById("caseAuditChips");
  if (auditRoot) auditRoot.dataset.auditFiltersBound = "1";
  if (actionEl) actionEl.addEventListener("change", applyAuditFilters);
  if (fieldEl) fieldEl.addEventListener("change", applyAuditFilters);
  if (searchEl) searchEl.addEventListener("input", applyAuditFilters);
  if (limitEl) limitEl.addEventListener("change", refreshAuditLog);
  if (resetBtn) {
   resetBtn.addEventListener("click", () => {
    if (actionEl) actionEl.value = "all";
    if (fieldEl) fieldEl.value = "all";
    if (searchEl) searchEl.value = "";
    applyAuditFilters();
   });
  }
  if (chipsHost && actionEl) {
   chipsHost.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-audit-chip]");
    if (!btn) return;
    const action = (btn.dataset.auditChip || "all").toLowerCase();
    actionEl.value = action;
    applyAuditFilters();
   });
  }
 }

 function setupAuditUndoHandler() {
  const tbody = document.getElementById("caseAuditList");
  if (!tbody) return;
  if (tbody.dataset.auditUndoBound === "1") return;
  tbody.dataset.auditUndoBound = "1";
  tbody.addEventListener("click", async (e) => {
   const btn = e.target.closest("[data-audit-undo]");
   if (!btn) return;
   const auditId = btn.dataset.auditUndo;
   if (!auditId) return;
   const ok = await ipmConfirm("Change ?");
   if (!ok) return;
   btn.disabled = true;
   try {
    await apiJson(`/api/case_audits/${auditId}/undo`, "POST");
    const item = AUDIT_STATE.itemMap[String(auditId)];
    if (item) {
     const displayValue = resolveInlineDisplayValue(
      item.field,
      item.old_value,
      item.old_display
     );
     applyInlineDisplay(item.field, displayValue, item.old_value);
    }
    refreshSummary();
    refreshAuditLog();
    showToast("Undo Done", "success");
   } catch (err) {
    showToast(err.message || "Undo ", "danger");
   } finally {
    btn.disabled = false;
   }
  });
 }

 function applyClientDisplay(displayEl, clientId, clientName) {
  const base = displayEl?.dataset?.clientUrlBase || "";
  if (clientId && clientName && base) {
   const url = base.replace(/\/0(?=\/|$|\?|#)/, `/${encodeURIComponent(String(clientId))}`);
   if (displayEl) {
    displayEl.innerHTML = "";
    const link = document.createElement("a");
    link.href = url;
    link.textContent = clientName;
    displayEl.appendChild(link);
   }
  } else if (displayEl) {
   displayEl.textContent = clientName || "-";
  }
 }

 function applyAssigneeDisplay(displayEl, assigneeId, assigneeName) {
  const base = displayEl?.dataset?.worklogUrl || "";
  if (assigneeId && base) {
   let href = "";
   try {
    const url = new URL(base, window.location.origin);
    url.searchParams.set("owner_role", "attorney");
    url.searchParams.set("owner", String(assigneeId));
    url.searchParams.set("filter", "todo");
    href = `${url.pathname}${url.search}${url.hash}`;
   } catch (e) {
    href = `${base}?owner_role=attorney&owner=${encodeURIComponent(String(assigneeId))}&filter=todo`;
   }

   displayEl.innerHTML = "";
   const link = document.createElement("a");
   link.href = href;
   link.textContent = assigneeName || "-";
   displayEl.appendChild(link);
   return;
  }

  if (displayEl) {
   displayEl.textContent = assigneeName || "-";
  }
 }

 function applyInlineDisplay(field, displayValue, rawValue) {
  if (field === "title") {
   const titleEl = document.getElementById("summaryTitleText");
   if (titleEl) titleEl.textContent = displayValue || " ";
   const proposal = document.getElementById("caseProposalTitle");
   if (proposal) proposal.textContent = displayValue || "";
   const headerTitle = document.getElementById("caseHeaderTitle");
   if (headerTitle) headerTitle.textContent = `(${displayValue || ""})`;
   return;
  }
  if (field === "our_ref") {
   const headerRef = document.getElementById("caseHeaderOurRef");
   if (headerRef) headerRef.textContent = displayValue || "";
   const tableRef = document.getElementById("caseOurRefDisplay");
   if (tableRef) tableRef.textContent = displayValue || "";
   return;
  }
  if (field === "your_ref") {
   const el = document.getElementById("inlineYourRefDisplay");
   if (el) el.textContent = displayValue || "";
   return;
  }
  if (field === "status") {
   const el = document.getElementById("inlineStatusDisplay");
   if (el) el.textContent = displayValue || "";
   const badge = document.getElementById("summaryStatusBadge");
   if (badge) badge.textContent = displayValue || "";
   const focusValue = document.getElementById("summaryFocusValue");
   if (focusValue && (focusValue.dataset.summaryFocusMode || "") === "status") {
    focusValue.textContent = displayValue || "Internal status ";
   }
   return;
  }
  if (field === "assignee_id") {
   const el = document.getElementById("inlineAssigneeDisplay");
   applyAssigneeDisplay(el, rawValue, displayValue);
   return;
  }
  if (field === "client_id") {
   const el = document.getElementById("inlineClientDisplay");
   applyClientDisplay(el, rawValue, displayValue);
  }
 }

 async function saveInlineField(container, field, rawValue, displayValue, prevState) {
  if (!CASE_VIEW.caseId) return;
  const payload = {};
  payload[field] = rawValue;
  container.dataset.saving = "1";
  try {
   const data = await apiJson(`/api/cases/${CASE_VIEW.caseId}`, "PATCH", payload);
   container.dataset.value = rawValue;
   if (field === "client_id") {
    container.dataset.name = displayValue || "";
   }
   applyInlineDisplay(field, displayValue, rawValue);
   if (data.audit_ids && data.audit_ids.length) {
    const auditId = data.audit_ids[0];
    showUndoToast("Changed", auditId, async () => {
     await apiJson(`/api/case_audits/${auditId}/undo`, "POST");
     container.dataset.value = prevState.rawValue;
     if (field === "client_id") {
      container.dataset.name = prevState.displayValue || "";
     }
     applyInlineDisplay(field, prevState.displayValue, prevState.rawValue);
     refreshSummary();
     refreshAuditLog();
    });
   } else {
    showToast("Changed", "success");
   }
   refreshSummary();
   refreshAuditLog();
  } finally {
   container.dataset.saving = "0";
  }
 }

 function setupInlineEdits() {
  if (!CASE_VIEW.canEditCase) return;
  const root = document.getElementById("caseViewRoot");
  if (!root) return;
  if (root.dataset.inlineEditsBound === "1") return;
  root.dataset.inlineEditsBound = "1";

  const modalEl = document.getElementById("clientSearchModal");
  let activeClientEdit = null;

  const clearEditor = (container) => {
   if (!container) return;
   const editor = container.querySelector(".inline-editor");
   if (editor) editor.innerHTML = "";
   container.classList.remove("is-editing");
   container.dataset.inlineCommitting = "0";
   if (activeClientEdit && activeClientEdit.container === container) {
    activeClientEdit = null;
   }
  };

  const getCurrentPrevState = (container) => {
   const rawValue = (container?.dataset?.inlinePrevRawValue ?? container?.dataset?.value ?? "").toString();
   const fallbackDisplay = container?.querySelector(".inline-display")?.textContent?.trim() || "";
   const displayValue = (container?.dataset?.inlinePrevDisplayValue ?? fallbackDisplay).toString();
   return { rawValue, displayValue };
  };

  const setCurrentPrevState = (container, state) => {
   if (!container) return;
   container.dataset.inlinePrevRawValue = (state?.rawValue ?? "").toString();
   container.dataset.inlinePrevDisplayValue = (state?.displayValue ?? "").toString();
  };

  const commitInlineEdit = async (container, nextRawValue, nextDisplayValue) => {
   if (!container) return;
   if (!container.classList.contains("is-editing")) return;
   if (container.dataset.saving === "1" || container.dataset.inlineCommitting === "1") return;
   const field = (container.dataset.inlineField || "").trim();
   if (!field) {
    clearEditor(container);
    return;
   }
   const prevState = getCurrentPrevState(container);
   const nextRaw = (nextRawValue ?? "").toString().trim();
   const nextDisplay = (nextDisplayValue ?? "").toString().trim();
   if (nextRaw === prevState.rawValue) {
    clearEditor(container);
    return;
   }
   container.dataset.inlineCommitting = "1";
   try {
    await saveInlineField(container, field, nextRaw, nextDisplay || nextRaw, prevState);
    clearEditor(container);
   } catch (err) {
    showToast(err.message || "Save failed", "danger");
   } finally {
    container.dataset.inlineCommitting = "0";
   }
  };

  const openInlineEditor = (container) => {
   if (!container || container.classList.contains("is-editing")) return;
   const editor = container.querySelector(".inline-editor");
   if (!editor) return;
   const type = (container.dataset.inlineType || "text").toLowerCase();
   const currentValue = (container.dataset.value || "").toString();
   const currentDisplay = container.querySelector(".inline-display")?.textContent?.trim() || "";
   setCurrentPrevState(container, { rawValue: currentValue, displayValue: currentDisplay });

   root.querySelectorAll(".inline-edit.is-editing").forEach((other) => {
    if (other !== container) clearEditor(other);
   });

   container.classList.add("is-editing");
   editor.innerHTML = "";

   if (type === "select") {
    const template = document.getElementById("assigneeOptionsTemplate");
    if (!template) {
     clearEditor(container);
     return;
    }
    const select = template.cloneNode(true);
    select.classList.remove("d-none");
    select.id = "";
    select.value = currentValue;
    select.dataset.inlineInput = "select";
    editor.appendChild(select);
    select.focus();
    return;
   }

   if (type === "client") {
    const uid = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "form-control form-control-sm";
    nameInput.value = container.dataset.name || currentDisplay || "";
    nameInput.readOnly = true;
    nameInput.id = `inlineClientNameTemp-${uid}`;

    const idInput = document.createElement("input");
    idInput.type = "hidden";
    idInput.value = currentValue;
    idInput.id = `inlineClientIdTemp-${uid}`;

    const searchBtn = document.createElement("button");
    searchBtn.type = "button";
    searchBtn.className = "btn btn-sm btn-outline-secondary";
    searchBtn.textContent = "Search";
    searchBtn.dataset.inlineClientSearch = "1";
    searchBtn.dataset.inlineClientNameInput = nameInput.id;
    searchBtn.dataset.inlineClientIdInput = idInput.id;

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-sm btn-outline-secondary mt-1";
    cancelBtn.textContent = "Cancel";
    cancelBtn.dataset.inlineCancel = "1";

    const group = document.createElement("div");
    group.className = "input-group input-group-sm";
    group.appendChild(nameInput);
    group.appendChild(searchBtn);
    editor.appendChild(group);
    editor.appendChild(idInput);
    editor.appendChild(cancelBtn);
    return;
   }

   const input = document.createElement("input");
   input.type = "text";
   input.className = "form-control form-control-sm";
   input.value = currentValue || currentDisplay;
   input.dataset.inlineInput = "text";
   editor.appendChild(input);
   input.focus();
   input.select();
  };

  if (modalEl && modalEl.dataset.inlineModalBound !== "1") {
   modalEl.dataset.inlineModalBound = "1";
   modalEl.addEventListener("hidden.bs.modal", () => {
    if (!activeClientEdit) return;
    const { container, idInput, nameInput } = activeClientEdit;
    const prevState = getCurrentPrevState(container);
    activeClientEdit = null;
    const nextId = (idInput?.value || "").trim();
    const nextName = (nameInput?.value || "").trim();
    if (!nextId || nextId === prevState.rawValue) {
     clearEditor(container);
     return;
    }
    commitInlineEdit(container, nextId, nextName);
   });
  }

  root.addEventListener("click", (e) => {
   const editBtn = e.target.closest(".inline-edit-btn");
   if (editBtn) {
    const container = editBtn.closest(".inline-edit");
    openInlineEditor(container);
    return;
   }

   const searchBtn = e.target.closest("[data-inline-client-search='1']");
   if (searchBtn) {
    const container = searchBtn.closest(".inline-edit");
    const idInput = document.getElementById(searchBtn.dataset.inlineClientIdInput || "");
    const nameInput = document.getElementById(searchBtn.dataset.inlineClientNameInput || "");
    if (!container || !idInput || !nameInput) return;
    activeClientEdit = { container, idInput, nameInput };
    if (window.openClientSearch) {
     window.openClientSearch(nameInput.id, idInput.id);
    }
    return;
   }

   const cancelBtn = e.target.closest("[data-inline-cancel='1']");
   if (cancelBtn) {
    const container = cancelBtn.closest(".inline-edit");
    clearEditor(container);
   }
  });

  root.addEventListener("change", (e) => {
   const select = e.target.closest(".inline-editor select[data-inline-input='select']");
   if (!select) return;
   const container = select.closest(".inline-edit");
   const value = (select.value || "").toString();
   const label = select.options[select.selectedIndex]?.textContent || "";
   commitInlineEdit(container, value, label);
  });

  root.addEventListener("keydown", (e) => {
   const input = e.target.closest(".inline-editor input[data-inline-input='text']");
   if (!input) return;
   const container = input.closest(".inline-edit");
   if (!container) return;
   if (e.key === "Enter") {
    e.preventDefault();
    const value = (input.value || "").trim();
    commitInlineEdit(container, value, value);
   } else if (e.key === "Escape") {
    e.preventDefault();
    clearEditor(container);
   }
  });

  root.addEventListener("focusout", (e) => {
   const input = e.target.closest(".inline-editor [data-inline-input]");
   if (!input) return;
   const editor = input.closest(".inline-editor");
   if (!editor) return;
   if (editor.contains(e.relatedTarget)) return;
   const container = editor.closest(".inline-edit");
   if (!container) return;
   if (!container.classList.contains("is-editing")) return;

   const inputType = input.dataset.inlineInput;
   if (inputType === "select") {
    const select = input;
    const value = (select.value || "").toString();
    const label = select.options?.[select.selectedIndex]?.textContent || "";
    commitInlineEdit(container, value, label);
   } else if (inputType === "text") {
    const value = (input.value || "").trim();
    commitInlineEdit(container, value, value);
   }
  });
 }

 function appendDeadlineRow(item) {
  const tbody = document.getElementById("caseDeadlineList");
  if (!tbody || !item) return;
  const empty = tbody.querySelector('[data-empty="1"]');
  if (empty) empty.remove();
  const tr = document.createElement("tr");
  tr.className = "legacy-row-hover";
  const docketId = item.id || "";
  const assigneeName = item.assignee_id ? (STAFF_USER_MAP[item.assignee_id] || "") : "";
  const visibleFrom = item.visible_from_date || "";

  tr.dataset.docketId = docketId;
  tr.dataset.docketName = item.label || "";
  tr.dataset.nameRef = "";
  tr.dataset.dueDate = item.due_date || "";
  tr.dataset.internalDueDate = item.internal_due_date || "";
  tr.dataset.visibleFromDate = visibleFrom;
  tr.dataset.ownerPartyId = "";
  tr.dataset.assigneeId = item.assignee_id || "";
  tr.dataset.isSystem = "0";
  tr.dataset.locked = "0";
  tr.dataset.priority = item.priority || "";

  const editBtn = CASE_VIEW.canEditCase
   ? `<button type="button" class="btn btn-sm btn-outline-secondary" data-deadline-edit="1">Edit</button>`
   : "";
  const nameHtml = docketId
   ? `<a href="/deadline/item/${encodeURIComponent(docketId)}" class="text-decoration-none">${escapeHtml(item.label || "")}</a>`
   : escapeHtml(item.label || "");
  tr.innerHTML = `
   <td style="text-align:left;">${nameHtml}</td>
   <td align="center">${escapeHtml(item.due_date || "-")}</td>
   <td align="center">${escapeHtml(item.internal_due_date || "-")}</td>
   <td align="center">${visibleFrom ? escapeHtml(visibleFrom) : '<span class="text-muted">Immediate</span>'}</td>
   <td align="center">${escapeHtml(assigneeName)}</td>
   <td align="center">${escapeHtml(item.priority || "")}</td>
   ${CASE_VIEW.canEditCase ? `<td align="center">${editBtn}</td>` : ""}
  `;
  tbody.prepend(tr);
  const countEl = document.getElementById("caseDeadlineCount");
  if (countEl) {
   const openCount = tbody.querySelectorAll("tr[data-docket-id]").length;
   const completedCount = Number.parseInt(countEl.dataset.completedCount || "0", 10) || 0;
   countEl.textContent = completedCount
    ? `Open ${openCount}items / Done ${completedCount}items`
    : `Open ${openCount}items`;
  }
 }

 function appendMemoRow(item) {
  const tbody = document.getElementById("caseMemoList");
  if (!tbody || !item) return;
  const empty = tbody.querySelector('[data-empty="1"]');
  if (empty) empty.remove();
  const tr = document.createElement("tr");
  tr.className = "legacy-row-hover";
  const createdAt = item.created_at ? formatDateTime(item.created_at) : formatDateTime(new Date().toISOString());
  const createdDate = createdAt ? createdAt.slice(0, 10) : "-";
  const attachments = Array.isArray(item.attachments) ? item.attachments : [];
  const attachmentsHtml = attachments.length
   ? attachments.map((att) => {
     const fileId = att.file_asset_id || "";
     const name = escapeHtml(att.original_name || att.filename || fileId || "");
     const href = fileId ? `/case/${CASE_VIEW.caseId}/file/${fileId}/download` : "#";
     return `<div class="d-flex align-items-center gap-2"><a href="${href}">${name}</a></div>`;
    }).join("")
   : '<span class="text-muted small">-</span>';
  const memoDeleteHtml = item.id
   ? `
    <form method="POST" action="/case/${CASE_VIEW.caseId}/memo/${item.id}/delete" class="edit-only mt-1" data-confirm="Notes Delete">
     <input type="hidden" name="csrf_token" value="${escapeAttr(CASE_VIEW.csrfToken || '')}">
     <button type="submit" class="btnRed">Notes Delete</button>
    </form>
   `
   : "";
  tr.innerHTML = `
   <td align="center">${escapeHtml(item.created_by || "-")}</td>
   <td align="center" title="${escapeHtml(createdAt)}">${escapeHtml(createdDate)}</td>
   <td style="text-align:left; white-space: pre-wrap;">${escapeHtml(item.content || "-")}</td>
   <td style="text-align:left;">${attachmentsHtml}${memoDeleteHtml}</td>
  `;
  tbody.prepend(tr);
 }

 function appendFileRow(item) {
  const tbody = document.getElementById("caseFileList");
  if (!tbody || !item) return;
  const empty = tbody.querySelector('[data-empty="1"]');
  if (empty) empty.remove();
  const docLabel = DOC_TYPE_LABELS[item.doc_type] || "Other";
  const tags = Array.isArray(item.tags) ? item.tags : (item.tags ? [item.tags] : []);
  const tagsHtml = tags.length
   ? tags.map((t) => `<span class="badge bg-light text-secondary border">${escapeHtml(t)}</span>`).join("")
   : '<span class="text-muted small">-</span>';
  const safeName = escapeHtml(item.filename || "");
  const tr = document.createElement("tr");
  tr.setAttribute("data-file-id", item.file_id);
  tr.setAttribute("data-doc-type", item.doc_type || "OTHER");
  const previewBtn = isPreviewableFilename(item.filename)
   ? `<button type="button" class="btn btn-sm btn-outline-primary js-preview-file" data-preview-url="${escapeAttr(safeUrl(item.preview_url) || '')}" data-filename="${safeName}">Preview</button>`
   : "";
  const deleteBtn = CASE_VIEW.canEditCase && item.matter_file_id
   ? `
    <form method="POST" action="/case/${encodeURIComponent(CASE_VIEW.caseId)}/fm/delete" class="d-inline" data-confirm=" Delete">
     <input type="hidden" name="csrf_token" value="${escapeAttr(CASE_VIEW.csrfToken || "")}">
     <input type="hidden" name="matter_file_id" value="${escapeAttr(item.matter_file_id)}">
     <input type="hidden" name="current_folder_id" value="${escapeAttr(CASE_VIEW.fmFolderId || "")}">
     <button type="submit" class="btn btn-sm btn-outline-danger">Delete</button>
    </form>
   `
   : "";
  tr.innerHTML = `
   <td>
    <div class="d-flex align-items-center gap-2">
     <i class="bi bi-file-earmark-text"></i>
     <div class="text-truncate">
      <div class="fw-semibold text-truncate">${safeName}</div>
      <div class="text-muted" style="font-size: 11px;">-</div>
     </div>
     <span class="badge bg-light text-dark border doc-type-badge">${escapeHtml(docLabel)}</span>
    </div>
   </td>
   <td>
    <select class="form-select form-select-sm doc-type-select" data-file-id="${item.file_id}">
     ${Object.keys(DOC_TYPE_LABELS).map((code) => `<option value="${code}" ${code === item.doc_type ? "selected" : ""}>${DOC_TYPE_LABELS[code]}</option>`).join("")}
    </select>
   </td>
   <td><div class="case-file-tags">${tagsHtml}</div></td>
   <td>${(item.created_at || "").slice(0, 10)}</td>
   <td class="d-flex gap-1">
    ${previewBtn}
    <a class="btn btn-sm btn-outline-secondary" href="${item.download_url}">Download</a>
    ${deleteBtn}
   </td>
  `;
  tbody.prepend(tr);
 }

 function closeQuickActionModal(form) {
  if (!form || !window.bootstrap) return;
  const modalEl = form.closest(".modal");
  if (!modalEl) return;
  const modal = bootstrap.Modal.getInstance(modalEl) || bootstrap.Modal.getOrCreateInstance(modalEl);
  if (modal) modal.hide();
 }

 function bindQuickFormSubmit(options) {
  const {
   formId,
   successMessage = "Saved",
   fallbackErrorMessage = "Save failed",
   submit,
   onSuccess,
   onAfterSuccess,
  } = options || {};
  if (!formId || typeof submit !== "function") return;
  const form = document.getElementById(formId);
  if (!form) return;
  if (form.dataset.quickSubmitBound === "1") return;
  form.dataset.quickSubmitBound = "1";

  form.addEventListener("submit", async (e) => {
   e.preventDefault();
   const submitBtn = form.querySelector('button[type="submit"]');
   if (submitBtn) submitBtn.disabled = true;
   try {
    const data = await submit(form);
    if (typeof onSuccess === "function") {
     await onSuccess(data, form);
    }
    refreshSummary();
    showToast(successMessage, "success");
    closeQuickActionModal(form);
    form.reset();
    if (typeof onAfterSuccess === "function") {
     onAfterSuccess(data, form);
    }
   } catch (err) {
    showToast(err.message || fallbackErrorMessage, "danger");
   } finally {
    if (submitBtn) submitBtn.disabled = false;
   }
  });
 }

 function setupQuickActions() {
  if (!CASE_VIEW.canEditCase) return;

  bindQuickFormSubmit({
   formId: "quickDeadlineForm",
   successMessage: "Saved",
   fallbackErrorMessage: "Save failed",
   submit: async (form) => {
    const dueDate = form.due_date.value || null;
    const internalDueDate = form.internal_due_date.value || null;
    if (!dueDate && !internalDueDate) {
     throw new Error(" Due date(Statutory deadlinedays) Internal/ Due date enter.");
    }
    const payload = {
     label: form.label.value.trim(),
     due_date: dueDate,
     internal_due_date: internalDueDate,
     visible_from_date: form.visible_from_date.value || null,
     assignee_id: form.assignee_id.value || null,
     priority: form.priority.value.trim(),
    };
    return apiJson(`/api/cases/${CASE_VIEW.caseId}/deadlines`, "POST", payload);
   },
   onSuccess: async (data) => {
    try {
     await refreshCaseSection("deadlines", "sec-deadlines");
    } catch (e) {
     appendDeadlineRow(data);
    }
   },
   onAfterSuccess: () => {},
  });

  bindQuickFormSubmit({
   formId: "quickMemoForm",
   successMessage: "Saved",
   fallbackErrorMessage: "Save failed",
   submit: async (form) => {
    const payload = { content: form.content.value.trim() };
    return apiJson(`/api/cases/${CASE_VIEW.caseId}/memos`, "POST", payload);
   },
   onSuccess: async (data) => {
    try {
     await refreshCaseSection("memo", "sec-memo");
    } catch (e) {
     appendMemoRow(data);
    }
   },
  });

  bindQuickFormSubmit({
   formId: "quickFileForm",
   successMessage: "Upload complete",
   fallbackErrorMessage: "Upload failed",
   submit: async (form) => {
    const formData = new FormData(form);
    return apiForm(`/api/cases/${CASE_VIEW.caseId}/files`, formData, "POST");
   },
   onSuccess: async (data) => {
    try {
     await refreshCaseSection("files", "sec-files");
    } catch (e) {
     appendFileRow(data);
    }
   },
  });
 }

 function setupDeadlineEditor() {
  if (!CASE_VIEW.canEditCase) return;
  if (!window.bootstrap) return;

  const root = document.getElementById("caseViewRoot");
  const modalEl = document.getElementById("editDeadlineModal");
  const form = document.getElementById("editDeadlineForm");
  if (!root || !modalEl || !form) return;

  const systemHint = document.getElementById("editDeadlineSystemHint");
  const priorityGroup = document.getElementById("editDeadlinePriorityGroup");
  const lockGroup = document.getElementById("editDeadlineLockGroup");
  const lockInput = document.getElementById("editDeadlineLock");
  const deleteBtn = document.getElementById("editDeadlineDeleteBtn");

  const labelInput = form.querySelector('input[name="label"]');
  const dueInput = form.querySelector('input[name="due_date"]');
  const internalDueInput = form.querySelector('input[name="internal_due_date"]');
  const visibleFromInput = form.querySelector('input[name="visible_from_date"]');
  const assigneeSelect = form.querySelector('select[name="assignee_id"]');
  const priorityInput = form.querySelector('input[name="priority"]');

  function applySystemMode(isSystem, isAuto, locked) {
   if (form.is_system) form.is_system.value = isSystem ? "1" : "0";
   form.dataset.isAuto = isAuto ? "1" : "0";
   const showAutoUi = !!isSystem && !!isAuto;
   if (systemHint) systemHint.style.display = showAutoUi ? "" : "none";
   if (lockGroup) lockGroup.style.display = showAutoUi ? "" : "none";
   if (priorityGroup) priorityGroup.style.display = isSystem ? "none" : "";
   if (deleteBtn) {
    deleteBtn.style.display = isSystem ? "none" : "";
    deleteBtn.disabled = false;
   }

   // Guardrails: system deadlines are auto-updated unless locked.
   const allowOverride = !isSystem || !isAuto || !!locked;
   if (labelInput) labelInput.disabled = !allowOverride;
   if (dueInput) dueInput.disabled = !allowOverride;
   if (internalDueInput) internalDueInput.disabled = !allowOverride;
   if (lockInput && !showAutoUi) lockInput.checked = false;
  }

  root.addEventListener("click", (e) => {
   const btn = e.target.closest('[data-deadline-edit="1"]');
   if (!btn) return;
   const row = btn.closest("tr[data-docket-id]");
   if (!row) return;

   const docketId = row.dataset.docketId || "";
   if (!docketId) return;

   const isSystem = row.dataset.isSystem === "1";
   const isAuto = row.dataset.isAuto === "1";
   const locked = row.dataset.locked === "1";

   if (form.docket_id) form.docket_id.value = docketId;
   if (labelInput) labelInput.value = row.dataset.docketName || "";
   if (dueInput) dueInput.value = row.dataset.dueDate || "";
   if (internalDueInput) internalDueInput.value = row.dataset.internalDueDate || "";
   if (visibleFromInput) visibleFromInput.value = row.dataset.visibleFromDate || "";
   if (assigneeSelect) assigneeSelect.value = row.dataset.assigneeId || "";
   if (priorityInput) priorityInput.value = row.dataset.priority || "";
   if (lockInput) lockInput.checked = isAuto ? locked : false;

   applySystemMode(isSystem, isAuto, locked);

   const modal =
    bootstrap.Modal.getInstance(modalEl) || bootstrap.Modal.getOrCreateInstance(modalEl);
   modal.show();
  });

  if (lockInput && lockInput.dataset.bound !== "1") {
   lockInput.dataset.bound = "1";
   lockInput.addEventListener("change", () => {
    const isSystem = (form.is_system?.value || "0") === "1";
    const isAuto = (form.dataset.isAuto || "0") === "1";
    applySystemMode(isSystem, isAuto, lockInput.checked);
   });
  }

  if (deleteBtn && deleteBtn.dataset.bound !== "1") {
   deleteBtn.dataset.bound = "1";
   deleteBtn.addEventListener("click", async () => {
    const docketId = (form.docket_id?.value || "").trim();
    if (!docketId) return;
    const isSystem = (form.is_system?.value || "0") === "1";
    if (isSystem) {
     showToast(" TaskDeadline cannot be deleted.", "warning");
     return;
    }
    if (!window.confirm("User TaskDeadline Delete ? ")) return;

    const submitBtn = form.querySelector('button[type="submit"]');
    deleteBtn.disabled = true;
    if (submitBtn) submitBtn.disabled = true;
    try {
     await apiJson(
      `/api/cases/${encodeURIComponent(CASE_VIEW.caseId)}/deadlines/${encodeURIComponent(docketId)}`,
      "DELETE"
     );
     await refreshCaseSection("deadlines", "sec-deadlines");
     refreshSummary();
     showToast("Delete", "success");
     closeQuickActionModal(form);
     form.reset();
     applySystemMode(false, false, false);
    } catch (err) {
     showToast(err.message || "Delete ", "danger");
    } finally {
     deleteBtn.disabled = false;
     if (submitBtn) submitBtn.disabled = false;
    }
   });
  }

  if (form.dataset.deadlineEditBound === "1") return;
  form.dataset.deadlineEditBound = "1";

  form.addEventListener("submit", async (e) => {
   e.preventDefault();
   const submitBtn = form.querySelector('button[type="submit"]');
   if (submitBtn) submitBtn.disabled = true;

   const docketId = (form.docket_id?.value || "").trim();
   if (!docketId) {
    if (submitBtn) submitBtn.disabled = false;
    return;
   }

   const isSystem = (form.is_system?.value || "0") === "1";
   const isAuto = (form.dataset.isAuto || "0") === "1";

   const payload = {};

   // Only send label/due when editable (avoid accidentally clearing system fields).
   if (labelInput && !labelInput.disabled) {
    payload.label = labelInput.value.trim();
   }
   if (dueInput && !dueInput.disabled) {
    payload.due_date = dueInput.value || null;
   }
   if (internalDueInput && !internalDueInput.disabled) {
    payload.internal_due_date = internalDueInput.value || null;
   }

   payload.visible_from_date = visibleFromInput?.value ? visibleFromInput.value : null;
   payload.assignee_id = assigneeSelect?.value ? assigneeSelect.value : null;

   if (!isSystem) {
    payload.priority = (priorityInput?.value || "").trim();
   } else if (isAuto) {
    payload.locked = !!(lockInput && lockInput.checked);
   }

   try {
    await apiJson(
     `/api/cases/${encodeURIComponent(CASE_VIEW.caseId)}/deadlines/${encodeURIComponent(docketId)}`,
     "PATCH",
     payload
    );
    await refreshCaseSection("deadlines", "sec-deadlines");
    refreshSummary();
    showToast("Saved", "success");
    closeQuickActionModal(form);
    form.reset();
    applySystemMode(false, false, false);
   } catch (err) {
    showToast(err.message || "Save failed", "danger");
   } finally {
    if (submitBtn) submitBtn.disabled = false;
    if (deleteBtn) deleteBtn.disabled = false;
   }
  });
 }

 function setupFileFilters() {
  const filterWrap = document.getElementById("caseFileFilters");
  const list = document.getElementById("caseFileList");
  if (!filterWrap || !list) return;
  if (filterWrap.dataset.bound === "1") return;
  filterWrap.dataset.bound = "1";
  filterWrap.addEventListener("click", (e) => {
   const btn = e.target.closest("[data-doc-filter]");
   if (!btn) return;
   filterWrap.querySelectorAll("[data-doc-filter]").forEach((b) => b.classList.remove("active"));
   btn.classList.add("active");
   const filter = btn.dataset.docFilter;
   list.querySelectorAll("tr[data-file-id]").forEach((row) => {
    const doc = row.dataset.docType || "OTHER";
    row.style.display = (filter === "all" || filter === doc) ? "" : "none";
   });
  });
 }

 function setupFilePreview() {
  const list = document.getElementById("caseFileList");
  if (!list) return;
  if (list.dataset.previewBound === "1") return;
  list.dataset.previewBound = "1";
  list.addEventListener("click", (e) => {
   const btn = e.target.closest(".js-preview-file");
   if (!btn) return;
   const url = btn.dataset.previewUrl;
   const name = btn.dataset.filename || "Preview";
   const frame = document.getElementById("filePreviewFrame");
   const title = document.getElementById("filePreviewTitle");
   const safe = safeUrl(url);
   if (!safe) {
    showToast("Preview URL is invalid.", "danger");
    return;
   }
   if (frame) frame.src = safe;
   if (title) title.textContent = name;
   const modalEl = document.getElementById("filePreviewModal");
   if (modalEl && window.bootstrap) {
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modal.show();
   }
  });

  const modalEl = document.getElementById("filePreviewModal");
  if (modalEl && modalEl.dataset.previewBound !== "1") {
   modalEl.dataset.previewBound = "1";
   modalEl.addEventListener("hidden.bs.modal", () => {
    const frame = document.getElementById("filePreviewFrame");
    if (frame) frame.src = "";
   });
  }
 }

 function setupDocTypeUpdates() {
  if (!CASE_VIEW.canEditCase) return;
  const list = document.getElementById("caseFileList");
  if (!list) return;
  if (list.dataset.docTypeBound === "1") return;
  list.dataset.docTypeBound = "1";
  list.addEventListener("change", async (e) => {
   const sel = e.target.closest(".doc-type-select");
   if (!sel) return;
   const fileId = sel.dataset.fileId;
   const docType = sel.value;
   const row = sel.closest("tr");
   const prevType = row?.dataset?.docType || docType;
   try {
    await apiJson(`/api/files/${fileId}`, "PATCH", { doc_type: docType, case_id: CASE_VIEW.caseId });
    if (row) row.dataset.docType = docType;
    const badge = row?.querySelector(".doc-type-badge");
    if (badge) badge.textContent = DOC_TYPE_LABELS[docType] || "Other";
    showToast("Type Change.", "success");
   } catch (err) {
    sel.value = prevType;
    showToast(err.message || "Change ", "danger");
   }
  });
 }

 const FILE_DROP_ZONES = [
  { id: "left-drop-zone-header", formId: "uploadInternalForm", inputId: "upl-int-file", role: "internal" },
  { id: "right-drop-zone-header", formId: "uploadSubmissionForm", inputId: "upl-sub-file", role: "submission" },
  { id: "empty-drop-zone", formId: "uploadInternalForm", inputId: "upl-int-file", role: "internal" },
  { id: "sec-files", formId: "uploadInternalForm", inputId: "upl-int-file", role: "internal" },
 ];
 let FILE_DROP_GLOBAL_GUARDS_BOUND = false;

 function setupFileDropZones() {
  if (!CASE_VIEW.canEditCase) return;
  if (!FILE_DROP_GLOBAL_GUARDS_BOUND) {
   // Prevent default browser behavior for file drops globally to avoid opening files.
   window.addEventListener("dragover", (e) => e.preventDefault(), false);
   window.addEventListener("drop", (e) => e.preventDefault(), false);
   FILE_DROP_GLOBAL_GUARDS_BOUND = true;
  }

  FILE_DROP_ZONES.forEach((zone) => {
   const el = document.getElementById(zone.id);
   if (!el) return;
   if (el.dataset.dropZoneBound === "1") return;
   el.dataset.dropZoneBound = "1";

   ["dragenter", "dragover", "dragleave", "drop"].forEach((eventName) => {
    el.addEventListener(
     eventName,
     (e) => {
      e.preventDefault();
      e.stopPropagation();
     },
     false
    );
   });

   el.addEventListener(
    "dragenter",
    () => {
     if (zone.id === "sec-files") return; // Don't highlight the whole container.
     el.classList.add("bg-primary", "text-white");
     if (zone.id !== "empty-drop-zone") {
      el.dataset.oldHtml = el.innerHTML;
      el.innerHTML = '<i class="bi bi-download me-2"></i> ';
     } else {
      el.style.borderColor = "#0d6efd";
      el.style.backgroundColor = "rgba(13, 110, 253, 0.05)";
     }
    },
    false
   );

   el.addEventListener(
    "dragover",
    (e) => {
     e.preventDefault();
     if (zone.id === "sec-files") return;
     el.classList.add("bg-primary", "text-white");
    },
    false
   );

   el.addEventListener(
    "dragleave",
    () => {
     el.classList.remove("bg-primary", "text-white");
     if (zone.id !== "empty-drop-zone" && zone.id !== "sec-files") {
      if (el.dataset.oldHtml) el.innerHTML = el.dataset.oldHtml;
     } else if (zone.id === "empty-drop-zone") {
      el.style.borderColor = "transparent";
      el.style.backgroundColor = "";
     }
    },
    false
   );

   el.addEventListener(
    "drop",
    (e) => {
     el.classList.remove("bg-primary", "text-white");
     if (zone.id !== "empty-drop-zone" && zone.id !== "sec-files") {
      if (el.dataset.oldHtml) el.innerHTML = el.dataset.oldHtml;
     } else if (zone.id === "empty-drop-zone") {
      el.style.borderColor = "transparent";
      el.style.backgroundColor = "";
     }

     const dt = e.dataTransfer;
     const files = dt.files;

     if (files.length> 0) {
      const input = document.getElementById(zone.inputId);
      if (input) {
       const container = new DataTransfer();
       for (let i = 0; i < files.length; i += 1) container.items.add(files[i]);
       input.files = container.files;
       const form = document.getElementById(zone.formId);
       if (form) form.submit();
      }
      return;
     }

     const matterFileId = dt.getData("text/plain");
     if (matterFileId && zone.id !== "sec-files") {
      const form = document.createElement("form");
      form.method = "POST";
      form.action = CASE_VIEW_CONFIG.moveFmItemUrl || form.action;
      const csrfInput = document.createElement("input");
      csrfInput.type = "hidden";
      csrfInput.name = "csrf_token";
      csrfInput.value = CASE_VIEW.csrfToken || "";
      form.appendChild(csrfInput);
      const inputId = document.createElement("input");
      inputId.type = "hidden";
      inputId.name = "matter_file_id";
      inputId.value = matterFileId;
      form.appendChild(inputId);
      const inputRole = document.createElement("input");
      inputRole.type = "hidden";
      inputRole.name = "target_role";
      inputRole.value = zone.role;
      form.appendChild(inputRole);
      const inputFolder = document.createElement("input");
      inputFolder.type = "hidden";
      inputFolder.name = "current_folder_id";
      inputFolder.value = CASE_VIEW_CONFIG.fmFolderId || "";
      form.appendChild(inputFolder);
      document.body.appendChild(form);
      form.submit();
     }
    },
    false
   );
  });
 }

 function setupDraggableFileRows() {
  if (!CASE_VIEW.canEditCase) return;
  document.querySelectorAll(".draggable-item").forEach((item) => {
   if (item.dataset.dragBound === "1") return;
   item.dataset.dragBound = "1";
   item.addEventListener("dragstart", (e) => {
    item.style.opacity = "0.4";
    e.dataTransfer.setData("text/plain", item.dataset.id);
    e.dataTransfer.effectAllowed = "move";
   });
   item.addEventListener("dragend", () => {
    item.style.opacity = "1";
   });
  });
 }

 /* ═══════════════════════════════════════════════════
   Matter view items — JS (2026-02-16)
   ═══════════════════════════════════════════════════ */

 /**
  * D-day live countdown — calculates days remaining and applies
  * color-coded chip class to all elements with [data-due-date].
  */
 function updateDdayChips() {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  document.querySelectorAll('.dday-chip[data-due-date]').forEach(chip => {
   const raw = (chip.dataset.dueDate || '').trim();
   if (!raw) { chip.textContent = '-'; return; }
   const due = new Date(raw + 'T00:00:00');
   if (isNaN(due.getTime())) { chip.textContent = '-'; return; }
   const diff = Math.round((due - today) / 86400000);

   chip.classList.remove('dday-chip--overdue', 'dday-chip--urgent', 'dday-chip--normal', 'dday-chip--safe');

   if (diff < 0) {
    chip.textContent = `D+${Math.abs(diff)} Delayed`;
    chip.classList.add('dday-chip--overdue');
   } else if (diff === 0) {
    chip.textContent = 'D-Day';
    chip.classList.add('dday-chip--overdue');
   } else if (diff <= 3) {
    chip.textContent = `D-${diff}`;
    chip.classList.add('dday-chip--urgent');
   } else if (diff <= 7) {
    chip.textContent = `D-${diff}`;
    chip.classList.add('dday-chip--normal');
   } else {
    chip.textContent = `D-${diff}`;
    chip.classList.add('dday-chip--safe');
   }
  });
 }

 /**
  * Clipboard copy handler — handles clicks on .app-copy-btn elements.
  */
 function setupClipboardCopy() {
  if (document.documentElement.dataset.caseClipboardCopyBound === "1") return;
  document.documentElement.dataset.caseClipboardCopyBound = "1";
  document.addEventListener('click', async (e) => {
   const btn = e.target.closest('.app-copy-btn');
   if (!btn) return;
   const text = btn.dataset.copyText || '';
   if (!text) return;
   try {
    await navigator.clipboard.writeText(text);
    btn.classList.add('is-copied');
    const icon = btn.querySelector('i');
    if (icon) {
     icon.className = 'bi bi-clipboard-check';
     setTimeout(() => {
      icon.className = 'bi bi-clipboard';
      btn.classList.remove('is-copied');
     }, 1500);
    }
    showToast('', 'success');
   } catch (err) {
    showToast(' ', 'danger');
   }
  });
 }

 /**
  * Overdue row auto-highlighting — compares due_date columns against today
  * and adds .overdue-row class to rows with past deadlines.
  */
 function highlightOverdueRows() {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  // DUE notice rows
  document.querySelectorAll('#sec-deadlines tbody tr[data-docket-id]').forEach(row => {
   const cells = row.querySelectorAll('td');
   // Typically the due_date is in the 2nd or 3rd column — look for date patterns
   cells.forEach(td => {
    const text = (td.textContent || '').trim();
    const match = text.match(/^(\d{4}-\d{2}-\d{2})/);
    if (match) {
     const d = new Date(match[1] + 'T00:00:00');
     if (!isNaN(d.getTime()) && d < today) {
      row.classList.add('overdue-row');
     }
    }
   });
  });

  // Annuity rows — check due_date column and status
  document.querySelectorAll('#sec-annuity tbody tr').forEach(row => {
   const statusSelect = row.querySelector('select[name="annuity_status"]');
   const status = statusSelect ? statusSelect.value : (row.dataset.status || '');
   if (status === 'paid') {
    row.classList.add('paid-row');
   } else if (status === 'giveup') {
    row.classList.add('annuity-giveup-row');
   } else if (status === 'pending') {
    row.classList.add('annuity-pending-row');
   }
  });
 }

 /**
  * Nav scroll indicators — shows/hides fade gradients when tabs overflow.
  */
 function setupNavScrollIndicators() {
  const wrapper = document.querySelector('.legacy-nav-wrapper');
  const nav = wrapper ? wrapper.querySelector('.legacy-nav') : null;
  if (!wrapper || !nav) return;
  if (nav.dataset.scrollIndicatorsBound === "1") return;
  nav.dataset.scrollIndicatorsBound = "1";

  function updateIndicators() {
   const { scrollLeft, scrollWidth, clientWidth } = nav;
   wrapper.classList.toggle('has-scroll-left', scrollLeft> 4);
   wrapper.classList.toggle('has-scroll-right', scrollLeft + clientWidth < scrollWidth - 4);
  }

  nav.addEventListener('scroll', updateIndicators, { passive: true });
  window.addEventListener('resize', updateIndicators, { passive: true });
  setTimeout(updateIndicators, 200);

  // Auto-center the active tab when clicked
  nav.querySelectorAll('a, button').forEach(link => {
   link.addEventListener('click', () => {
    setTimeout(() => {
     const active = nav.querySelector('.active, [aria-selected="true"]');
     if (active) {
      active.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
     }
    }, 100);
   });
  });
 }

 function setupHistoryOrdering() {
  if (!CASE_VIEW.canEditCase) return;
  const toggleBtn = document.getElementById("historyOrderToggleBtn");
  const saveBtn = document.getElementById("historyOrderSaveBtn");
  const resetBtn = document.getElementById("historyOrderResetBtn");
  const tbody = document.getElementById("caseHistoryTableBody");
  if (!toggleBtn || !tbody) return;
  if (tbody.dataset.historyOrderBound === "1") return;
  tbody.dataset.historyOrderBound = "1";

  const orderUrl = (CASE_VIEW_CONFIG.historyOrderUrl || "").trim();
  const resetUrl = (CASE_VIEW_CONFIG.historyOrderResetUrl || orderUrl).trim();
  if (!orderUrl) return;

  let editing = false;
  let dirty = false;

  function getRows() {
   return Array.from(tbody.querySelectorAll("tr[data-history-row-key]"));
  }

  function refreshRowNumbers() {
   const rows = getRows();
   const total = rows.length;
   rows.forEach((row, idx) => {
    const noEl = row.querySelector(".history-row-no");
    if (noEl) noEl.textContent = String(total - idx);
   });
  }

  function refreshMoveButtons() {
   const rows = getRows();
   rows.forEach((row, idx) => {
    const up = row.querySelector('[data-history-order-move="up"]');
    const down = row.querySelector('[data-history-order-move="down"]');
    if (up) up.disabled = !editing || idx === 0;
    if (down) down.disabled = !editing || idx === rows.length - 1;
    row.classList.toggle("history-order-editing", editing);
   });
   if (saveBtn) saveBtn.disabled = !dirty;
  }

  function setEditing(next) {
   editing = !!next;
   toggleBtn.textContent = editing ? "Order Closed" : "Order ";
   if (saveBtn) saveBtn.classList.toggle("d-none", !editing);
   if (resetBtn) resetBtn.classList.toggle("d-none", !editing);
   tbody.querySelectorAll("[data-history-order-controls-row='1']").forEach((el) => {
    el.classList.toggle("d-none", !editing);
   });
   if (!editing) dirty = false;
   refreshMoveButtons();
  }

  function markDirty() {
   dirty = true;
   refreshRowNumbers();
   refreshMoveButtons();
  }

  tbody.addEventListener("click", (e) => {
   if (!editing) return;
   const btn = e.target.closest("[data-history-order-move]");
   if (!btn) return;
   const row = btn.closest("tr[data-history-row-key]");
   if (!row) return;
   const dir = btn.dataset.historyOrderMove;
   if (dir === "up") {
    const prev = row.previousElementSibling;
    if (prev && prev.matches("tr[data-history-row-key]")) {
     tbody.insertBefore(row, prev);
     markDirty();
    }
   } else if (dir === "down") {
    const next = row.nextElementSibling;
    if (next && next.matches("tr[data-history-row-key]")) {
     tbody.insertBefore(next, row);
     markDirty();
    }
   }
  });

  toggleBtn.addEventListener("click", async () => {
   if (editing && dirty) {
    const ok = await ipmConfirm("Save Order Change exists. Closed ? ");
    if (!ok) return;
    window.location.reload();
    return;
   }
   setEditing(!editing);
  });

  if (saveBtn) {
   saveBtn.addEventListener("click", async () => {
    const order = getRows()
     .map((row) => (row.dataset.historyRowKey || "").trim())
     .filter(Boolean);
    saveBtn.disabled = true;
    try {
     await apiJson(orderUrl, "POST", { order });
     dirty = false;
     refreshMoveButtons();
     showToast(" Order Save.", "success");
    } catch (err) {
     showToast(err.message || "Order Save failed", "danger");
     refreshMoveButtons();
    }
   });
  }

  if (resetBtn) {
   resetBtn.addEventListener("click", async () => {
    const ok = await ipmConfirm("Save Order Reset ? ");
    if (!ok) return;
    resetBtn.disabled = true;
    try {
     await apiJson(resetUrl, "DELETE");
     showToast("Default Sort Reset.", "success");
     window.setTimeout(() => window.location.reload(), 120);
    } catch (err) {
     showToast(err.message || "Order Reset ", "danger");
    } finally {
     resetBtn.disabled = false;
    }
   });
  }

  refreshRowNumbers();
  setEditing(false);
 }

 function setupHistoryMerging() {
  const table = document.getElementById("caseHistoryTable");
  const tbody = document.getElementById("caseHistoryTableBody");
  if (!table || !tbody) return;
  if (tbody.dataset.historyMergeBound === "1") return;
  tbody.dataset.historyMergeBound = "1";

  const mergeUrl = String(CASE_VIEW_CONFIG.historyMergeUrl || "").trim();
  const mergeGroupUrlTemplate = String(CASE_VIEW_CONFIG.historyMergeGroupUrlTemplate || "").trim();
  const mergeAttachmentsUrlTemplate = String(CASE_VIEW_CONFIG.historyMergeAttachmentsUrlTemplate || "").trim();
  const canEdit = !!CASE_VIEW.canEditCase && !!mergeUrl;

  const viewToggleBtn = document.getElementById("historyMergeViewToggleBtn");
  const expandToggleBtn = document.getElementById("historyMergeExpandToggleBtn");
  const selectToggleBtn = document.getElementById("historyMergeSelectToggleBtn");
  const createBtn = document.getElementById("historyMergeCreateBtn");
  const selectAllBtn = document.getElementById("historyMergeSelectAllBtn");
  const selectionClearBtn = document.getElementById("historyMergeSelectionClearBtn");
  const orderToggleBtn = document.getElementById("historyOrderToggleBtn");
  const orderToggleInitiallyDisabled = !!(orderToggleBtn && orderToggleBtn.disabled);
  const groupsBadgeEl = document.getElementById("historyMergeGroupsBadge");
  const modeBadgeEl = document.getElementById("historyMergeModeBadge");
  const selectionBadgeEl = document.getElementById("historyMergeSelectionBadge");
  const hintEl = document.getElementById("historyMergeHint");
  const mergeDataEl = document.getElementById("historyMergeGroupsData");

  function normalizeRowKey(raw) {
   const key = String(raw || "").trim();
   const sep = key.indexOf(":");
   if (sep <= 0) return "";
   const kind = key.slice(0, sep).trim().toLowerCase();
   const rowId = key.slice(sep + 1).trim();
   if (!rowId) return "";
   if (kind !== "letter" && kind !== "notice") return "";
   return `${kind}:${rowId}`;
  }

  function normalizeGroups(rawGroups) {
   if (!Array.isArray(rawGroups)) return [];
   const out = [];
   const seenIds = new Set();
   rawGroups.forEach((raw) => {
    if (!raw || typeof raw !== "object") return;
    const groupId = String(raw.group_id || raw.id || "").trim();
    if (!groupId || seenIds.has(groupId)) return;
    const keys = [];
    const seenKeys = new Set();
    const rawKeys = Array.isArray(raw.member_keys) ? raw.member_keys : [];
    rawKeys.forEach((item) => {
     const key = normalizeRowKey(item);
     if (!key || seenKeys.has(key)) return;
     seenKeys.add(key);
     keys.push(key);
    });
    if (keys.length < 2) return;
    out.push({
     group_id: groupId,
     title: String(raw.title || "").trim(),
     member_keys: keys,
     collapsed: !!raw.collapsed,
     latest_date: String(raw.latest_date || "").trim(),
     doc_names: Array.isArray(raw.doc_names) ? raw.doc_names.map((x) => String(x || "").trim()).filter(Boolean) : [],
     action_summary: String(raw.action_summary || "").trim(),
     owner_name: String(raw.owner_name || "").trim(),
     target: String(raw.target || "").trim(),
     attach_total_count: Number(raw.attach_total_count || 0) || 0,
     attach_email_count: Number(raw.attach_email_count || 0) || 0,
     attach_work_count: Number(raw.attach_work_count || 0) || 0,
     member_count: Number(raw.member_count || keys.length) || keys.length,
    });
    seenIds.add(groupId);
   });
   return out;
  }

  function resolveGroupUrl(groupId) {
   if (!mergeGroupUrlTemplate || !groupId) return "";
   return mergeGroupUrlTemplate.replace("__GROUP_ID__", encodeURIComponent(String(groupId)));
  }

  function resolveAttachmentsUrl(groupId) {
   if (!mergeAttachmentsUrlTemplate || !groupId) return "";
   return mergeAttachmentsUrlTemplate.replace("__GROUP_ID__", encodeURIComponent(String(groupId)));
  }

  function parseMergeGroupsFromDom() {
   if (!mergeDataEl) return [];
   try {
    const parsed = JSON.parse(mergeDataEl.textContent || "[]");
    return normalizeGroups(parsed);
   } catch (e) {
    return [];
   }
  }

  function getDataRows() {
   return Array.from(tbody.querySelectorAll("tr[data-history-row-key]"));
  }

  function getSummaryRows() {
   return Array.from(tbody.querySelectorAll('tr[data-history-merge-summary="1"]'));
  }

  function removeSummaryRows() {
   getSummaryRows().forEach((row) => row.remove());
  }

  function selectedRowKeys() {
   return getDataRows()
    .map((row) => row.querySelector(".history-merge-row-checkbox"))
    .filter(Boolean)
    .filter((cb) => cb.checked)
    .map((cb) => normalizeRowKey(cb.dataset.historyMergeRowKey || ""))
    .filter(Boolean);
  }

  function visibleDataRows() {
   return getDataRows().filter((row) => !row.classList.contains("history-merge-member-hidden"));
  }

  function selectedCount() {
   return selectedRowKeys().length;
  }

  function syncRowSelectionClasses() {
   getDataRows().forEach((row) => {
    const cb = row.querySelector(".history-merge-row-checkbox");
    row.classList.toggle("history-merge-row-selected", !!selecting && !!(cb && cb.checked));
   });
  }

  function checkboxRowKey(cb) {
   if (!cb) return "";
   const fromDataset = normalizeRowKey(cb.dataset.historyMergeRowKey || "");
   if (fromDataset) return fromDataset;
   const row = cb.closest ? cb.closest("tr[data-history-row-key]") : null;
   return normalizeRowKey((row && row.dataset.historyRowKey) || "");
  }

  function rememberSelectionAnchor(cb) {
   selectionAnchorKey = checkboxRowKey(cb) || "";
  }

  function applyShiftSelectionRange(cb, shiftPressed) {
   if (!selecting || !shiftPressed) return false;
   const currentKey = checkboxRowKey(cb);
   if (!currentKey || !selectionAnchorKey || selectionAnchorKey === currentKey) return false;
   const rows = visibleDataRows();
   if (!rows.length) return false;
   const rowKeys = rows.map((row) => normalizeRowKey(row.dataset.historyRowKey || ""));
   const anchorIdx = rowKeys.indexOf(selectionAnchorKey);
   const currentIdx = rowKeys.indexOf(currentKey);
   if (anchorIdx < 0 || currentIdx < 0) return false;
   const checked = !!cb.checked;
   const start = Math.min(anchorIdx, currentIdx);
   const end = Math.max(anchorIdx, currentIdx);
   for (let idx = start; idx <= end; idx += 1) {
    const rowCb = rows[idx] ? rows[idx].querySelector(".history-merge-row-checkbox") : null;
    if (!rowCb || rowCb.disabled) continue;
    rowCb.checked = checked;
   }
   return true;
  }

  let mergeGroups = parseMergeGroupsFromDom();
  let selecting = false;
  let selectionAnchorKey = "";
  const expandedGroupIds = new Set();
  // Default to merged summary view whenever merged groups exist.
  // (Ignore previously saved per-case toggle to keep a consistent default UX.)
  let summaryMode = mergeGroups.length> 0;

  function areAllGroupsExpanded() {
   if (!mergeGroups.length) return false;
   return mergeGroups.every((g) => expandedGroupIds.has(String(g.group_id || "")));
  }

  function updateToolbarStatus() {
   const groupsCount = mergeGroups.length;
   const selCount = selectedCount();
   const visibleCount = visibleDataRows().length;
   const totalRows = getDataRows().length;

   if (groupsBadgeEl) {
    groupsBadgeEl.textContent = ` ${groupsCount}items`;
    groupsBadgeEl.classList.toggle("text-primary", groupsCount> 0);
   }
   if (modeBadgeEl) {
    if (selecting) {
     modeBadgeEl.textContent = " Select ";
     modeBadgeEl.classList.add("text-primary");
    } else if (summaryMode) {
     modeBadgeEl.textContent = " ";
     modeBadgeEl.classList.add("text-primary");
    } else {
     modeBadgeEl.textContent = "items View ";
     modeBadgeEl.classList.remove("text-primary");
    }
   }
   if (selectionBadgeEl) {
    selectionBadgeEl.textContent = `Select ${selCount}items`;
    selectionBadgeEl.classList.toggle("d-none", !selecting);
    selectionBadgeEl.classList.toggle("text-primary", selecting && selCount> 0);
   }
   if (hintEl) {
    if (selecting) {
     hintEl.textContent = " row 2items Select. Shift+to  Select exists.";
    } else if (summaryMode) {
     hintEl.textContent = `row ${groupsCount}items Display · Details ${visibleCount}/${totalRows}row`;
    } else if (groupsCount> 0) {
     hintEl.textContent = ` ${groupsCount}items Save exists. View  exists.`;
    } else {
     hintEl.textContent = "  items View Display .";
    }
   }
   if (expandToggleBtn) {
    expandToggleBtn.disabled = !groupsCount;
    expandToggleBtn.textContent = areAllGroupsExpanded() ? "Details Collapse all" : "Details Expand all";
   }
   if (selectAllBtn) {
    selectAllBtn.classList.toggle("d-none", !selecting);
   }
   if (selectionClearBtn) {
    selectionClearBtn.classList.toggle("d-none", !selecting);
   }
  }

  function syncCreateButtonState() {
   const count = selectedCount();
   syncRowSelectionClasses();
   if (createBtn) {
    createBtn.disabled = !canEdit || !selecting || count < 2;
    createBtn.textContent = selecting ? ` ${count> 0 ? ` (${count})` : ""}` : " ";
   }
   if (selectToggleBtn) {
    selectToggleBtn.textContent = selecting ? ` Select Closed (${count})` : " Select";
   }
   updateToolbarStatus();
  }

  function setSelecting(next) {
   selecting = !!next && canEdit;
   selectionAnchorKey = "";
   table.classList.toggle("history-merge-selecting", selecting);
   if (createBtn) createBtn.classList.toggle("d-none", !selecting);
   if (orderToggleBtn) {
    orderToggleBtn.disabled = orderToggleInitiallyDisabled || !!selecting;
   }
   if (!selecting) {
    getDataRows().forEach((row) => {
     const cb = row.querySelector(".history-merge-row-checkbox");
     if (cb) cb.checked = false;
    });
   } else if (summaryMode) {
    summaryMode = false;
   }
   syncCreateButtonState();
   applySummaryMode();
  }

  function buildSummaryRows() {
   removeSummaryRows();
   const dataRows = getDataRows();
   if (!dataRows.length) {
    mergeGroups = [];
    return;
   }

   const rowIndex = new Map();
   const rowByKey = new Map();
   dataRows.forEach((row, idx) => {
    const rowKey = normalizeRowKey(row.dataset.historyRowKey || "");
    if (!rowKey) return;
    row.dataset.historyMergeGroup = "";
    row.classList.remove("history-merge-member-hidden");
    rowByKey.set(rowKey, row);
    rowIndex.set(rowKey, idx);
   });

   const validGroups = [];
   const claimed = new Set();
   mergeGroups.forEach((group) => {
    const keys = [];
    (group.member_keys || []).forEach((rawKey) => {
     const key = normalizeRowKey(rawKey);
     if (!key || claimed.has(key) || !rowByKey.has(key)) return;
     claimed.add(key);
     keys.push(key);
    });
    if (keys.length < 2) return;
    const item = { ...group, member_keys: keys, member_count: keys.length };
    validGroups.push(item);
    keys.forEach((key) => {
     const row = rowByKey.get(key);
     if (row) row.dataset.historyMergeGroup = item.group_id;
    });
   });

   validGroups.sort((a, b) => {
    const ai = Math.min(...a.member_keys.map((k) => rowIndex.get(k) ?? 999999));
    const bi = Math.min(...b.member_keys.map((k) => rowIndex.get(k) ?? 999999));
    return ai - bi;
   });

   validGroups.forEach((group, idx) => {
    const memberRows = group.member_keys
     .map((k) => ({ key: k, row: rowByKey.get(k), idx: rowIndex.get(k) ?? 999999 }))
     .filter((x) => !!x.row)
     .sort((a, b) => a.idx - b.idx);
    if (memberRows.length < 2) return;

    const topRow = memberRows[0].row;
    if (!topRow) return;
    const summaryRow = document.createElement("tr");
    summaryRow.className = "app-comm-group-row";
    summaryRow.dataset.historyMergeSummary = "1";
    summaryRow.dataset.historyMergeGroup = group.group_id;

    const docPreview = (group.doc_names || []).slice(0, 2).join(" / ");
    const title = String(group.title || "").trim() || docPreview || ` ${idx + 1}`;
    const latestDate = String(group.latest_date || "").trim();
    const actionSummary = String(group.action_summary || "").trim() || "";
    const ownerName = String(group.owner_name || "").trim();
    const target = String(group.target || "").trim();
    const attachTotal = Number(group.attach_total_count || 0) || 0;
    const attachEmail = Number(group.attach_email_count || 0) || 0;
    const attachWork = Number(group.attach_work_count || Math.max(attachTotal - attachEmail, 0)) || 0;
    const attachmentUrl = resolveAttachmentsUrl(group.group_id);
    const expanded = expandedGroupIds.has(group.group_id);

    summaryRow.innerHTML = `
     <td data-label="No"><span class="badge bg-primary-subtle text-primary border border-primary-subtle">G${idx + 1}</span></td>
     <td class="legacy-doc" style="text-align:left;" data-label="Document name">
      <div class="history-doc-main">
       <span class="history-doc-title fw-bold">${escapeHtml(title)}</span>
       <span class="badge bg-light text-secondary border"></span>
      </div>
      <div class="history-merge-group-meta mt-1">
       ${escapeHtml(String(group.member_count || memberRows.length))}items
       ${docPreview ? ` · ${escapeHtml(docPreview)}` : ""}
       ${latestDate ? ` · Recent ${escapeHtml(latestDate)}` : ""}
      </div>
     </td>
     <td data-label="Upload/">${escapeHtml(actionSummary)}</td>
     <td data-label="Uploaddays">${escapeHtml(latestDate)}</td>
     <td data-label="/days">-</td>
     <td data-label="Due date">-</td>
     <td data-label="Due date">-</td>
     <td data-label="TaskContact">${escapeHtml(ownerName)}</td>
     <td class="legacy-target" data-label="target">${escapeHtml(target)}</td>
     <td data-label="">${attachTotal> 0 ? String(attachTotal) : "-"}</td>
     <td data-label="">${
      attachTotal> 0 && attachmentUrl
       ? `<a href="${escapeAttr(attachmentUrl)}" target="_blank" rel="noopener" class="text-decoration-none">View</a>`
       : "-"
     }</td>
     <td data-label="">
      <button type="button" class="btn btn-sm btn-outline-secondary" data-history-merge-toggle-details="${escapeAttr(group.group_id)}">${expanded ? "Collapse" : "Details"}</button>
      ${attachTotal> 0 ? `<span class="badge bg-light text-secondary border ms-1" title="Task attachment/Originaldays">${attachWork}/${attachEmail}</span>` : ""}
      ${
       canEdit
        ? `<button type="button" class="btn btn-sm btn-outline-secondary" data-history-merge-rename="${escapeAttr(group.group_id)}"></button>
          <button type="button" class="btn btn-sm btn-outline-danger" data-history-merge-delete="${escapeAttr(group.group_id)}"></button>`
        : ""
      }
     </td>
    `;
    tbody.insertBefore(summaryRow, topRow);

    group.attach_total_count = attachTotal;
    group.attach_email_count = attachEmail;
    group.attach_work_count = attachWork;
   });

   mergeGroups = validGroups;
   if (!mergeGroups.length) expandedGroupIds.clear();
   updateToolbarStatus();
  }

  function applySummaryMode() {
   if (!mergeGroups.length) summaryMode = false;
   if (summaryMode && selecting) summaryMode = false;

   const summaryRows = getSummaryRows();
   summaryRows.forEach((row) => {
    row.style.display = summaryMode ? "" : "none";
   });

   getDataRows().forEach((row) => {
    const groupId = String(row.dataset.historyMergeGroup || "").trim();
    const hide = !!summaryMode && !!groupId && !expandedGroupIds.has(groupId);
    row.classList.toggle("history-merge-member-hidden", hide);
   });

   if (viewToggleBtn) {
    viewToggleBtn.disabled = !mergeGroups.length;
    viewToggleBtn.textContent = summaryMode
     ? `items View (${mergeGroups.length})`
     : ` View (${mergeGroups.length})`;
   }
   setStoredCasePref("historyMergeSummary", summaryMode ? "1" : "0");
   updateToolbarStatus();
  }

  function suggestTitleFromSelectedRows(keys) {
   const candidates = [];
   keys.forEach((key) => {
    const row = getDataRows().find((r) => normalizeRowKey(r.dataset.historyRowKey || "") === key);
    if (!row) return;
    const docEl = row.querySelector(".legacy-doc span.fw-bold");
    const text = String((docEl && docEl.textContent) || "").trim();
    if (text) candidates.push(text);
   });
   return candidates.length ? candidates[0] : "";
  }

  if (orderToggleBtn) {
   orderToggleBtn.addEventListener("click", () => {
    if (summaryMode) summaryMode = false;
    if (selecting) setSelecting(false);
    applySummaryMode();
   });
  }

  if (viewToggleBtn) {
   viewToggleBtn.addEventListener("click", () => {
    if (!mergeGroups.length) return;
    summaryMode = !summaryMode;
    applySummaryMode();
   });
  }

  if (expandToggleBtn) {
   expandToggleBtn.addEventListener("click", () => {
    if (!mergeGroups.length) return;
    const allExpanded = areAllGroupsExpanded();
    if (allExpanded) {
     expandedGroupIds.clear();
    } else {
     mergeGroups.forEach((g) => {
      const gid = String(g.group_id || "").trim();
      if (gid) expandedGroupIds.add(gid);
     });
    }
    applySummaryMode();
    buildSummaryRows();
    applySummaryMode();
   });
  }

  if (selectToggleBtn) {
   selectToggleBtn.addEventListener("click", async () => {
    if (!canEdit) return;
    if (selecting && selectedRowKeys().length> 0) {
     const ok = await ipmConfirm("selected target exists. Select Closed ? ");
     if (!ok) return;
    }
    setSelecting(!selecting);
   });
  }

  if (selectAllBtn) {
   selectAllBtn.addEventListener("click", () => {
    if (!selecting) return;
    visibleDataRows().forEach((row) => {
     const cb = row.querySelector(".history-merge-row-checkbox");
     if (cb) cb.checked = true;
    });
    syncCreateButtonState();
   });
  }

  if (selectionClearBtn) {
   selectionClearBtn.addEventListener("click", () => {
    if (!selecting) return;
    selectionAnchorKey = "";
    getDataRows().forEach((row) => {
     const cb = row.querySelector(".history-merge-row-checkbox");
     if (cb) cb.checked = false;
    });
    syncCreateButtonState();
   });
  }

  if (createBtn) {
   createBtn.addEventListener("click", async () => {
    if (!canEdit) return;
    const keys = selectedRowKeys();
    if (keys.length < 2) return;
    const defaultTitle = suggestTitleFromSelectedRows(keys);
    const inputTitle = await ipmPrompt(
     " people enter. table Document nameto Auto .",
     defaultTitle
    );
    if (inputTitle === null) return;
    createBtn.disabled = true;
    try {
     await apiJson(mergeUrl, "POST", { row_keys: keys, title: String(inputTitle || "").trim() });
     showToast(" .", "success");
     window.setTimeout(() => window.location.reload(), 120);
    } catch (err) {
     showToast(err.message || " Save failed", "danger");
     syncCreateButtonState();
    }
   });
  }

  tbody.addEventListener("change", (e) => {
   const cb = e.target.closest ? e.target.closest(".history-merge-row-checkbox") : null;
   if (!cb) return;
   rememberSelectionAnchor(cb);
   syncCreateButtonState();
  });

  tbody.addEventListener("click", async (e) => {
   const rowCheckbox = selecting && e.target.closest ? e.target.closest(".history-merge-row-checkbox") : null;
   if (rowCheckbox) {
    applyShiftSelectionRange(rowCheckbox, !!e.shiftKey);
    rememberSelectionAnchor(rowCheckbox);
    syncCreateButtonState();
    return;
   }

   const focusBtn = e.target.closest ? e.target.closest("[data-history-merge-focus]") : null;
   if (focusBtn) {
    const gid = String(focusBtn.getAttribute("data-history-merge-focus") || "").trim();
    if (!gid) return;
    summaryMode = true;
    expandedGroupIds.add(gid);
    applySummaryMode();
    buildSummaryRows();
    applySummaryMode();
    return;
   }

   const toggleBtn = e.target.closest ? e.target.closest("[data-history-merge-toggle-details]") : null;
   if (toggleBtn) {
    const gid = String(toggleBtn.getAttribute("data-history-merge-toggle-details") || "").trim();
    if (!gid) return;
    if (expandedGroupIds.has(gid)) expandedGroupIds.delete(gid);
    else expandedGroupIds.add(gid);
    applySummaryMode();
    buildSummaryRows();
    applySummaryMode();
    return;
   }

   const renameBtn = e.target.closest ? e.target.closest("[data-history-merge-rename]") : null;
   if (renameBtn) {
    if (!canEdit) return;
    const gid = String(renameBtn.getAttribute("data-history-merge-rename") || "").trim();
    const target = mergeGroups.find((g) => g.group_id === gid);
    if (!gid || !target) return;
    const url = resolveGroupUrl(gid);
    if (!url) return;
    const nextTitle = await ipmPrompt(" people enter.", String(target.title || ""));
    if (nextTitle === null) return;
    try {
     await apiJson(url, "PATCH", { title: String(nextTitle || "").trim() });
     showToast(" people Edit.", "success");
     window.setTimeout(() => window.location.reload(), 120);
    } catch (err) {
     showToast(err.message || " Edit ", "danger");
    }
    return;
   }

   const deleteBtn = e.target.closest ? e.target.closest("[data-history-merge-delete]") : null;
   if (deleteBtn) {
    if (!canEdit) return;
    const gid = String(deleteBtn.getAttribute("data-history-merge-delete") || "").trim();
    if (!gid) return;
    const url = resolveGroupUrl(gid);
    if (!url) return;
    const ok = await ipmConfirm(" ? items Delete .");
    if (!ok) return;
    try {
     await apiJson(url, "DELETE");
     showToast(" .", "success");
     window.setTimeout(() => window.location.reload(), 120);
    } catch (err) {
     showToast(err.message || " ", "danger");
    }
    return;
   }

   if (selecting) {
    const row = e.target.closest ? e.target.closest("tr[data-history-row-key]") : null;
    if (!row || row.classList.contains("history-merge-member-hidden")) return;
    const ignoredTarget = e.target.closest
     ? e.target.closest("a, button, input, label, select, textarea, [role='button']")
     : null;
    if (ignoredTarget) return;
    const cb = row.querySelector(".history-merge-row-checkbox");
    if (!cb) return;
    cb.checked = !cb.checked;
    applyShiftSelectionRange(cb, !!e.shiftKey);
    rememberSelectionAnchor(cb);
    syncCreateButtonState();
   }
  });

  let hoverGroupId = "";
  function setRelatedHover(groupId) {
   const gid = String(groupId || "").trim();
   if (gid === hoverGroupId) return;
   hoverGroupId = gid;
   Array.from(tbody.querySelectorAll("tr[data-history-merge-group]")).forEach((row) => {
    const rowGroupId = String(row.dataset.historyMergeGroup || "").trim();
    row.classList.toggle("history-merge-related-hover", !!gid && rowGroupId === gid);
   });
  }

  tbody.addEventListener("mousemove", (e) => {
   const row = e.target.closest ? e.target.closest("tr[data-history-merge-group]") : null;
   if (!row) {
    setRelatedHover("");
    return;
   }
   const gid = String(row.dataset.historyMergeGroup || "").trim();
   setRelatedHover(gid);
  });
  tbody.addEventListener("mouseleave", () => {
   setRelatedHover("");
  });

  buildSummaryRows();
  applySummaryMode();
  setSelecting(false);
 }

 function getWorkspaceSectionIds() {
  const ids = [];
  const pushId = (rawId) => {
   const id = (rawId || "").trim();
   if (!id || ids.includes(id)) return;
   const el = document.getElementById(id);
   if (!el) return;
   ids.push(id);
  };
  ["sec-basic", "sec-domestic-patent"].forEach(pushId);
  document.querySelectorAll(".top-panel[id]").forEach((el) => pushId(el.id));
  document.querySelectorAll("[data-top]").forEach((el) => pushId(el.dataset.top || ""));
  document.querySelectorAll("#bottom-panel> [id]").forEach((el) => pushId(el.id));
  return ids;
 }

 function getWorkspaceSectionHeader(sectionEl) {
  if (!sectionEl) return null;
  const children = Array.from(sectionEl.children || []);
  for (const child of children) {
   if (child.classList && child.classList.contains("legacy-section-header")) return child;
  }
  for (const child of children) {
   if (!child.classList) continue;
   if (child.classList.contains("d-flex")) return child;
  }
  return children[0] || null;
 }

 function getWorkspaceSectionLabel(sectionEl, fallbackId) {
  if (!sectionEl) return fallbackId || "";
  const titleEl = sectionEl.querySelector(".legacy-title");
  if (titleEl) {
   const text = (titleEl.textContent || "").trim();
   if (text) return text;
  }
  const boldEl = sectionEl.querySelector(".fw-bold");
  if (boldEl) {
   const text = (boldEl.textContent || "").trim();
   if (text) return text;
  }
  return fallbackId || "";
 }

 function getWorkspaceSectionBodies(sectionEl, headerEl) {
  return Array.from(sectionEl.children || []).filter((child) => child !== headerEl);
 }

 function updateWorkspaceSectionToggle(item) {
  if (!item || !item.id) return;
  const collapsed = item.section?.dataset?.sectionCollapsed === "1";
  document.querySelectorAll(`[data-section-toggle="${item.id}"]`).forEach((btn) => {
   btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
   btn.innerHTML = collapsed
    ? '<i class="bi bi-chevron-down"></i> Expand'
    : '<i class="bi bi-chevron-up"></i> Collapse';
  });
 }

 function setWorkspaceSectionCollapsed(item, collapsed, persist = true) {
  if (!item || !item.section) return;
  const enabled = !!collapsed;
  item.section.dataset.sectionCollapsed = enabled ? "1" : "0";
  item.section.classList.toggle("case-section-collapsed", enabled);
  (item.bodies || []).forEach((node) => {
   node.hidden = enabled;
  });
  CASE_WORKSPACE_STATE.collapsed[item.id] = enabled;
  updateWorkspaceSectionToggle(item);
 }

 function setupWorkspaceSections() {
  const previousCollapsed = { ...(CASE_WORKSPACE_STATE.collapsed || {}) };
  CASE_WORKSPACE_STATE.sections = [];
  CASE_WORKSPACE_STATE.collapsed = {};
  const ids = getWorkspaceSectionIds();
  ids.forEach((id) => {
   const section = document.getElementById(id);
   if (!section) return;
   const header = getWorkspaceSectionHeader(section);
   const label = getWorkspaceSectionLabel(section, id);
   const bodies = getWorkspaceSectionBodies(section, header);
   section.dataset.caseSectionId = id;
   delete section.dataset.workspaceSectionId;
   const item = {
    id,
    section,
    header,
    bodies,
    label,
    isTop: section.classList.contains("top-panel"),
    toggleBtn: null,
   };
   if (bodies.length> 0) {
    document.querySelectorAll(`[data-section-toggle="${id}"]`).forEach((btn) => {
     if (btn.dataset.sectionToggleBound === "1") return;
     btn.dataset.sectionToggleBound = "1";
     btn.addEventListener("click", (e) => {
      e.preventDefault();
      const collapsed = item.section?.dataset?.sectionCollapsed === "1";
      setWorkspaceSectionCollapsed(item, !collapsed, true);
     });
    });
   }
   const hasDomCollapsedState =
    section.dataset.sectionCollapsed === "1" || section.dataset.sectionCollapsed === "0";
   const hasPreviousCollapsedState =
    Object.prototype.hasOwnProperty.call(previousCollapsed, id);
   let collapsed = section.dataset.defaultCollapsed === "1";
   if (hasPreviousCollapsedState) collapsed = previousCollapsed[id] === true;
   if (hasDomCollapsedState) collapsed = section.dataset.sectionCollapsed === "1";
   setWorkspaceSectionCollapsed(item, collapsed, false);
   CASE_WORKSPACE_STATE.sections.push(item);
  });
 }

 function revealAndScrollToSection(id, options = {}) {
  const targetId = (id || "").trim();
  if (!targetId) return;
  let section = (CASE_WORKSPACE_STATE.sections || []).find((item) => item.id === targetId);
  if (section && section.section && !section.section.isConnected) {
   setupCaseSectionRegistry();
   section = (CASE_WORKSPACE_STATE.sections || []).find((item) => item.id === targetId);
  }
  const isTop = section ? section.isTop : !!options.isTop;
  if (isTop) showTop(targetId);
  if (section && section.section?.dataset?.sectionCollapsed === "1") {
   setWorkspaceSectionCollapsed(section, false, true);
  }
  scrollToId(targetId);
 }

 function setupCaseSectionRegistry() {
  setupWorkspaceSections();
 }

 function scrollToTopSmooth() {
  try {
   window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (e) {
   window.scrollTo(0, 0);
  }
 }

 function hideCaseMobileSheet(sheetEl) {
  if (!sheetEl) return;
  try {
   const bs = window.bootstrap;
   if (!bs || !bs.Offcanvas) return;
   const inst = bs.Offcanvas.getInstance(sheetEl) || bs.Offcanvas.getOrCreateInstance(sheetEl);
   inst.hide();
  } catch (e) {}
 }

 function clickIfExists(id) {
  const el = document.getElementById(id);
  if (!el) return false;
  try {
   el.click();
   return true;
  } catch (e) {
   return false;
  }
 }

 function setupCaseMobileTools() {
  const bar = document.getElementById("caseMobileBar");
  const sheet = document.getElementById("caseMobileSheet");
  if (!bar && !sheet) return;

  const memoBtn = document.getElementById("caseMobileBarMemoBtn");
  if (memoBtn) {
   memoBtn.addEventListener("click", (e) => {
    e.preventDefault();
    clickIfExists("caseQuickMemoBtn");
   });
  }

  const topBtn = document.getElementById("caseMobileBarTopBtn");
  if (topBtn) {
   topBtn.addEventListener("click", (e) => {
    e.preventDefault();
    scrollToTopSmooth();
   });
  }

  if (bar) {
   bar.querySelectorAll("[data-case-mobile-jump]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
     e.preventDefault();
     revealAndScrollToSection(btn.dataset.caseMobileJump || "");
    });
   });
  }

  if (!sheet) return;

  const listEl = document.getElementById("caseMobileSheetSectionList");
  const filterInput = document.getElementById("caseMobileSheetFilter");
  const filterClear = document.getElementById("caseMobileSheetFilterClear");
  let lastQuery = "";

  function buildSectionItem(item) {
   const btn = document.createElement("button");
   btn.type = "button";
   btn.className =
    "list-group-item list-group-item-action d-flex justify-content-between align-items-center gap-2";
   btn.dataset.caseMobileSection = item.id;
   btn.dataset.caseMobileIsTop = item.isTop ? "1" : "0";

   const label = document.createElement("div");
   label.className = "fw-semibold";
   label.textContent = item.label || item.id;

   const badge = document.createElement("span");
   badge.className = `badge ${item.isTop ? "bg-secondary" : "bg-light text-dark border"}`;
   badge.textContent = item.isTop ? "top" : "";

   btn.appendChild(label);
   btn.appendChild(badge);
   return btn;
  }

  function buildGroupHeader(text) {
   const div = document.createElement("div");
   div.className = "list-group-item small text-uppercase text-muted fw-bold";
   div.textContent = text;
   return div;
  }

  function renderSections(queryRaw) {
   if (!listEl) return;
   const query = (queryRaw || "").toString().trim().toLowerCase();
   lastQuery = query;

   const items = Array.from(CASE_WORKSPACE_STATE.sections || []);
   const filtered = !query
    ? items
    : items.filter((it) => {
      const label = (it.label || "").toString().toLowerCase();
      const id = (it.id || "").toString().toLowerCase();
      return label.includes(query) || id.includes(query);
     });

   listEl.innerHTML = "";
   if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "list-group-item text-muted small";
    empty.textContent = "match Section none.";
    listEl.appendChild(empty);
    return;
   }

   const shortcuts = filtered.filter((it) => it.id === "sec-basic" || it.id === "sec-domestic-patent");
   const tops = filtered.filter((it) => it.isTop && it.id !== "sec-basic" && it.id !== "sec-domestic-patent");
   const bottoms = filtered.filter((it) => !it.isTop && it.id !== "sec-basic" && it.id !== "sec-domestic-patent");

   if (shortcuts.length) {
    listEl.appendChild(buildGroupHeader(""));
    shortcuts.forEach((it) => listEl.appendChild(buildSectionItem(it)));
   }
   if (tops.length) {
    listEl.appendChild(buildGroupHeader("top"));
    tops.forEach((it) => listEl.appendChild(buildSectionItem(it)));
   }
   if (bottoms.length) {
    listEl.appendChild(buildGroupHeader(""));
    bottoms.forEach((it) => listEl.appendChild(buildSectionItem(it)));
   }
  }

  // Actions inside the sheet
  sheet.addEventListener("click", (e) => {
   const target = e.target && e.target.closest ? e.target.closest("[data-case-mobile-action]") : null;
   if (!target) return;
   const action = (target.dataset.caseMobileAction || "").trim();
   if (!action) return;
   e.preventDefault();

   hideCaseMobileSheet(sheet);

   window.setTimeout(() => {
    if (action === "top") {
     scrollToTopSmooth();
     return;
    }
    if (action === "edit") {
     if (!clickIfExists("toggleEditMode")) clickIfExists("toggleEditModeWorkflow");
     return;
    }
    if (action === "upload") {
     clickIfExists("caseQuickUploadBtn");
     return;
    }
    if (action === "memo") {
     clickIfExists("caseQuickMemoBtn");
     return;
    }
    if (action === "invoice") {
     clickIfExists("caseQuickInvoiceBtn");
    }
   }, 160);
  });

  // Section navigation inside the sheet
  if (listEl) {
   listEl.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest ? e.target.closest("[data-case-mobile-section]") : null;
    if (!btn) return;
    const id = (btn.dataset.caseMobileSection || "").trim();
    if (!id) return;
    const isTop = (btn.dataset.caseMobileIsTop || "") === "1";
    e.preventDefault();

    hideCaseMobileSheet(sheet);
    window.setTimeout(() => {
     revealAndScrollToSection(id, { isTop });
    }, 160);
   });
  }

  if (filterInput) {
   let timer = null;
   filterInput.addEventListener("input", () => {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(() => renderSections(filterInput.value || ""), 60);
   });
  }
  if (filterInput && filterClear) {
   filterClear.addEventListener("click", () => {
    filterInput.value = "";
    filterInput.focus();
    renderSections("");
   });
  }

  const shouldAutoFocusSheetFilter = () => {
   try {
    return !window.matchMedia("(max-width: 768px)").matches &&
     !window.matchMedia("(pointer: coarse)").matches;
   } catch (e) {
    return true;
   }
  };

  // Refresh list whenever the sheet opens (section labels/counts can change after lazy loads).
  sheet.addEventListener("shown.bs.offcanvas", () => {
   renderSections(filterInput ? filterInput.value : lastQuery);
   if (filterInput && shouldAutoFocusSheetFilter()) {
    try {
     filterInput.focus();
     filterInput.select();
    } catch (e) {}
   }
  });

  renderSections("");
 }

 function scrollHighlightTarget(target) {
  if (!target) return;
  target.classList.add("docket-highlight");
  try {
   target.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (e) {
   target.scrollIntoView();
  }
  window.setTimeout(() => target.classList.remove("docket-highlight"), 6000);
 }

 function highlightTargetByDataAttr(selector, datasetKey, expectedValue) {
  const value = (expectedValue || "").toString().trim();
  if (!value) return;
  const rows = Array.from(document.querySelectorAll(selector));
  const target = rows.find((row) => (row?.dataset?.[datasetKey] || "").toString() === value);
  if (target) {
   const parentDetails = target.closest ? target.closest("details") : null;
   if (parentDetails && !parentDetails.open) parentDetails.open = true;
   if (target.dataset.workflowClosed === "1") {
    setClosedWorkflowRowExpanded(target, true, false);
    updateWorkflowClosedToggleAll();
   }
   scrollHighlightTarget(target);
  }
 }

 function applyInitialQueryHighlights() {
  const params = new URLSearchParams(window.location.search || "");
  const docketId = (params.get("docket_id") || "").trim();
  const workflowId = (params.get("workflow_id") || "").trim();
  const invoiceId = (params.get("invoice_id") || "").trim();

  highlightTargetByDataAttr("#sec-deadlines [data-docket-id]", "docketId", docketId);
  highlightTargetByDataAttr("#sec-workflow [data-workflow-id]", "workflowId", workflowId);
  if (invoiceId) {
   Promise.resolve(showFinanceTab("invoice")).then(() => {
    highlightTargetByDataAttr("#costTabInv [data-invoice-id]", "invoiceId", invoiceId);
   });
  }
 }

 function setupRelatedApplicationPrompt() {
  const modalEl = document.getElementById("relatedApplicationModal");
  if (!modalEl || modalEl.dataset.promptBound === "1") return;
  modalEl.dataset.promptBound = "1";
  if (modalEl.dataset.autoOpen !== "1" || !window.bootstrap) return;

  const target = (modalEl.dataset.target || "").trim();
  const sourceId = (modalEl.dataset.sourceMatterId || "").trim();
  const basisDate = (modalEl.dataset.basisDate || "").trim();
  const promptKey = [
   "case-view",
   "related-application",
   CASE_VIEW.caseId || "",
   target,
   sourceId,
   basisDate,
  ].join(":");
  try {
   if (window.sessionStorage.getItem(promptKey) === "1") return;
   window.sessionStorage.setItem(promptKey, "1");
  } catch (e) {}

  const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
  modal.show();
 }

 function exposeCaseViewGlobals() {
  Object.assign(window, {
   toggleBlock,
   toggleAll,
   toggleAnnuityAll,
   applyWorkflowFilter,
   showFinanceTab,
   showCostTab,
   setEditMode,
   openWorkflowAssign,
   setWorkflowAssignMode,
   openCostAdd,
   openAnnuityAdd,
   openAnnuityEdit,
   openMemoAdd,
   submitAnnuityDelete,
   copySelectValue,
   showTop,
   scrollToId,
   promptLink,
   registerPayablePayment,
   linkPayableInvoice,
   unlinkExpenseInvoice,
   editPayable,
   deletePayable,
  });
 }

 exposeCaseViewGlobals();

 (function initCaseView() {
  // ... existing initCaseView code ...
  const topHost = document.getElementById('top-panel');
  if (topHost) {
   document.querySelectorAll('.top-panel').forEach(p => topHost.appendChild(p));
  }

  const bottomHost = document.getElementById('bottom-panel');
  if (bottomHost) {
   BOTTOM_PANEL_SECTION_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) bottomHost.appendChild(el);
   });
  }
  setupLazySectionViewportFallbacks();

  setupStaticFormBindings();

  document.querySelectorAll('[data-top]').forEach(el => {
   el.addEventListener('click', (e) => {
    const panelId = el.dataset.top;
    if (!panelId) return;
    e.preventDefault();
    showTop(panelId);
    if (el.dataset.scroll) scrollToId(el.dataset.scroll);
    else scrollToId('top-panel');
   });
  });

  document.querySelectorAll('[data-delete-matter]').forEach(btn => {
   btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    confirmDeleteMatter(btn.dataset.deleteMatter || '');
   });
  });

  // Handle history delete buttons
  document.addEventListener('click', function(e) {
   if (e.target.classList.contains('history-delete-btn')) {
    const type = e.target.dataset.type;
    const docName = e.target.dataset.docName;
    const formId = e.target.dataset.formId;
    const formElement = document.getElementById(formId);
    confirmDeleteHistory(type, docName, formElement);
   }
  });

  const key = 'caseView.editMode';
  const canEditMode = !!CASE_VIEW.canEditMode;
  function attachEditToggle(id) {
   if (!canEditMode) return;
   const toggle = document.getElementById(id);
   if (!toggle) return;
   toggle.addEventListener('click', () => {
    const next = !document.body.classList.contains('edit-mode');
    PreferencesManager.setFlag(window.localStorage, key, next);
    setEditMode(next);
   });
  }
  attachEditToggle('toggleEditMode');
  attachEditToggle('toggleEditModeWorkflow');

  let editOn = false;
  if (canEditMode) {
   editOn = PreferencesManager.getFlag(window.localStorage, key) === true;
  } else {
   // Ensure view-only users never see edit-only controls, even if localStorage has a stale flag.
   PreferencesManager.setFlag(window.localStorage, key, false);
  }
  setEditMode(canEditMode ? editOn : false);

  let initialTop = 'sec-history';
  const fromHash = getCurrentHashAnchor();
  if (fromHash) {
   const el = document.getElementById(fromHash);
   if (el && el.classList.contains('top-panel')) {
    initialTop = fromHash;
   }
  }
  showTop(initialTop, { skipHash: true });
  setupCaseSectionRegistry();
  setupCaseMobileTools();

  const initialParams = new URLSearchParams(window.location.search || '');
  const requestedFinanceTab = (initialParams.get('finance_tab') || '').trim().toLowerCase();
  if (requestedFinanceTab) {
   showFinanceTab(requestedFinanceTab);
  } else if (document.getElementById("costTabLedger")) {
   // Keep case detail finance view consistent: default to ledger on open.
   showFinanceTab('ledger');
  }

  const ledgerHiddenPrefKey = 'caseView.showLedgerHidden';
  const ledgerHiddenSessionKey = 'caseView.showLedgerHidden.session';
  const ledgerToggleBtnId = 'toggleLedgerHiddenBtn';

  function normalizeLedgerRows(showHiddenDetails) {
   const registryRows = document.querySelectorAll('#sec-domestic-patent .registry-table tbody tr');
   if (!registryRows.length) return;

   registryRows.forEach((row) => {
    const cells = row.children;
    if (!cells || cells.length < 4) return;

    const leftTh = cells[0];
    const leftTd = cells[1];
    const rightTh = cells[2];
    const rightTd = cells[3];

    // Always reset before applying compact-mode adjustments.
    row.classList.remove('ledger-row-compact-hidden');
    row.style.removeProperty('display');
    leftTh.removeAttribute('colspan');
    leftTd.removeAttribute('colspan');
    rightTh.removeAttribute('colspan');
    rightTd.removeAttribute('colspan');

    if (showHiddenDetails) return;

    const leftHidden = leftTh.classList.contains('ledger-hidden') && leftTd.classList.contains('ledger-hidden');
    const rightHidden = rightTh.classList.contains('ledger-hidden') && rightTd.classList.contains('ledger-hidden');

    if (leftHidden && rightHidden) {
     row.classList.add('ledger-row-compact-hidden');
     return;
    }
    if (!leftHidden && rightHidden) {
     leftTd.colSpan = 3;
     return;
    }
    if (leftHidden && !rightHidden) {
     rightTd.colSpan = 3;
    }
   });
  }

  function applyLedgerHidden(enabled, { persist = false } = {}) {
   document.body.classList.toggle('show-ledger-hidden', !!enabled);
   normalizeLedgerRows(!!enabled);
   const btn = document.getElementById(ledgerToggleBtnId);
   if (btn) {
    btn.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    btn.textContent = enabled ? 'Details ' : 'Details';
   }
   if (persist) {
    PreferencesManager.setFlag(window.localStorage, ledgerHiddenPrefKey, !!enabled);
    PreferencesManager.setFlag(window.sessionStorage, ledgerHiddenSessionKey, !!enabled);
   }
  }

  function syncLedgerHiddenState() {
   const hash = getCurrentHashAnchor();
   const forcedByHash = (hash === 'ledger');
   if (forcedByHash) {
    // Make "Registry details" (hash link) behave consistently across tab clicks in this session.
    PreferencesManager.setFlag(window.sessionStorage, ledgerHiddenSessionKey, true);
   }
   const pref = PreferencesManager.getFlag(window.localStorage, ledgerHiddenPrefKey);
   const sessionPref = PreferencesManager.getFlag(window.sessionStorage, ledgerHiddenSessionKey);
   const enabled = forcedByHash || pref === true || sessionPref === true;
   applyLedgerHidden(enabled, { persist: false });
  }

  function setupLedgerHiddenToggle() {
   const btn = document.getElementById(ledgerToggleBtnId);
   if (!btn) return;
   btn.addEventListener('click', () => {
    const current = document.body.classList.contains('show-ledger-hidden');
    const next = !current;

    // If the page is currently forcing the mode via #ledger, dropping the hash avoids re-forcing.
    const hash = getCurrentHashAnchor();
    if (!next && hash === 'ledger') {
     try {
      history.replaceState(null, '', window.location.pathname + window.location.search);
     } catch (e) {}
    }

    applyLedgerHidden(next, { persist: true });
   });
  }

  window.addEventListener('hashchange', syncLedgerHiddenState);
  setupLedgerHiddenToggle();
  syncLedgerHiddenState();
  setupInlineEdits();
  setupRegistryImageDropzones();
  setupMemoAttachmentDropzones();
  setupQuickActions();
  setupDeadlineEditor();
  setupFileFilters();
  setupFilePreview();
  setupDocTypeUpdates();
  setupHistoryOrdering();
  setupHistoryMerging();
  refreshSummary();
  setupAuditFilters();
  setupAuditUndoHandler();
  refreshAuditLog();
  const auditBtn = document.getElementById("refreshAuditBtn");
  if (auditBtn) {
   auditBtn.addEventListener("click", () => {
    refreshAuditLog();
   });
  }

  // ──  Reset ──
  updateDdayChips();
  setupClipboardCopy();
  highlightOverdueRows();
  setupNavScrollIndicators();
  applyInitialQueryHighlights();
  setupRelatedApplicationPrompt();
  maybeRunNoticeSendSemiClosePrompt();
  const sectionInitializers = {
   "sec-files": () => {
    setupFileFilters();
    setupFilePreview();
    setupDocTypeUpdates();
    if (CASE_VIEW.canEditCase) {
     setupFileDropZones();
     setupDraggableFileRows();
    }
   },
   "sec-history": () => {
    setupHistoryOrdering();
    setupHistoryMerging();
   },
   "sec-cost": () => {
    setupFinanceFormBindings();
   },
   "sec-deadlines": () => {
    updateDdayChips();
    applyInitialQueryHighlights();
   },
   "sec-memo": () => {
    setupMemoAttachmentDropzones();
   },
  };
  document.body.addEventListener("htmx:afterSwap", (e) => {
   const target = e.target;
   if (!target || !(target instanceof HTMLElement)) return;
   if (target.hasAttribute("hx-get")) {
    target.dataset.lazyLoaded = "1";
    target.dataset.lazyLoadError = "0";
    target.dataset.lazyLoading = "0";
    target.removeAttribute("aria-busy");
   }
   const sectionId = (target.id || "").trim();
   const initialize = sectionInitializers[sectionId];
   setupLazySectionViewportFallbacks(target);
   if (sectionId.startsWith("sec-")) setupCaseSectionRegistry();
   if (!initialize) return;
   initialize();
  });

  document.body.addEventListener("htmx:responseError", (e) => {
   const detail = e && e.detail ? e.detail : null;
   const target = (detail && detail.target) || null;
   if (!target || !(target instanceof HTMLElement)) return;
   if (!target.hasAttribute("hx-get")) return;
   target.dataset.lazyLoadError = "1";
   target.dataset.lazyLoading = "0";
   target.removeAttribute("aria-busy");
   scheduleLazyPanelFallback(target);
  });

  document.addEventListener("click", (e) => {
   const retryBtn = e.target && e.target.closest
    ? e.target.closest("[data-retry-lazy-section]")
    : null;
   if (!retryBtn) return;
   const panelId = (retryBtn.getAttribute("data-retry-lazy-section") || "").trim();
   if (!panelId) return;
   const panel = document.getElementById(panelId);
   if (!panel) return;
   e.preventDefault();
   loadLazyPanelSection(panel, { force: true });
  });

  if (CASE_VIEW.canEditCase) {
   setupFileDropZones();
   setupDraggableFileRows();
  }

 })();
