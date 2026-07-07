import { escapeHtml } from "./utils.js";

export const CASE_VIEW_CONFIG = window.CASE_VIEW_CONFIG || {};

const CASE_VIEW_ROOT = document.getElementById("caseViewRoot");
const caseId = (CASE_VIEW_CONFIG.caseId || CASE_VIEW_ROOT?.dataset.caseId || "").toString();
const caseViewScopeId = caseId || "global";

export const CASE_VIEW = {
 caseId,
 csrfToken:
  document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") ||
  CASE_VIEW_CONFIG.csrfToken ||
  "",
 fmFolderId: CASE_VIEW_CONFIG.fmFolderId || "",
 // UI gating only; server-side permission checks still apply to every write endpoint.
 canEditCase: !!(CASE_VIEW_CONFIG.canEditCase ?? (CASE_VIEW_ROOT?.dataset.canEditCase === "1")),
 canInvoice: !!(CASE_VIEW_CONFIG.canInvoice ?? (CASE_VIEW_ROOT?.dataset.canInvoice === "1")),
};
CASE_VIEW.canEditMode = CASE_VIEW.canEditCase || CASE_VIEW.canInvoice;

export const STAFF_USER_MAP = (CASE_VIEW_CONFIG.staffUsers || []).reduce((acc, user) => {
 if (!user) return acc;
 const key = String(user.id ?? "");
 if (key) acc[key] = user.username || "";
 return acc;
}, {});

export function ipmAlert(message, opts) {
 try {
  if (window.AppAlert) return window.AppAlert(message, opts);
 } catch (e) {}
 try {
  window.alert(String(message ?? ""));
 } catch (e) {}
 return Promise.resolve();
}

export function ipmConfirm(message) {
 try {
  if (window.AppConfirm) return window.AppConfirm(message);
 } catch (e) {}
 try {
  return Promise.resolve(window.confirm(String(message ?? "")));
 } catch (e) {}
 return Promise.resolve(false);
}

export function ipmPrompt(message, defaultValue, opts) {
 try {
  if (window.AppPrompt) return window.AppPrompt(message, defaultValue, opts);
 } catch (e) {}
 try {
  return Promise.resolve(window.prompt(String(message ?? ""), String(defaultValue ?? "")));
 } catch (e) {}
 return Promise.resolve(null);
}

export function getTodayIsoDate() {
 try {
  if (window.AppDate && typeof window.AppDate.formatYmd === "function") {
   return window.AppDate.formatYmd(new Date());
  }
 } catch (e) {}
 const now = new Date();
 const pad2 = (n) => String(n).padStart(2, "0");
 return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
}

export function setInputValueIfEmpty(selector, value) {
 const input = document.querySelector(selector);
 if (input && !input.value) {
  input.value = value;
 }
}

export function getCurrentHashAnchor() {
 return (window.location.hash || "").replace(/^#/, "").trim();
}

export function bindElementEventOnce(element, marker, eventName, handler) {
 if (!element) return;
 const attr = `data-case-view-${marker}-bound`;
 if (element.getAttribute(attr) === "1") return;
 element.setAttribute(attr, "1");
 element.addEventListener(eventName, handler);
}

function caseStorageKey(key) {
 return `caseView.${key}.${caseViewScopeId}`;
}

export const PreferencesManager = {
 get(storage, key, fallback = "") {
  try {
   const value = storage.getItem(key);
   return value === null || typeof value === "undefined" ? fallback : value;
  } catch (e) {
   return fallback;
  }
 },
 set(storage, key, value) {
  try {
   storage.setItem(key, String(value ?? ""));
  } catch (e) {}
 },
 getFlag(storage, key) {
  const value = String(this.get(storage, key, "") || "").toLowerCase();
  if (value === "1" || value === "true") return true;
  if (value === "0" || value === "false") return false;
  return null;
 },
 setFlag(storage, key, enabled) {
  this.set(storage, key, enabled ? "1" : "0");
 },
 getCasePref(key, fallback = "") {
  return this.get(window.localStorage, caseStorageKey(key), fallback);
 },
 setCasePref(key, value) {
  this.set(window.localStorage, caseStorageKey(key), value);
 },
};

export function setStoredCasePref(key, value) {
 PreferencesManager.setCasePref(key, value);
}

export function getStoredCasePref(key) {
 return PreferencesManager.getCasePref(key, "");
}

export async function confirmAction(message) {
 return await ipmConfirm(message);
}

export function showToast(message, type = "success") {
 const container = document.getElementById("case-toast-container");
 if (!container || !window.bootstrap) return;
 const bs = window.bootstrap;
 if (!bs.Toast) return;
 const safeMessage = escapeHtml(String(message ?? ""));
 const toastId = `toast-${Date.now()}`;
 const bgClass = type === "success" ? "bg-success" : type === "danger" ? "bg-danger" : "bg-warning";
 const icon = type === "success" ? "check-circle" : type === "danger" ? "exclamation-circle" : "exclamation-triangle";
 const html = `
  <div id="${toastId}" class="toast align-items-center text-white ${bgClass} border-0 shadow" role="alert" aria-live="assertive" aria-atomic="true">
   <div class="d-flex">
    <div class="toast-body">
     <i class="bi bi-${icon} me-2"></i>${safeMessage}
    </div>
    <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
   </div>
  </div>
 `;
 container.insertAdjacentHTML("beforeend", html);
 const toastEl = document.getElementById(toastId);
 const toast = new bs.Toast(toastEl, { delay: 3500 });
 toast.show();
 toastEl.addEventListener("hidden.bs.toast", () => toastEl.remove());
}

export function showUndoToast(message, auditId, onUndo) {
 const container = document.getElementById("case-toast-container");
 if (!container || !window.bootstrap) return;
 const bs = window.bootstrap;
 if (!bs.Toast) return;
 const toastId = `toast-${Date.now()}`;
 const safeMessage = escapeHtml(String(message ?? ""));
 const html = `
  <div id="${toastId}" class="toast align-items-center text-white bg-success border-0 shadow" role="alert" aria-live="assertive" aria-atomic="true">
   <div class="d-flex">
    <div class="toast-body">
     ${safeMessage} <button type="button" class="btn btn-link text-white p-0 ms-2" data-undo-id="${auditId}">Undo</button>
    </div>
    <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
   </div>
  </div>
 `;
 container.insertAdjacentHTML("beforeend", html);
 const toastEl = document.getElementById(toastId);
 const toast = new bs.Toast(toastEl, { delay: 6000 });
 toast.show();
 const undoBtn = toastEl.querySelector(`[data-undo-id="${auditId}"]`);
 if (undoBtn) {
  undoBtn.addEventListener("click", async (e) => {
   e.preventDefault();
   undoBtn.disabled = true;
   try {
    await onUndo();
    showToast("Undo Done", "success");
    toast.hide();
   } catch (err) {
    showToast(err.message || "Undo ", "danger");
    undoBtn.disabled = false;
   }
  });
 }
 toastEl.addEventListener("hidden.bs.toast", () => toastEl.remove());
}

async function parseJsonSafe(response) {
 try {
  return await response.json();
 } catch (e) {
  return null;
 }
}

function getApiErrorMessage(data, fallback = " Error .") {
 return (data && (data.message || data.error)) || fallback;
}

export async function apiJson(url, method = "GET", body = null, extraHeaders = null) {
 const headers = Object.assign(
  { "Content-Type": "application/json", "X-CSRFToken": CASE_VIEW.csrfToken },
  extraHeaders || {}
 );
 const options = { method, headers, credentials: "same-origin" };
 if (body) options.body = JSON.stringify(body);
 const response = await fetch(url, options);
 const data = await parseJsonSafe(response);
 if (!response.ok) {
  throw new Error(getApiErrorMessage(data));
 }
 return data;
}

export async function apiForm(url, formData, method = "POST") {
 const response = await fetch(url, {
  method,
  headers: { "X-CSRFToken": CASE_VIEW.csrfToken },
  body: formData,
 });
 const data = await parseJsonSafe(response);
 if (!response.ok) {
  throw new Error(getApiErrorMessage(data));
 }
 return data;
}

function sectionRefreshUrl(sectionKey, target) {
 const hxGet = (target?.getAttribute("hx-get") || "").trim();
 if (hxGet) return hxGet;
 return `/case/${encodeURIComponent(CASE_VIEW.caseId)}/section/${encodeURIComponent(sectionKey)}`;
}

export async function refreshCaseSection(sectionKey, targetId) {
 if (!CASE_VIEW.caseId) return false;
 const target = document.getElementById(targetId);
 if (!target) return false;
 const url = sectionRefreshUrl(sectionKey, target);
 const swapMode = (target.getAttribute("hx-swap") || "").toLowerCase();
 const wasActive = target.classList.contains("is-active");
 const response = await fetch(url, {
  method: "GET",
  headers: { "HX-Request": "true" },
  credentials: "same-origin",
 });
 if (!response.ok) {
  throw new Error("Section ? ");
 }
 const html = await response.text();
 if (swapMode.includes("outerhtml")) {
  target.insertAdjacentHTML("afterend", html);
  const newTarget = target.nextElementSibling;
  target.remove();
  if (newTarget) {
   if (wasActive && newTarget.classList) newTarget.classList.add("is-active");
   newTarget.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true }));
  }
 } else {
  target.innerHTML = html;
  target.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true }));
 }
 return true;
}
