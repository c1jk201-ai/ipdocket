(function () {
 "use strict";

 const MAX_SNIPPET = 200;

 function onReady(fn) {
  if (document.readyState === "loading") {
   document.addEventListener("DOMContentLoaded", fn, { once: true });
   return;
  }
  fn();
 }

 function readJsonScript(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  const raw = (el.textContent || "").trim();
  if (!raw) return fallback;
  try {
   return JSON.parse(raw);
  } catch (e) {
   console.error(`[ipm_core] invalid JSON in #${id}`, e);
   return fallback;
  }
 }

 const localeConfig = readJsonScript("app-locale-config", {});
 const APP_LOCALE = localeConfig.locale || "en-US";
 const APP_TIME_ZONE = localeConfig.timeZone || "America/New_York";
 window.AppLocale = Object.assign({}, window.AppLocale || {}, {
  locale: APP_LOCALE,
  timeZone: APP_TIME_ZONE,
  dateFormat: localeConfig.dateFormat || "MM/DD/YYYY",
  dateTimeFormat: localeConfig.dateTimeFormat || "MM/DD/YYYY h:mm A",
 });

 window.esc = window.esc || function (value) {
  return String(value == null ? "" : value)
   .replaceAll("&", "&amp;")
   .replaceAll("<", "&lt;")
   .replaceAll(">", "&gt;")
   .replaceAll('"', "&quot;")
   .replaceAll("'", "&#39;");
 };

 const formatYmd = (value) => {
  if (!value) return "";
  const dt = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(dt.getTime())) return "";
  const pad2 = (n) => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${pad2(dt.getMonth() + 1)}-${pad2(dt.getDate())}`;
 };

 const parseYmd = (value) => {
  if (!value) return null;
  const raw = String(value).trim();
  const m = raw.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]) - 1;
  const day = Number(m[3]);
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) {
   return null;
  }
  return new Date(year, month, day, 12, 0, 0);
 };

 const formatDisplayDate = (value) => {
  if (!value) return "";
  const raw = String(value || "").trim();
  const ymd = raw.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (ymd) return `${ymd[2]}/${ymd[3]}/${ymd[1]}`;
  const dt = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(dt.getTime())) return "";
  return dt.toLocaleDateString(APP_LOCALE, {
   timeZone: APP_TIME_ZONE,
   year: "numeric",
   month: "2-digit",
   day: "2-digit",
  });
 };

 const formatDisplayDateTime = (value, options) => {
  if (!value) return "";
  const opts = options || {};
  const dt = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(dt.getTime())) return "";
  const formatterOptions = {
   timeZone: APP_TIME_ZONE,
   year: "numeric",
   month: "2-digit",
   day: "2-digit",
   hour: "2-digit",
   minute: "2-digit",
  };
  if (opts.seconds) formatterOptions.second = "2-digit";
  if (opts.timeZoneName) formatterOptions.timeZoneName = "short";
  return dt.toLocaleString(APP_LOCALE, formatterOptions);
 };

 window.AppDate = window.AppDate || {};
 if (typeof window.AppDate.formatYmd !== "function") {
  window.AppDate.formatYmd = formatYmd;
 }
 if (typeof window.AppDate.parseYmd !== "function") {
  window.AppDate.parseYmd = parseYmd;
 }
 if (typeof window.AppDate.formatDisplayDate !== "function") {
  window.AppDate.formatDisplayDate = formatDisplayDate;
 }
 if (typeof window.AppDate.formatDisplayDateTime !== "function") {
  window.AppDate.formatDisplayDateTime = formatDisplayDateTime;
 }

 const isStrictYmdDate = (value) => {
  const raw = String(value || "").trim();
  const m = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return false;
  const year = Number(m[1]);
  const month = Number(m[2]);
  const day = Number(m[3]);
  if (!Number.isInteger(year) || year < 1 || year> 9999) return false;
  if (!Number.isInteger(month) || month < 1 || month> 12) return false;
  const monthLengths = [31, year % 400 === 0 || (year % 4 === 0 && year % 100 !== 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return Number.isInteger(day) && day>= 1 && day <= monthLengths[month - 1];
 };

 if (typeof window.AppDate.isStrictYmdDate !== "function") {
  window.AppDate.isStrictYmdDate = isStrictYmdDate;
 }

 const STRICT_DATE_SELECTOR = 'input[type="date"], input[data-ipm-date-input="1"]';
 const STRICT_DATE_PLACEHOLDER = "YYYY-MM-DD";

 function ipmPaginationItems(currentPage, totalPages) {
  const items = [];
  if (totalPages <= 10) {
   for (let page = 1; page <= totalPages; page += 1) items.push(page);
   return items;
  }
  if (currentPage <= 6) {
   for (let page = 1; page <= 8; page += 1) items.push(page);
   items.push("ellipsis-right", totalPages);
   return items;
  }
  if (currentPage>= totalPages - 5) {
   items.push(1, "ellipsis-left");
   for (let page = totalPages - 7; page <= totalPages; page += 1) items.push(page);
   return items;
  }
  items.push(1, "ellipsis-left");
  for (let page = currentPage - 3; page <= currentPage + 3; page += 1) items.push(page);
  items.push("ellipsis-right", totalPages);
  return items;
 }

 function setupPaginationJumpForm(form, options) {
  if (!form || !form.querySelector) return;
  const opts = options || {};
  if (form.dataset.paginationJumpBound === "1" && !opts.force) return;
  form.dataset.paginationJumpBound = "1";

  const input = form.querySelector("[data-pagination-jump-input]");
  if (!input) return;
  const onPageChange = typeof opts.onPageChange === "function" ? opts.onPageChange : null;

  input.addEventListener("input", () => {
   const digitsOnly = input.value.replace(/[^0-9]/g, "");
   if (input.value !== digitsOnly) input.value = digitsOnly;
   input.setCustomValidity("");
  });

  form.addEventListener("submit", (event) => {
   const totalPages = Math.max(1, parseInt(input.dataset.totalPages || "1", 10) || 1);
   const targetPage = parseInt(input.value || "", 10);
   if (!Number.isInteger(targetPage) || targetPage < 1 || targetPage> totalPages) {
    event.preventDefault();
    input.setCustomValidity(`1 ${totalPages} page enter.`);
    input.reportValidity();
    return;
   }
   input.value = String(targetPage);
   if (onPageChange) {
    event.preventDefault();
    onPageChange(targetPage);
   }
  });
 }

 function bindPaginationJumpForms(root) {
  const scope = root && root.querySelectorAll ? root : document;
  scope
   .querySelectorAll("[data-pagination-jump-form]")
   .forEach((form) => setupPaginationJumpForm(form));
 }

 function renderAppPagination(container, options) {
  const root = typeof container === "string" ? document.getElementById(container) : container;
  if (!root) return;
  const opts = options || {};
  const totalPages = Math.max(1, parseInt(opts.totalPages || opts.pages || 1, 10) || 1);
  const currentPage = Math.min(
   totalPages,
   Math.max(1, parseInt(opts.page || opts.currentPage || 1, 10) || 1)
  );
  const onPageChange = typeof opts.onPageChange === "function" ? opts.onPageChange : null;
  const ariaLabel = opts.ariaLabel || "Pagination";

  const pageControl = (page, label, className, aria, disabled) => {
   if (disabled) {
    return `<span class="${className} is-disabled" aria-label="${window.esc(aria)}" aria-disabled="true">${label}</span>`;
   }
   return `<button class="${className}" type="button" data-page="${page}" aria-label="${window.esc(aria)}">${label}</button>`;
  };

  const pageNumbers = ipmPaginationItems(currentPage, totalPages)
   .map((item) => {
    if (typeof item === "string") {
     return '<span class="app-pagination__ellipsis" aria-hidden="true">…</span>';
    }
    if (item === currentPage) {
     return `<span class="app-pagination__button app-pagination__button--page is-current" aria-label="Current ${item}page" aria-current="page">${item}</span>`;
    }
    return pageControl(
     item,
     String(item),
     "app-pagination__button app-pagination__button--page",
     `${item}page Go`,
     false
    );
   })
   .join("");

  root.classList.add("app-pagination");
  root.setAttribute("role", "navigation");
  root.setAttribute("aria-label", ariaLabel);
  root.innerHTML = `
   <div class="app-pagination__main">
    ${pageControl(
     currentPage - 1,
     "&lsaquo; Previous",
     "app-pagination__button app-pagination__button--edge",
     "Go to previous page",
     currentPage <= 1
    )}
    <div class="app-pagination__numbers" aria-label="Page number">${pageNumbers}</div>
    ${pageControl(
     currentPage + 1,
     "Next &rsaquo;",
     "app-pagination__button app-pagination__button--edge",
     "Go to next page",
     currentPage>= totalPages
    )}
   </div>
   <form class="app-pagination__jump" data-pagination-jump-form>
    <input
     class="app-pagination__input"
     type="text"
     value="${currentPage}"
     inputmode="numeric"
     pattern="[0-9]*"
     autocomplete="off"
     data-pagination-jump-input
     data-total-pages="${totalPages}"
     aria-label="Page number to go to"
   >
    <span class="app-pagination__total" aria-label="Total ${totalPages}page">/ ${totalPages}</span>
    <button class="app-pagination__go" type="submit">Go</button>
   </form>
  `;

  const goToPage = (targetPage) => {
   if (!Number.isInteger(targetPage) || targetPage < 1 || targetPage> totalPages) return;
   if (onPageChange) onPageChange(targetPage);
  };

  root.querySelectorAll("[data-page]").forEach((control) => {
   control.addEventListener("click", () => {
    goToPage(parseInt(control.dataset.page || "", 10));
   });
  });

  const form = root.querySelector("[data-pagination-jump-form]");
  setupPaginationJumpForm(form, { onPageChange: goToPage });
 }

 const queuedPaginationRenders = Array.isArray(window.AppPagination && window.AppPagination._queue)
  ? window.AppPagination._queue.slice()
  : [];
 window.AppPagination = window.AppPagination || {};
 window.AppPagination.items = ipmPaginationItems;
 window.AppPagination.render = renderAppPagination;
 window.AppPagination._queue = [];
 queuedPaginationRenders.forEach((entry) => {
  if (!Array.isArray(entry)) return;
  renderAppPagination(entry[0], entry[1]);
 });
 document.dispatchEvent(new CustomEvent("ipm:pagination-ready"));

 function isStrictDateControl(input) {
  return !!(
   input &&
   input.matches &&
   input.matches(STRICT_DATE_SELECTOR)
  );
 }

 function normalizeStrictDateInput(input) {
  if (!isStrictDateControl(input)) return;
  input.dataset.ipmDateInput = "1";
  input.setAttribute("lang", "en");
  if (!input.getAttribute("placeholder")) input.setAttribute("placeholder", STRICT_DATE_PLACEHOLDER);
  if (!input.getAttribute("inputmode")) input.setAttribute("inputmode", "numeric");
  if (!input.getAttribute("autocomplete")) input.setAttribute("autocomplete", "off");
  if (!input.getAttribute("pattern")) input.setAttribute("pattern", "\\d{4}-\\d{2}-\\d{2}");
  if (String(input.type || "").toLowerCase() === "date") {
   const currentValue = input.value;
   input.type = "text";
   input.value = currentValue;
  }
 }

 function validateStrictDateInput(input) {
  if (!isStrictDateControl(input)) return true;
  const raw = String(input.value || "").trim();
  if (!raw) {
   input.setCustomValidity("");
   return true;
  }
  if (!isStrictYmdDate(raw)) {
   input.setCustomValidity("Enter a valid date.");
   return false;
  }
  input.setCustomValidity("");
  return true;
 }

 function setupStrictDateInput(input) {
  if (!input) return;
  if (input.dataset.ipmStrictDate === "1") {
   setupStrictDatePicker(input);
   return;
  }
  normalizeStrictDateInput(input);
  input.dataset.ipmStrictDate = "1";
  if (!input.getAttribute("min")) input.setAttribute("min", "0001-01-01");
  if (!input.getAttribute("max")) input.setAttribute("max", "9999-12-31");
  input.addEventListener("input", () => validateStrictDateInput(input));
  input.addEventListener("change", () => validateStrictDateInput(input));
  validateStrictDateInput(input);
  setupStrictDatePicker(input);
 }

 function setupStrictDatePicker(input) {
  if (!isStrictDateControl(input)) return;
  if (input.dataset.ipmDatepicker === "1") return;
  if (typeof window.flatpickr !== "function") return;
  input.dataset.ipmDatepicker = "1";
  try {
   window.flatpickr(input, {
    dateFormat: "Y-m-d",
    allowInput: true,
    allowInvalidPreload: true,
    clickOpens: true,
    disableMobile: true,
    monthSelectorType: "dropdown",
    onChange: () => validateStrictDateInput(input),
    onClose: () => validateStrictDateInput(input),
   });
  } catch (e) {
   input.dataset.ipmDatepicker = "0";
   console.error("[ipm_core] datepicker init failed", e);
  }
 }

 function initStrictDateInputs(root) {
  const scope = root && root.querySelectorAll ? root : document;
  if (isStrictDateControl(scope)) setupStrictDateInput(scope);
  scope.querySelectorAll(STRICT_DATE_SELECTOR).forEach(setupStrictDateInput);
 }

 window.AppDate.initDateInputs = initStrictDateInputs;

 function initStrictDateMutationObserver() {
  if (document.documentElement.dataset.ipmStrictDateObserver === "1") return;
  if (typeof MutationObserver !== "function") return;
  document.documentElement.dataset.ipmStrictDateObserver = "1";

  let queuedRoot = null;
  let queued = false;
  const queueScan = (root) => {
   queuedRoot = root || queuedRoot || document;
   if (queued) return;
   queued = true;
   const run = () => {
    const targetRoot = queuedRoot || document;
    queuedRoot = null;
    queued = false;
    initStrictDateInputs(targetRoot);
   };
   if (typeof window.requestAnimationFrame === "function") {
    window.requestAnimationFrame(run);
   } else {
    window.setTimeout(run, 0);
   }
  };

  const observer = new MutationObserver((mutations) => {
   for (const mutation of mutations) {
    if (mutation.type === "attributes") {
     const target = mutation.target;
     if (target instanceof Element && isStrictDateControl(target)) {
      queueScan(target);
      return;
     }
     continue;
    }
    for (const node of Array.from(mutation.addedNodes || [])) {
     if (!(node instanceof Element)) continue;
     if (isStrictDateControl(node) || node.querySelector(STRICT_DATE_SELECTOR)) {
      queueScan(node);
      return;
     }
    }
   }
  });

  onReady(() => {
   if (!document.body) return;
   observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["type", "data-ipm-date-input"],
   });
  });
 }

 function initStrictDateSubmitGuard() {
  if (document.documentElement.dataset.ipmStrictDateSubmit === "1") return;
  document.documentElement.dataset.ipmStrictDateSubmit = "1";
  document.addEventListener(
   "submit",
   (event) => {
    const form = event.target;
    if (!form || !form.querySelectorAll) return;
    initStrictDateInputs(form);
    const invalid = Array.from(form.querySelectorAll(STRICT_DATE_SELECTOR)).find((input) => {
     const strictOk = validateStrictDateInput(input);
     return !strictOk || (input.value && input.validity && !input.validity.valid);
    });
    if (!invalid) return;
    event.preventDefault();
    invalid.reportValidity();
    invalid.focus();
   },
   true
  );
  document.body?.addEventListener("htmx:afterSwap", (event) => {
   initStrictDateInputs(event.target || document);
  });
 }

 const normalizeContentType = (ct) => (ct || "").toLowerCase();
 const isJsonContentType = (ct) =>
  ct.includes("application/json") || ct.includes("application/problem+json");

 const isAuthPageUrl = (rawUrl) => {
  try {
   const url = new URL(rawUrl, window.location.href);
   const path = (url.pathname || "").toLowerCase();
   return (
    path === "/login" ||
    path === "/auth/login" ||
    path === "/auth/test-login" ||
    path.startsWith("/login/") ||
    path.startsWith("/auth/login/") ||
    path.startsWith("/auth/test-login/")
   );
  } catch (e) {
   return false;
  }
 };

 const readSnippet = async (res) => {
  try {
   const text = await res.clone().text();
   return text.slice(0, MAX_SNIPPET);
  } catch (e) {
   return "";
  }
 };

 const handleAuthRedirect = (res) => {
  if (!res) return;
  if (res.status === 401) {
   try {
    window.location.reload();
   } catch (e) {}
   return;
  }
  if (res.redirected && isAuthPageUrl(res.url)) {
   try {
    window.location.assign(res.url);
   } catch (e) {}
  }
 };

 const parseJsonResponse = async (res) => {
  handleAuthRedirect(res);
  if (!res) throw new Error("Empty response");
  if (res.status === 204 || res.status === 205) return null;

  if (!res.ok) {
   const body = await readSnippet(res);
   throw new Error(`HTTP ${res.status}: ${body}`);
  }

  const ct = normalizeContentType(res.headers?.get("content-type"));
  if (ct && !isJsonContentType(ct)) {
   const body = await readSnippet(res);
   throw new Error(`Non-JSON response: ${body}`);
  }

  try {
   return await res.json();
  } catch (err) {
   const body = await readSnippet(res);
   throw new Error(`Non-JSON response: ${body}`);
  }
 };

 const fetchJson = async (url, opts = {}) => {
  const res = await fetch(url, opts);
  return parseJsonResponse(res);
 };

 const wrapFetch = () => {
  if (typeof window.fetch !== "function") return;
  if (window.fetch._ipmWrapped) return;
  const origFetch = window.fetch;
  const wrapped = function (...args) {
   return origFetch.apply(this, args).then((res) => {
    handleAuthRedirect(res);
    return res;
   });
  };
  wrapped._ipmWrapped = true;
  wrapped._ipmOriginal = origFetch;
  window.fetch = wrapped;
 };

 window.AppFetch = {
  json: fetchJson,
  parseJsonResponse,
  handleAuthRedirect,
 };

 function buildLinks(rawLinks) {
  const links = Object.assign({}, rawLinks || {});
  links.buildCaseUrl = function (caseId) {
   const raw = String(caseId || "").trim();
   if (!raw) return "";
   return this.caseDetailBase.replace("__CASE__", encodeURIComponent(raw));
  };
  links.buildCaseListUrl = function (query) {
   const raw = String(query || "").trim();
   if (!raw) return this.caseListBase;
   return `${this.caseListBase}?q=${encodeURIComponent(raw)}`;
  };
  links.buildWorkflowUrl = function (workflowId) {
   const raw = String(workflowId || "").trim();
   if (!raw) return "";
   return this.workflowDetailBase.replace(/0$/, encodeURIComponent(raw));
  };
  links.buildWorklogUrl = function (params) {
   const q = new URLSearchParams(params || {});
   const qs = q.toString();
   return qs ? `${this.worklogBase}?${qs}` : this.worklogBase;
  };
  links.buildCrmSearchUrl = function (query) {
   const raw = String(query || "").trim();
   if (!raw) return this.crmClientsBase;
   return `${this.crmClientsBase}?q=${encodeURIComponent(raw)}`;
  };
  links.buildInvoiceClientUrl = function (clientId) {
   const raw = String(clientId || "").trim();
   if (!raw) return "";
   return this.invoiceClientBase.replace(/0$/, encodeURIComponent(raw));
  };
  links.buildDocketUrl = function (docketId) {
   const raw = String(docketId || "").trim();
   if (!raw || !this.docketDetailBase) return "";
   return this.docketDetailBase.replace("__DOCKET__", encodeURIComponent(raw));
  };
  links.buildInvoiceUrl = function (invoiceId) {
   const raw = String(invoiceId || "").trim();
   if (!raw || !this.invoiceViewBase) return "";
   return this.invoiceViewBase.replace(/0$/, encodeURIComponent(raw));
  };
  links.buildCalendarUrl = function (dateValue) {
   const raw = String(dateValue || "").trim();
   if (!raw) return "";
   return `${this.deadlineCalendarBase}?date=${encodeURIComponent(raw)}`;
  };
  links.buildRenewalCalendarUrl = function (dateValue) {
   const raw = String(dateValue || "").trim();
   if (!raw) return "";
   return `${this.renewalCalendarBase}?date=${encodeURIComponent(raw)}`;
  };
  return links;
 }

 function applyPrefill(prefill) {
  if (!prefill || typeof prefill !== "object") return;
  Object.keys(prefill).forEach(function (name) {
   const value = prefill[name];
   if (value === null || value === undefined) return;
   const elements = document.getElementsByName(name);
   Array.from(elements).forEach(function (el) {
    if (el.type === "file") return;
    if (el.type === "checkbox") {
     const normalized = String(value).toUpperCase();
     el.checked =
      normalized === "Y" ||
      normalized === "YES" ||
      normalized === "TRUE" ||
      normalized === "1" ||
      String(el.value) === String(value);
     return;
    }
    if (el.type === "radio") {
     if (String(el.value) === String(value)) {
      el.checked = true;
     }
     return;
    }
    el.value = value;
   });
  });
 }

 function applyDefaultCaseDates() {
  const today = new Date();
  const plus30 = new Date(today);
  plus30.setMonth(plus30.getMonth() + 1);
  const setDefaultDate = (selector, dateVal) => {
   document.querySelectorAll(selector).forEach((el) => {
    if (!el.value) {
     el.value = dateVal;
    }
   });
  };
  setDefaultDate("input[name='custom_field_commission_date']", window.AppDate.formatYmd(today));
  setDefaultDate("input[name='custom_field_filing_deadline']", window.AppDate.formatYmd(plus30));
  document.querySelectorAll("select[name='custom_field_filing_deadline_type']").forEach((el) => {
   if (!el.value) {
    el.value = "INTERNAL";
   }
  });
 }

 function initDrilldownState() {
  const STATE_PREFIX = "app.drilldown.state:";
  const MAX_AGE_MS = 2 * 60 * 60 * 1000;
  const MAX_SCROLLERS = 40;
  const MAX_FORMS = 20;
  const MAX_FIELDS = 240;

  const pageKey = () => `${window.location.pathname}${window.location.search}`;

  const storageKey = (key) => `${STATE_PREFIX}${key || pageKey()}`;

  try {
   if ("scrollRestoration" in history) {
    history.scrollRestoration = "manual";
   }
  } catch (e) {}

  const navType = () => {
   try {
    const nav = performance.getEntriesByType("navigation");
    return nav && nav[0] ? nav[0].type || "" : "";
   } catch (e) {
    return "";
   }
  };

  const selectorFor = (el) => {
   if (!el || !el.id) return "";
   if (window.CSS && typeof window.CSS.escape === "function") {
    return `#${window.CSS.escape(el.id)}`;
   }
   return `#${String(el.id).replace(/(["\\#.:[\],>+~*^$|= ])/g, "\\$1")}`;
  };

  const isRestorableControl = (el) => {
   if (!el || !el.name || el.disabled) return false;
   const tag = String(el.tagName || "").toLowerCase();
   const type = String(el.type || "").toLowerCase();
   if (tag !== "input" && tag !== "select" && tag !== "textarea") return false;
   if (["button", "submit", "reset", "file", "password", "hidden"].includes(type)) return false;
   return true;
  };

  const readControl = (el) => {
   const type = String(el.type || "").toLowerCase();
   if (type === "radio") return el.checked ? { radioValue: el.value } : {};
   if (type === "checkbox") return { checkedValues: el.checked ? [el.value] : [] };
   if (el.tagName && String(el.tagName).toLowerCase() === "select" && el.multiple) {
    return { value: Array.from(el.selectedOptions || []).map((opt) => opt.value) };
   }
   return { value: el.value };
  };

  const writeControl = (el, data) => {
   if (!el || !data || typeof data !== "object") return;
   const type = String(el.type || "").toLowerCase();
   if (type === "radio" && Object.prototype.hasOwnProperty.call(data, "radioValue")) {
    el.checked = String(el.value) === String(data.radioValue);
    return;
   }
   if (type === "checkbox" || type === "radio") {
    if (Array.isArray(data.checkedValues)) {
     el.checked = data.checkedValues.map(String).includes(String(el.value));
     return;
    }
    if (Object.prototype.hasOwnProperty.call(data, "checked")) {
     el.checked = !!data.checked;
    }
    return;
   }
   if (el.tagName && String(el.tagName).toLowerCase() === "select" && el.multiple && Array.isArray(data.value)) {
    const selected = new Set(data.value.map(String));
    Array.from(el.options || []).forEach((opt) => {
     opt.selected = selected.has(String(opt.value));
    });
    return;
   }
   if (Object.prototype.hasOwnProperty.call(data, "value")) {
    el.value = data.value == null ? "" : String(data.value);
   }
  };

  const collectForms = () => {
   const forms = [];
   let fieldCount = 0;
   Array.from(document.forms || []).slice(0, MAX_FORMS).forEach((form, formIndex) => {
    const controls = {};
    Array.from(form.elements || []).forEach((el) => {
     if (fieldCount>= MAX_FIELDS || !isRestorableControl(el)) return;
     const key = el.name || el.id;
     if (!key) return;
     const type = String(el.type || "").toLowerCase();
     if (type === "checkbox" && controls[key]) {
      if (!Array.isArray(controls[key].checkedValues)) {
       controls[key] = { checkedValues: [] };
      }
      if (el.checked) controls[key].checkedValues.push(el.value);
     } else if (type === "radio" && controls[key]) {
      if (el.checked) controls[key] = { radioValue: el.value };
     } else {
      controls[key] = readControl(el);
     }
     fieldCount += 1;
    });
    if (Object.keys(controls).length) {
     forms.push({
      selector: selectorFor(form),
      index: formIndex,
      controls,
     });
    }
   });
   return forms;
  };

  const collectScrollers = () => {
   const out = [];
   Array.from(document.querySelectorAll("[id]")).some((el) => {
    if (!el || el === document.documentElement || el === document.body) return false;
    const top = Number(el.scrollTop || 0);
    const left = Number(el.scrollLeft || 0);
    if (top <= 0 && left <= 0) return false;
    if (el.scrollHeight <= el.clientHeight + 8 && el.scrollWidth <= el.clientWidth + 8) return false;
    out.push({ selector: selectorFor(el), top, left });
    return out.length>= MAX_SCROLLERS;
   });
   return out;
  };

  const snapshot = () => ({
   version: 1,
   url: pageKey(),
   savedAt: Date.now(),
   scrollX: Number(window.scrollX || window.pageXOffset || 0),
   scrollY: Number(window.scrollY || window.pageYOffset || 0),
   hash: window.location.hash || "",
   forms: collectForms(),
   scrollers: collectScrollers(),
   activeSelector: selectorFor(document.activeElement),
  });

  const save = () => {
   const key = pageKey();
   try {
    sessionStorage.setItem(storageKey(key), JSON.stringify(snapshot()));
   } catch (e) {}
   try {
    const current = history.state && typeof history.state === "object" ? history.state : {};
    history.replaceState(
     Object.assign({}, current, {
      ipmDrilldownRestore: true,
      ipmDrilldownKey: key,
     }),
     "",
     window.location.href
    );
   } catch (e) {}
  };

  const navigate = (url, options) => {
   const href = String(url || "").trim();
   if (!href) return;
   save();
   if (options && options.replace) {
    window.location.replace(href);
    return;
   }
   window.location.assign(href);
  };

  const restoreForms = (state) => {
   if (!state || !Array.isArray(state.forms)) return;
   state.forms.forEach((entry) => {
    const form = entry.selector
     ? document.querySelector(entry.selector)
     : (document.forms || [])[entry.index];
    if (!form || !entry.controls) return;
    Object.keys(entry.controls).forEach((key) => {
     const controls = form.elements ? form.elements[key] : null;
     if (!controls) return;
     if (typeof controls.length === "number" && !controls.tagName) {
      Array.from(controls).forEach((el) => writeControl(el, entry.controls[key]));
      return;
     }
     writeControl(controls, entry.controls[key]);
    });
   });
  };

  const restoreScroll = (state) => {
   if (!state) return;
   const run = () => {
    try {
     (state.scrollers || []).forEach((entry) => {
      if (!entry.selector) return;
      const el = document.querySelector(entry.selector);
      if (!el) return;
      el.scrollTop = Number(entry.top || 0);
      el.scrollLeft = Number(entry.left || 0);
     });
     window.scrollTo(Number(state.scrollX || 0), Number(state.scrollY || 0));
     if (state.activeSelector) {
      const active = document.querySelector(state.activeSelector);
      if (active && typeof active.focus === "function") {
       active.focus({ preventScroll: true });
      }
     }
    } catch (e) {}
   };
   requestAnimationFrame(() => {
    run();
    setTimeout(run, 80);
    setTimeout(run, 300);
   });
  };

  const shouldRestore = (state) => {
   if (!state || state.url !== pageKey()) return false;
   if (!state.savedAt || Date.now() - Number(state.savedAt)> MAX_AGE_MS) return false;
   if (navType() === "reload") return false;
   const historyState = history.state && typeof history.state === "object" ? history.state : {};
   return !!historyState.ipmDrilldownRestore || navType() === "back_forward";
  };

  const restore = () => {
   let state = null;
   try {
    state = JSON.parse(sessionStorage.getItem(storageKey()) || "null");
   } catch (e) {
    state = null;
   }
   if (!shouldRestore(state)) return;
   restoreForms(state);
   restoreScroll(state);
  };

  const isNavigableAnchor = (anchor) => {
   if (!anchor || !anchor.href || anchor.hasAttribute("download")) return false;
   const raw = String(anchor.getAttribute("href") || "").trim();
   if (!raw || raw === "#" || raw.startsWith("#")) return false;
   if (/^(javascript|mailto|tel):/i.test(raw)) return false;
   try {
    const url = new URL(anchor.href, window.location.href);
    if (url.origin !== window.location.origin) return false;
    if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) {
     return false;
    }
    return true;
   } catch (e) {
    return false;
   }
  };

  document.addEventListener(
   "click",
   (event) => {
    const anchor = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!isNavigableAnchor(anchor)) return;
    save();
   },
   true
  );

  document.addEventListener(
   "submit",
   (event) => {
    const form = event.target;
    if (!form || String(form.method || "get").toLowerCase() !== "get") return;
    save();
   },
   true
  );

  window.addEventListener("pagehide", save);
  window.addEventListener("pageshow", (event) => {
   if (event && event.persisted) restore();
  });

  onReady(restore);
  window.AppDrilldown = { save, restore, navigate };
 }

 window.AppLinks = buildLinks(readJsonScript("app-links-config", {}));
 wrapFetch();
 applyPrefill(readJsonScript("app-prefill-data", null));
 initDrilldownState();
 initStrictDateSubmitGuard();
 initStrictDateMutationObserver();
 onReady(() => bindPaginationJumpForms(document));
 document.body?.addEventListener("htmx:afterSwap", (event) => {
  bindPaginationJumpForms(event.target || document);
 });
 onReady(() => initStrictDateInputs(document));
 onReady(applyDefaultCaseDates);
})();
