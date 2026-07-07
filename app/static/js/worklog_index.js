document.addEventListener('DOMContentLoaded', function() {
 // --- Constants & State ---
 const pageConfigEl = document.getElementById('worklog-page-config');
 let pageConfig = {};
 if (pageConfigEl) {
  try {
   pageConfig = JSON.parse(pageConfigEl.textContent || '{}');
  } catch (e) {
   pageConfig = {};
  }
 }
 const CURRENT_USER_ID = Number(pageConfig.currentUserId || 0) || 0;
 const CURRENT_USER_OWNER_VALUE = (pageConfig.currentUserOwnerValue || '').toString();
 const CONFIG = {
  itemsPerPage: 50,
  apiPrefix: '/worklog/api',
  exportMaxRows: 10000,
  crmClientsUrl: (pageConfig.crmClientsUrl || '').toString(),
  crmClientViewTemplate: (pageConfig.crmClientViewTemplate || '').toString(),
 };

 const state = {
  search: "",
  sort: "due_date",
  order: "asc",
  page: 1,
  bucket: "",
  dueAxis: "all",
  mineOnly: false,
  assignmentScope: "inbox",
  owners: [],
  loading: false,
  needsReload: false,
  lastCheckedTaskBox: null
 };
 let ownersLoaded = false;
 let ownersLoadingPromise = null;
 let transferTargetsLoaded = false;
 let transferTargetsLoadingPromise = null;

 function normalizeDateInput(raw) {
  const value = (raw || "").toString().trim();
  if (!value) return "";
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : "";
 }

 // --- DOM Elements ---
 const els = {
  taskList: document.getElementById("task-list"),
  taskCount: document.getElementById("task-count"),
  quickStatusTabs: Array.from(document.querySelectorAll(".worklog-filter-tab")),
  dueAxisTabs: Array.from(document.querySelectorAll(".worklog-due-axis-btn")),
  daysPresetTabs: Array.from(document.querySelectorAll(".worklog-days-btn")),
  activeFilterChips: document.getElementById("active-filter-chips"),
  currentViewSummary: document.getElementById("current-view-summary"),
  resetFiltersBtn: document.getElementById("reset-filters-btn"),
  ownerLabel: document.getElementById("filter-owner-label"),
  mineToggle: document.getElementById("filter-mine"),
  filters: {
   status: document.getElementById("filter-status"),
   category: document.getElementById("filter-category"),
   ownerRole: document.getElementById("filter-owner-role"),
   owner: document.getElementById("filter-owner"),
   days: document.getElementById("filter-days"),
   dueFrom: document.getElementById("filter-due-from"),
   dueTo: document.getElementById("filter-due-to"),
  },
  search: {
   input: document.getElementById("search-input"),
   btn: document.getElementById("search-btn"),
  },
  export: {
   pageBtn: document.getElementById("export-xlsx-btn"),
   allBtn: document.getElementById("export-xlsx-all-btn"),
  },
  refreshBtn: document.getElementById("refresh-btn"),
  selectAll: document.getElementById("select-all-checkbox"),
  bulk: {
   complete: document.getElementById("bulk-complete-btn"),
   abandon: document.getElementById("bulk-abandon-btn"),
   transfer: document.getElementById("bulk-transfer-btn"),
   transferTarget: document.getElementById("bulk-transfer-target"),
  },
  pagination: {
   container: document.getElementById("pagination-container"),
   info: document.getElementById("pagination-info"),
  },
  toastContainer: document.getElementById("toast-container"),
  summary: {
   pending: document.getElementById("summary-pending"),
   urgent: document.getElementById("summary-urgent"),
   overdue: document.getElementById("summary-overdue"),
   completed: document.getElementById("summary-completed"),
  },
  bucket: {
   badge: document.getElementById("bucket-badge"),
   text: document.getElementById("bucket-badge-text"),
   clear: document.getElementById("bucket-badge-clear"),
  },
  assignment: {
   tabs: Array.from(document.querySelectorAll("[data-assignment-scope]")),
   list: document.getElementById("assignment-request-list"),
   inboxCount: document.getElementById("assignment-inbox-count"),
   sentCount: document.getElementById("assignment-sent-count"),
  },
 };

 // CSRF Token logic
 const csrfToken = document.querySelector('input[name="csrf_token"]')?.value
  || document.querySelector('meta[name="csrf-token"]')?.content
  || "";

 // --- Utility Functions ---

 // XSS prevention
 function escapeHtml(text) {
  if (!text) return "";
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
 }

 function truncateText(text, limit = 120) {
  const normalized = (text || "")
   .toString()
   .replace(/\s+/g, " ")
   .trim();
  if (!normalized) return "";
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, Math.max(0, limit - 3)).trimEnd()}...`;
 }

 function splitCsvNames(raw) {
  return (raw || "")
   .toString()
   .split(/[,;]/)
   .map(s => s.trim())
   .filter(Boolean);
 }

 function buildClientViewUrl(clientId) {
  const cid = (clientId || "").toString().trim();
  if (!/^\d+$/.test(cid)) return "";
  return (CONFIG.crmClientViewTemplate || "").replace(/0$/, encodeURIComponent(cid));
 }

 function renderApplicantCell(task) {
  const names = splitCsvNames(task?.applicant_name || "");
  if (!names.length) return '<span class="text-muted">-</span>';

  const uniqueNames = [];
  const seen = new Set();
  names.forEach(name => {
   const token = (name || "").toString().trim();
   if (!token || seen.has(token)) return;
   seen.add(token);
   uniqueNames.push(token);
  });
  if (!uniqueNames.length) return '<span class="text-muted">-</span>';

  const rawClientId = (task?.applicant_client_id || "").toString().trim();
  const clientViewUrl = buildClientViewUrl(rawClientId);

  // If we have one clear applicant+client mapping, prefer direct CRM client view link.
  if (clientViewUrl && uniqueNames.length === 1) {
   const applicantName = uniqueNames[0];
   const escapedName = escapeHtml(applicantName);
   const searchHref = `${CONFIG.crmClientsUrl}?q=${encodeURIComponent(applicantName)}`;
   return `
    <div class="d-flex align-items-center gap-1" style="min-width:0;">
     <a href="${clientViewUrl}"
       class="flex-grow-1 text-truncate text-decoration-none text-dark"
       style="min-width:0;"
       title="CRM Client Details (Applicant)">
      ${escapedName}
     </a>
     <a href="${searchHref}"
       class="link-muted flex-shrink-0"
       title="Search CRM">
      <i class="bi bi-search"></i>
     </a>
    </div>
   `;
  }

  return uniqueNames
   .map(name => {
    const href = `${CONFIG.crmClientsUrl}?q=${encodeURIComponent(name)}`;
    return `<a href="${href}" class="text-decoration-none text-dark" title="Applicant to CRM Search">${escapeHtml(name)}</a>`;
   })
   .join('<span class="text-muted">; </span>');
 }

 function normalizeStaffRows(rows, fallbackNames = "") {
  const out = [];
  const seen = new Set();
  const add = (id, name) => {
   const sid = (id || "").toString().trim();
   const sname = (name || "").toString().trim();
   if (!sid && !sname) return;
   const key = sid || sname;
   if (seen.has(key)) return;
   seen.add(key);
   out.push({ id: sid, name: sname || sid });
  };

  if (Array.isArray(rows)) {
   rows.forEach(row => {
    if (!row || typeof row !== "object") return;
    add(row.id, row.name);
   });
  }

  if (!out.length) {
   splitCsvNames(fallbackNames).forEach(name => add("", name));
  }
  return out;
 }

 function ownerRoleLabel(role) {
  const labels = {
   owner: "Task Contact",
   any: "Matter Contact",
   attorney: "Responsible attorney",
   handler: "Handler",
   manager: "Manager",
  };
  return labels[role] || role;
 }

 function updateOwnerLabel() {
  const label = els.ownerLabel;
  if (!label) return;
  const role = (els.filters.ownerRole?.value || "owner").trim();
  label.textContent = ownerRoleLabel(role);
 }

 function updateOwnerPlaceholderOption() {
  const select = els.filters.owner;
  if (!select || !select.options.length) return;
  const role = (els.filters.ownerRole?.value || "owner").trim();
  select.options[0].textContent = `All ${ownerRoleLabel(role)}`;
 }

 function categoryLabel(category) {
  if (category === "mgmt") return " ";
  if (category === "work") return " ";
  return "All ";
 }

 function statusLabel(status) {
  if (status === "todo") return "Open Task";
  if (status === "completed") return "Done";
  return "All";
 }

 function daysRangeLabel(days) {
  const token = (days || "").toString().trim();
  if (!token || token === "all") return "All";
  if (token === "365") return "1";
  return `${token}days `;
 }

 function updateQuickStatusTabs() {
  const currentFilter = (els.filters.status?.value || "todo").trim();
  const currentBucket = (state.bucket || "").trim();
  const currentDays = (els.filters.days?.value || "all").trim();

  (els.quickStatusTabs || []).forEach(btn => {
   const filter = (btn.dataset.filter || "").trim();
   const bucket = (btn.dataset.bucket || "").trim();
   const days = (btn.dataset.days || "all").trim();
   const sameFilter = currentFilter === filter;
   const sameBucket = currentBucket === bucket;
   const sameDays = bucket === "urgent" ? currentDays === days : true;
   const active = sameFilter && sameBucket && sameDays;
   btn.classList.toggle("active", active);
   btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
 }

 function updateDueAxisTabs() {
  const currentAxis = (state.dueAxis || DEFAULTS.due_axis).trim();
  (els.dueAxisTabs || []).forEach(btn => {
   const axis = (btn.dataset.dueAxis || "").trim();
   const active = axis === currentAxis;
   btn.classList.toggle("active", active);
   btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
 }

 function updateDaysPresetTabs() {
  const currentDays = (els.filters.days?.value || DEFAULTS.days).trim();
  (els.daysPresetTabs || []).forEach(btn => {
   const days = (btn.dataset.days || "").trim();
   const active = days === currentDays;
   btn.classList.toggle("active", active);
   btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
 }

 function renderActiveFilterChips() {
  if (!els.activeFilterChips) return;
  const chips = [];
  const status = (els.filters.status?.value || "todo").trim();
  const category = (els.filters.category?.value || "").trim();
  const ownerRole = (els.filters.ownerRole?.value || "owner").trim();
  const owner = (els.filters.owner?.value || "").trim();
  const days = (els.filters.days?.value || "all").trim();
  const dueFrom = normalizeDateInput(els.filters.dueFrom?.value || "");
  const dueTo = normalizeDateInput(els.filters.dueTo?.value || "");
  const ownerLabel = owner && els.filters.owner
   ? (els.filters.owner.selectedOptions?.[0]?.textContent || owner).trim()
   : "";

  chips.push(`<span class="chip"><i class="bi bi-funnel"></i>${escapeHtml(statusLabel(status))}</span>`);
  chips.push(`<span class="chip"><i class="bi bi-calendar2-range"></i>${escapeHtml(dueAxisLabel(state.dueAxis))}</span>`);
  chips.push(`<span class="chip"><i class="bi bi-grid-1x2"></i>${escapeHtml(categoryLabel(category))}</span>`);
  if (state.mineOnly) {
   chips.push(`<span class="chip"><i class="bi bi-person-check"></i> Task</span>`);
  }

  if (state.bucket) {
   chips.push(`<span class="chip"><i class="bi bi-flag"></i>${escapeHtml(bucketLabel(state.bucket))}</span>`);
  }
  if (owner && !state.mineOnly) {
   chips.push(
    `<span class="chip"><i class="bi bi-person"></i>${escapeHtml(ownerRoleLabel(ownerRole))}: ${escapeHtml(ownerLabel || owner)}</span>`
   );
  }
  if (days !== "all") {
   chips.push(`<span class="chip"><i class="bi bi-calendar3"></i>${escapeHtml(daysRangeLabel(days))}</span>`);
  }
  if (dueFrom || dueTo) {
   const fromLabel = dueFrom || "";
   const toLabel = dueTo || "days";
   chips.push(
    `<span class="chip"><i class="bi bi-calendar-range"></i>Due date: ${escapeHtml(fromLabel)} ~ ${escapeHtml(toLabel)}</span>`
   );
  }
  if (state.search) {
   chips.push(`<span class="chip"><i class="bi bi-search"></i>Search: ${escapeHtml(state.search)}</span>`);
  }
  els.activeFilterChips.innerHTML = chips.join("");
 }

 function renderCurrentViewSummary() {
  if (!els.currentViewSummary) return;
  const status = (els.filters.status?.value || DEFAULTS.filter).trim();
  const category = (els.filters.category?.value || "").trim();
  const ownerRole = (els.filters.ownerRole?.value || DEFAULTS.owner_role).trim();
  const owner = (els.filters.owner?.value || "").trim();
  const days = (els.filters.days?.value || DEFAULTS.days).trim();
  const dueFrom = normalizeDateInput(els.filters.dueFrom?.value || "");
  const dueTo = normalizeDateInput(els.filters.dueTo?.value || "");
  const ownerLabel = owner && els.filters.owner
   ? (els.filters.owner.selectedOptions?.[0]?.textContent || owner).trim()
   : "";

  const parts = [];
  parts.push(state.bucket ? bucketLabel(state.bucket) : statusLabel(status));
  parts.push(`${dueAxisLabel(state.dueAxis)} `);
  parts.push(`Due date ${daysRangeLabel(days)}`);

  if (dueFrom || dueTo) {
   parts.push(`Period ${dueFrom || ""} ~ ${dueTo || "days"}`);
  }
  if (category) {
   parts.push(categoryLabel(category));
  }
  if (state.mineOnly) {
   parts.push(" Task");
  } else if (owner) {
   parts.push(`${ownerRoleLabel(ownerRole)} ${ownerLabel || owner}`);
  } else {
   parts.push(`All ${ownerRoleLabel(ownerRole)}`);
  }
  if (state.search) {
   parts.push(`Search ${state.search}`);
  }

  els.currentViewSummary.textContent = parts.join(" · ");
 }

 function hasActiveFilters() {
  const s = getQueryState();
  return Boolean(
   s.filter !== DEFAULTS.filter
   || s.category
   || s.owner_role !== DEFAULTS.owner_role
   || s.owner
   || s.mine
   || s.days !== DEFAULTS.days
   || s.due_axis !== DEFAULTS.due_axis
   || s.due_from
   || s.due_to
   || s.search
   || s.sort !== DEFAULTS.sort
   || s.order !== DEFAULTS.order
   || s.bucket
  );
 }

 function renderRoleChips({ roleKey, rows, fallbackNames, roleMark }) {
  const normalized = normalizeStaffRows(rows, fallbackNames);
  if (!normalized.length) return "";
  return normalized.map(row => {
   const id = (row.id || "").toString().trim();
   const name = escapeHtml((row.name || "").toString().trim() || "-");
   if (id) {
    const href = buildPageUrl({ owner_role: roleKey, owner: id, page: 1 });
    return `<a href="${href}" class="worklog-staff-chip role-${roleKey}"><strong>${roleMark}</strong>${name}</a>`;
   }
   return `<span class="worklog-staff-chip role-${roleKey}"><strong>${roleMark}</strong>${name}</span>`;
  }).join("");
 }

 function renderOwnerChips(task) {
  const ownerId = (task.owner_id || "").toString().trim();
  const ownerNameRaw = (task.owner_name || ownerId || "").toString().trim();
  const rows = normalizeStaffRows(task.owners, ownerNameRaw);
  if (rows.length) {
   return rows.map(row => {
    const id = (row.id || "").toString().trim();
    const name = escapeHtml((row.name || "").toString().trim() || "-");
    if (id) {
     const href = buildPageUrl({ owner_role: "owner", owner: id, page: 1 });
     return `<a href="${href}" class="worklog-staff-chip role-owner"><strong>Task</strong>${name}</a>`;
    }
    return `<span class="worklog-staff-chip role-owner"><strong>Task</strong>${name}</span>`;
   }).join("");
  }
  if (ownerId) {
   const href = buildPageUrl({ owner_role: "owner", owner: ownerId, page: 1 });
   return `<a href="${href}" class="worklog-staff-chip role-owner"><strong>Task</strong>${escapeHtml(ownerNameRaw || ownerId)}</a>`;
  }
  const names = splitCsvNames(ownerNameRaw);
  if (!names.length) return "";
  return names.map(name => `<span class="worklog-staff-chip role-owner"><strong>Task</strong>${escapeHtml(name)}</span>`).join("");
 }

 function renderStaffCell(task) {
  const chunks = [];
  const owner = renderOwnerChips(task);
  if (owner) chunks.push(owner);
  const attorneys = renderRoleChips({
   roleKey: "attorney",
   rows: task.attorneys,
   fallbackNames: task.attorney_names,
   roleMark: "",
  });
  if (attorneys) chunks.push(attorneys);
  const handlers = renderRoleChips({
   roleKey: "handler",
   rows: task.handlers,
   fallbackNames: task.handler_names,
   roleMark: "",
  });
  if (handlers) chunks.push(handlers);
  const managers = renderRoleChips({
   roleKey: "manager",
   rows: task.managers,
   fallbackNames: task.manager_names,
   roleMark: "",
  });
  if (managers) chunks.push(managers);

  if (!chunks.length) return '<span class="text-muted">-</span>';
  return `<div class="worklog-staff-wrap">${chunks.join("")}</div>`;
 }

 function buildDueCalendarUrl(dateValue, axis) {
  const ymd = (dateValue || "").toString().slice(0, 10);
  if (!ymd) return "";
  const url = new URL("/deadline/calendar/month", window.location.origin);
  url.searchParams.set("date", ymd);
  if (axis && axis !== "all") url.searchParams.set("due_axis", axis);
  if (state.mineOnly) {
   url.searchParams.set("mine", "1");
  } else {
   const ownerRole = (els.filters.ownerRole?.value || DEFAULTS.owner_role).trim();
   const owner = (els.filters.owner?.value || "").trim();
   const ownerLabel = owner && els.filters.owner
    ? (els.filters.owner.selectedOptions?.[0]?.textContent || owner).trim()
    : "";
   if (ownerRole === "owner" && owner) {
    url.searchParams.set("owner", owner);
    if (ownerLabel) url.searchParams.set("owner_name", ownerLabel);
   }
  }
  return `${url.pathname}${url.search}`;
 }

 function dueStatusToneClass(status, primary) {
  if (!primary) return "text-muted";
  if (status === "overdue") return "text-danger";
  if (status === "urgent") return "text-warning";
  return "";
 }

 function renderDueCell(task) {
  const finalDue = (task.final_due_date || "").toString().trim();
  const internalDue = (task.internal_due_date || "").toString().trim();
  const currentAxis = (state.dueAxis || DEFAULTS.due_axis).trim();
  const lines = [];

  const addLine = ({ axis, label, value, primary }) => {
   const href = buildDueCalendarUrl(value, axis);
   const toneClass = dueStatusToneClass((task.status || "").toString().toLowerCase(), primary);
   const valueClass = `worklog-due-line__value${primary ? " primary" : ""}${toneClass ? ` ${toneClass}` : ""}`;
   const valueHtml = href
    ? `<a href="${href}" class="text-decoration-none ${valueClass}">${escapeHtml(value)}</a>`
    : `<span class="${valueClass}">${escapeHtml(value)}</span>`;
   lines.push(`
    <div class="worklog-due-line">
     <span class="worklog-due-line__label ${axis}">${escapeHtml(label)}</span>
     ${valueHtml}
    </div>
   `);
  };

  if (currentAxis === "final") {
   if (finalDue) addLine({ axis: "final", label: "", value: finalDue, primary: true });
   if (internalDue) addLine({ axis: "internal", label: "Internal", value: internalDue, primary: false });
  } else if (currentAxis === "internal") {
   if (internalDue) addLine({ axis: "internal", label: "Internal", value: internalDue, primary: true });
   if (finalDue) addLine({ axis: "final", label: "", value: finalDue, primary: false });
  } else {
   if (internalDue) {
    addLine({ axis: "internal", label: "Internal", value: internalDue, primary: true });
   }
   if (finalDue) {
    addLine({ axis: "final", label: "", value: finalDue, primary: !internalDue });
   }
  }

  if (!lines.length && task.due_date) {
   addLine({ axis: "all", label: "Due date", value: task.due_date, primary: true });
  }

  if (!lines.length) return '<span class="text-muted">-</span>';
  return `<div class="worklog-due-stack">${lines.join("")}</div>`;
 }

 function dueAxisLabel(axis) {
  const token = (axis || "").toString().trim().toLowerCase();
  if (token === "final") return " Due date";
  if (token === "internal") return "Internal Due date";
  return "All";
 }

 const DEFAULTS = {
  filter: "todo",
  category: "",
  owner_role: "owner",
  owner: "",
  mine: false,
  days: "all",
  due_from: "",
  due_to: "",
  due_axis: "all",
  search: "",
  sort: "due_date",
  order: "asc",
  page: 1,
  bucket: "",
 };

 function parseUrlState() {
  const params = new URLSearchParams(window.location.search || "");

  const rawBucket = (params.get("bucket") || "").trim().toLowerCase();
  const bucket = ["urgent", "overdue", "recommended", "completed_week"].includes(rawBucket) ? rawBucket : "";

  let rawFilter = (params.get("filter") || DEFAULTS.filter).trim().toLowerCase();
  if (bucket === "completed_week") rawFilter = "completed";
  if ((bucket === "urgent" || bucket === "overdue" || bucket === "recommended") && !params.has("filter")) rawFilter = "todo";
  const filter = ["todo", "completed", "all"].includes(rawFilter) ? rawFilter : DEFAULTS.filter;

  const rawCategory = (params.get("category") || DEFAULTS.category).trim().toLowerCase();
  const category = ["mgmt", "work"].includes(rawCategory) ? rawCategory : DEFAULTS.category;

  const rawOwnerRole = (params.get("owner_role") || DEFAULTS.owner_role).trim().toLowerCase();
  const ownerRole = ["owner", "attorney", "handler", "manager", "any"].includes(rawOwnerRole) ? rawOwnerRole : DEFAULTS.owner_role;

  const owner = (params.get("owner") || DEFAULTS.owner).trim();
  const ownerName = (params.get("owner_name") || "").trim();
  const mine = ["1", "true", "yes"].includes((params.get("mine") || "").trim().toLowerCase());

  const rawDays = (params.get("days") || DEFAULTS.days).trim();
  const days = bucket === "urgent" && !params.has("days") ? "7" : rawDays;
  const rawDueAxis = (params.get("due_axis") || DEFAULTS.due_axis).trim().toLowerCase();
  const dueAxis = ["all", "final", "internal"].includes(rawDueAxis) ? rawDueAxis : DEFAULTS.due_axis;
  let dueFrom = normalizeDateInput(params.get("due_from") || DEFAULTS.due_from);
  let dueTo = normalizeDateInput(params.get("due_to") || DEFAULTS.due_to);
  if (dueFrom && dueTo && dueFrom> dueTo) {
   const swappedFrom = dueTo;
   dueTo = dueFrom;
   dueFrom = swappedFrom;
  }

  const search = (params.get("search") || DEFAULTS.search).trim();

  const rawSort = (params.get("sort") || DEFAULTS.sort).trim();
  const sort = ["our_ref", "due_date"].includes(rawSort) ? rawSort : DEFAULTS.sort;

  const rawOrder = (params.get("order") || DEFAULTS.order).trim().toLowerCase();
  const order = rawOrder === "desc" ? "desc" : DEFAULTS.order;

  const rawPage = parseInt(params.get("page") || "", 10);
  const page = Number.isFinite(rawPage) && rawPage> 0 ? rawPage : DEFAULTS.page;

  return {
   filter,
   category,
   ownerRole,
   owner,
   ownerName,
   mine,
   days,
   dueAxis,
   dueFrom,
   dueTo,
   search,
   sort,
   order,
   page,
   bucket
  };
 }

 function getQueryState() {
  return {
   filter: (els.filters.status?.value || DEFAULTS.filter).trim(),
   category: (els.filters.category?.value || DEFAULTS.category).trim(),
   owner_role: (els.filters.ownerRole?.value || DEFAULTS.owner_role).trim(),
   owner: (els.filters.owner?.value || DEFAULTS.owner).trim(),
   mine: Boolean(state.mineOnly),
   days: (els.filters.days?.value || DEFAULTS.days).trim(),
   due_axis: (state.dueAxis || DEFAULTS.due_axis).trim(),
   due_from: normalizeDateInput(els.filters.dueFrom?.value || DEFAULTS.due_from),
   due_to: normalizeDateInput(els.filters.dueTo?.value || DEFAULTS.due_to),
   search: (state.search || "").trim(),
   sort: (state.sort || DEFAULTS.sort).trim(),
   order: (state.order || DEFAULTS.order).trim(),
   page: state.page || DEFAULTS.page,
   bucket: (state.bucket || DEFAULTS.bucket).trim(),
  };
 }

 function buildQueryString(s) {
  const params = new URLSearchParams();
  if (s.filter && s.filter !== DEFAULTS.filter) params.set("filter", s.filter);
  if (s.category) params.set("category", s.category);
  if (s.mine) {
   params.set("mine", "1");
  } else {
   if (s.owner_role && s.owner_role !== DEFAULTS.owner_role) params.set("owner_role", s.owner_role);
   if (s.owner) params.set("owner", s.owner);
  }
  if (s.bucket) params.set("bucket", s.bucket);
  if (s.days && s.days !== DEFAULTS.days) params.set("days", s.days);
  if (s.due_axis && s.due_axis !== DEFAULTS.due_axis) params.set("due_axis", s.due_axis);
  if (s.due_from) params.set("due_from", s.due_from);
  if (s.due_to) params.set("due_to", s.due_to);
  if (s.search) params.set("search", s.search);
  if (s.sort && s.sort !== DEFAULTS.sort) params.set("sort", s.sort);
  if (s.order && s.order !== DEFAULTS.order) params.set("order", s.order);
  if (s.page && Number(s.page)> 1) params.set("page", String(s.page));

  // purely for display (badges / external links)
  if (!s.mine && s.owner && els.filters.owner) {
   const opt = els.filters.owner.selectedOptions && els.filters.owner.selectedOptions[0];
   const label = opt && opt.value === s.owner ? (opt.textContent || "").trim() : "";
   if (label) params.set("owner_name", label);
  }

  return params.toString();
 }

 function buildPageUrl(overrides = {}) {
  const merged = { ...getQueryState(), ...overrides };
  const hasMineOverride = Object.prototype.hasOwnProperty.call(overrides, "mine");
  const hasOwnerOverride = Object.prototype.hasOwnProperty.call(overrides, "owner");
  const hasOwnerRoleOverride = Object.prototype.hasOwnProperty.call(overrides, "owner_role");
  if (!hasMineOverride && (hasOwnerOverride || hasOwnerRoleOverride)) {
   merged.mine = false;
  }
  const qs = buildQueryString(merged);
  return qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
 }

 function buildExportUrl(scope = "page") {
  const s = getQueryState();
  const params = new URLSearchParams();
  if (s.filter) params.set("filter", s.filter);
  if (s.category) params.set("category", s.category);
  if (s.owner_role) params.set("owner_role", s.owner_role);
  if (s.owner) params.set("owner", s.owner);
  if (s.mine) params.set("mine", "1");
  if (s.bucket) params.set("bucket", s.bucket);
  if (s.days) params.set("days", s.days);
  if (s.due_axis) params.set("due_axis", s.due_axis);
  if (s.due_from) params.set("due_from", s.due_from);
  if (s.due_to) params.set("due_to", s.due_to);
  if (s.search) params.set("search", s.search);
  if (s.sort) params.set("sort", s.sort);
  if (s.order) params.set("order", s.order);

  if (scope === "all") {
   params.set("page", "1");
   params.set("limit", String(CONFIG.exportMaxRows));
   params.set("export", "1");
   params.set("export_scope", "all");
  } else {
   params.set("page", String(s.page || 1));
   params.set("limit", String(CONFIG.itemsPerPage));
   params.set("export", "1");
   params.set("export_scope", "page");
  }

  if (!s.mine && s.owner && els.filters.owner) {
   const opt = els.filters.owner.selectedOptions && els.filters.owner.selectedOptions[0];
   const label = opt && opt.value === s.owner ? (opt.textContent || "").trim() : "";
   if (label) params.set("owner_name", label);
  }

  return `${CONFIG.apiPrefix}/tasks?${params.toString()}`;
 }

 function updateExportLinks() {
  if (els.export.pageBtn) {
   els.export.pageBtn.href = buildExportUrl("page");
  }
  if (els.export.allBtn) {
   els.export.allBtn.href = buildExportUrl("all");
  }
 }

 function syncUrl(replace = true) {
  const url = buildPageUrl();
  if (replace) history.replaceState(null, "", url);
  else history.pushState(null, "", url);
 }

 function applySortIcons() {
  document.querySelectorAll(".sortable i").forEach(i => i.className = "bi bi-arrow-down-up text-muted small");
  const th = document.querySelector(`.sortable[data-sort=\"${state.sort}\"]`);
  const icon = th ? th.querySelector("i") : null;
  if (icon) icon.className = `bi bi-arrow-${state.order === "asc" ? "up" : "down"} text-primary`;
 }

 function bucketLabel(bucket) {
  const b = (bucket || "").toString().trim().toLowerCase();
  if (b === "urgent") return "Due in 7 days";
  if (b === "overdue") return "Overdue";
  if (b === "recommended") return "Suggested completion";
  if (b === "completed_week") return "Completed this week";
  return b;
 }

 function renderBucketBadge() {
  const badge = els.bucket?.badge;
  const text = els.bucket?.text;
  if (!badge || !text) return;
  if (!state.bucket) {
   badge.classList.add("d-none");
   text.textContent = "";
   return;
  }
  text.textContent = bucketLabel(state.bucket);
  badge.classList.remove("d-none");
 }

 function updateSummaryCardLinks() {
  if (els.summary.pending) els.summary.pending.href = buildPageUrl({ filter: "todo", bucket: "", page: 1 });
  if (els.summary.urgent) els.summary.urgent.href = buildPageUrl({ filter: "todo", bucket: "urgent", days: "7", page: 1 });
  if (els.summary.overdue) els.summary.overdue.href = buildPageUrl({ filter: "todo", bucket: "overdue", page: 1 });
  if (els.summary.completed) els.summary.completed.href = buildPageUrl({ filter: "completed", bucket: "completed_week", page: 1 });
 }

 function refreshFilterUi() {
  if (els.mineToggle) {
   els.mineToggle.checked = Boolean(state.mineOnly);
  }
  if (els.filters.ownerRole) {
   els.filters.ownerRole.disabled = Boolean(state.mineOnly);
  }
  if (els.filters.owner) {
   els.filters.owner.disabled = Boolean(state.mineOnly);
  }
  updateOwnerLabel();
  updateOwnerPlaceholderOption();
  updateDueAxisTabs();
  updateDaysPresetTabs();
  renderBucketBadge();
  renderActiveFilterChips();
  renderCurrentViewSummary();
  updateQuickStatusTabs();
  updateSummaryCardLinks();
  updateExportLinks();
  if (els.resetFiltersBtn) {
   els.resetFiltersBtn.disabled = !hasActiveFilters();
  }
 }

 // Debounce for search
 function debounce(func, wait) {
  let timeout;
  return function executedFunction(...args) {
   const later = () => {
    clearTimeout(timeout);
    func(...args);
   };
   clearTimeout(timeout);
   timeout = setTimeout(later, wait);
  };
 }

 function showToast(message, type = "success") {
  const toastId = `toast-${Date.now()}`;
  const bgClass = type === "success" ? "bg-success" : type === "danger" ? "bg-danger" : "bg-warning";
  const icon = type === "success" ? "check-circle" : "exclamation-circle";
  const safeMessage = escapeHtml(String(message ?? ""));

  const html = `
   <div id="${toastId}" class="toast align-items-center text-white ${bgClass} border-0 shadow" role="alert" aria-live="assertive" aria-atomic="true">
    <div class="d-flex">
     <div class="toast-body">
      <i class="bi bi-${icon} me-2"></i> ${safeMessage}
     </div>
     <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>
   </div>
  `;
  els.toastContainer.insertAdjacentHTML("beforeend", html);
  const toastEl = document.getElementById(toastId);
  const toast = new bootstrap.Toast(toastEl, { delay: 3000 });
  toast.show();
  toastEl.addEventListener("hidden.bs.toast", () => toastEl.remove());
 }

 async function apiCall(endpoint, method = "GET", body = null) {
  const options = {
   method,
   headers: {
    "Content-Type": "application/json",
    "X-CSRFToken": csrfToken
   }
  };
  if (body) options.body = JSON.stringify(body);

  const response = await fetch(`${CONFIG.apiPrefix}${endpoint}`, options);
  const raw = await response.text();
  let data = null;
  if (raw) {
   try {
    data = JSON.parse(raw);
   } catch (e) {
    data = { message: raw };
   }
  }
  if (!response.ok) {
   const msg = (data && typeof data === "object" && (data.error || data.message))
    || response.statusText
    || " Error .";
   throw new Error(msg);
  }
  return data || {};
 }

 function assignmentStatusBadge(status) {
  const key = (status || "").toString().toLowerCase();
  if (key === "pending") return '<span class="badge bg-warning text-dark">Confirm</span>';
  if (key === "accepted") return '<span class="badge bg-success"></span>';
  if (key === "rejected") return '<span class="badge bg-danger"></span>';
  if (key === "cancelled") return '<span class="badge bg-secondary">Cancel</span>';
  return `<span class="badge bg-light text-dark border">${escapeHtml(status || "-")}</span>`;
 }

 function formatAssignmentDate(value) {
  const raw = (value || "").toString().trim();
  if (!raw) return "-";
  return raw.replace("T", " ").slice(0, 16);
 }

 async function promptAssignmentRejectReason() {
  if (window.AppPrompt) {
   const value = await window.AppPrompt(" ", "", { title: " " });
   return value == null ? null : String(value).trim();
  }
  const value = window.prompt(" ", "");
  return value == null ? null : String(value).trim();
 }

 function updateAssignmentCounts(data) {
  const scope = data.scope || state.assignmentScope;
  const counts = data.counts || {};
  if (els.assignment.inboxCount) {
   const value = counts.inbox_pending != null
    ? counts.inbox_pending
    : (scope === "inbox" ? data.pending_count : els.assignment.inboxCount.textContent);
   els.assignment.inboxCount.textContent = String(value || 0);
  }
  if (els.assignment.sentCount) {
   const value = counts.sent_pending != null
    ? counts.sent_pending
    : (scope === "sent" ? data.pending_count : els.assignment.sentCount.textContent);
   els.assignment.sentCount.textContent = String(value || 0);
  }
 }

 function renderAssignmentRequests(data) {
  if (!els.assignment.list) return;
  const rows = data.requests || [];
  const scope = data.scope || state.assignmentScope;
  updateAssignmentCounts(data);
  if (!rows.length) {
   els.assignment.list.innerHTML = '<div class="text-muted">Display Confirm none.</div>';
   return;
  }
  els.assignment.list.innerHTML = rows.map(row => {
   const safeTitle = escapeHtml(row.workflow_name || "-");
   const safeRef = escapeHtml(row.our_ref || row.case_id || "-");
   const safeRole = escapeHtml(row.role_label || "-");
   const safeRequester = escapeHtml(row.requested_by_name || "-");
   const safeTarget = escapeHtml(row.target_user_name || "-");
   const safeNote = escapeHtml(row.response_note || "");
   const workflowUrl = row.workflow_url || (row.workflow_id ? `/workflow/${encodeURIComponent(row.workflow_id)}` : "#");
   const canRespond = Boolean(row.can_respond);
   const actionButtons = canRespond
    ? `<div class="btn-group btn-group-sm">
      <button type="button" class="btn btn-primary assignment-accept-btn" data-request-id="${row.id}"></button>
      <button type="button" class="btn btn-outline-danger assignment-reject-btn" data-request-id="${row.id}"></button>
     </div>`
    : "";
   return `
    <div class="assignment-request-item">
     <div class="d-flex flex-wrap justify-content-between gap-2">
      <div class="assignment-request-title">
       <div class="fw-semibold text-truncate">
        <a href="${workflowUrl}" class="text-decoration-none">${safeTitle}</a>
       </div>
       <div class="text-muted mt-1">
        ${safeRef} · ${safeRole} · ${scope === "sent" ? `target ${safeTarget}` : ` ${safeRequester}`}
       </div>
       <div class="text-muted mt-1"> ${formatAssignmentDate(row.requested_at)}${row.responded_at ? ` · Process ${formatAssignmentDate(row.responded_at)}` : ""}</div>
       ${safeNote ? `<div class="text-muted mt-1">: ${safeNote}</div>` : ""}
      </div>
      <div class="d-flex flex-column align-items-end gap-2">
       ${assignmentStatusBadge(row.status)}
       ${actionButtons}
      </div>
     </div>
    </div>`;
  }).join("");
 }

 async function loadAssignmentRequests(scope = state.assignmentScope) {
  if (!els.assignment.list) return;
  state.assignmentScope = scope;
  els.assignment.tabs.forEach(tab => {
   tab.classList.toggle("active", tab.dataset.assignmentScope === scope);
  });
  els.assignment.list.innerHTML = '<div class="text-muted">Loading...</div>';
  try {
   const data = await apiCall(`/assignment-requests?scope=${encodeURIComponent(scope)}`);
   renderAssignmentRequests(data);
  } catch (e) {
   els.assignment.list.innerHTML = `<div class="text-danger"> Confirm  : ${escapeHtml(e.message)}</div>`;
  }
 }

 async function respondAssignmentRequest(requestId, action) {
  const id = Number(requestId || 0);
  if (!id) return;
  let body = {};
  if (action === "reject") {
   const reason = await promptAssignmentRejectReason();
   if (reason === null) return;
   body = { reason };
  }
  try {
   await apiCall(`/assignment-requests/${id}/${action}`, "POST", body);
   showToast(action === "accept" ? " ." : " .");
   await Promise.all([loadAssignmentRequests(state.assignmentScope), loadTasks(), loadSummary()]);
  } catch (e) {
   showToast(e.message || " An error occurred while processing the request.", "danger");
  }
 }

 function renderAssignmentPendingBadges(task) {
  const roles = Array.isArray(task.assignment_pending_roles) ? task.assignment_pending_roles : [];
  if (!roles.length) return "";
  return roles.map(role => {
   const label = escapeHtml(role.role_label || "Contact");
   if (role.is_for_current_user) {
    return `<span class="badge bg-warning text-dark assignment-pending-badge"> Confirm required: ${label}</span>`;
   }
   return `<span class="badge bg-light text-dark border assignment-pending-badge">Confirm: ${label}</span>`;
  }).join("");
 }

 // --- Core Logic ---

 async function loadSummary() {
  try {
   const params = new URLSearchParams({
    category: els.filters.category.value,
    owner_role: els.filters.ownerRole.value,
    owner: els.filters.owner.value,
    mine: state.mineOnly ? "1" : "",
    days: els.filters.days.value,
    due_axis: state.dueAxis,
    due_from: normalizeDateInput(els.filters.dueFrom?.value || ""),
    due_to: normalizeDateInput(els.filters.dueTo?.value || ""),
    search: (state.search || "").trim(),
   });
   if (!state.mineOnly) params.delete("mine");
   const data = await apiCall(`/summary?${params}`);
   els.summary.pending.textContent = data.pending || 0;
   els.summary.urgent.textContent = data.urgent || 0;
   els.summary.overdue.textContent = data.overdue || 0;
   els.summary.completed.textContent = data.completed_week || 0;
   updateSummaryCardLinks();
  } catch (e) {
   console.error("Summary load failed", e);
  }
 }

 async function loadOwners() {
  try {
   const params = new URLSearchParams({
    owner_role: els.filters.ownerRole.value || DEFAULTS.owner_role,
   });
   const data = await apiCall(`/owners?${params}`);
   const select = els.filters.owner;
   // Keep first option
   while (select.options.length> 1) select.remove(1);
   updateOwnerPlaceholderOption();

   const owners = data.owners || [];
   state.owners = owners;
   owners.forEach(owner => {
    const opt = document.createElement("option");
    opt.value = owner.id;
    opt.textContent = owner.name;
    select.appendChild(opt);
   });
   ownersLoaded = true;
  } catch (e) {
   ownersLoaded = false;
   console.error("Owners load failed", e);
  }
 }

 async function loadTransferTargets() {
  const select = els.bulk.transferTarget;
  if (!select) return;
  const selected = (select.value || "").trim();

  try {
   const data = await apiCall("/transfer-targets");
   while (select.options.length> 1) select.remove(1);

   (data.users || []).forEach(user => {
    const uid = Number(user.id || 0);
    if (!uid || uid === CURRENT_USER_ID) return;
    const opt = document.createElement("option");
    opt.value = String(uid);
    opt.textContent = (user.name || `User #${uid}`).toString().trim();
    select.appendChild(opt);
   });

   if (selected && Array.from(select.options).some(opt => opt.value === selected)) {
    select.value = selected;
   } else {
    select.value = "";
   }
   transferTargetsLoaded = true;
  } catch (e) {
   transferTargetsLoaded = false;
   console.error("Transfer target load failed", e);
  } finally {
   updateBulkButtons();
  }
 }

 function ensureOwnersLoaded(force = false) {
  if (force) ownersLoaded = false;
  if (ownersLoaded && !force) return Promise.resolve();
  if (ownersLoadingPromise && !force) return ownersLoadingPromise;
  ownersLoadingPromise = loadOwners().finally(() => {
   ownersLoadingPromise = null;
  });
  return ownersLoadingPromise;
 }

 function ensureTransferTargetsLoaded(force = false) {
  if (force) transferTargetsLoaded = false;
  if (transferTargetsLoaded && !force) return Promise.resolve();
  if (transferTargetsLoadingPromise && !force) return transferTargetsLoadingPromise;
  transferTargetsLoadingPromise = loadTransferTargets().finally(() => {
   transferTargetsLoadingPromise = null;
  });
  return transferTargetsLoadingPromise;
 }

 function scheduleInitialSummaryLoad() {
  const run = () => {
   void loadSummary();
  };
  if (typeof window.requestIdleCallback === "function") {
   window.requestIdleCallback(run, { timeout: 400 });
   return;
  }
  window.setTimeout(run, 150);
 }

 async function loadTasks() {
  if (state.loading) {
   state.needsReload = true;
   return;
  }
  state.loading = true;
  state.needsReload = false;

  // Build Query
  const params = new URLSearchParams({
   filter: els.filters.status.value,
   category: els.filters.category.value,
   owner_role: els.filters.ownerRole.value,
   owner: els.filters.owner.value,
   mine: state.mineOnly ? "1" : "",
   bucket: state.bucket,
   days: els.filters.days.value,
   due_axis: state.dueAxis,
   due_from: normalizeDateInput(els.filters.dueFrom?.value || ""),
   due_to: normalizeDateInput(els.filters.dueTo?.value || ""),
   page: state.page,
   limit: CONFIG.itemsPerPage,
   search: state.search,
   sort: state.sort,
   order: state.order
  });
  if (!state.mineOnly) params.delete("mine");

  els.taskList.innerHTML = `
   <tr><td colspan="8" class="text-center py-5 text-muted">
    <div class="worklog-empty-state">
     <div class="spinner-border spinner-border-sm text-primary" role="status"></div>
     <div>Loading...</div>
    </div>
   </td></tr>`;

  try {
   const data = await apiCall(`/tasks?${params}`);
   renderTasks(data);
  } catch (e) {
   els.taskList.innerHTML = `
    <tr><td colspan="8" class="text-center py-5 text-danger">
     <div class="worklog-empty-state">
      <i class="bi bi-exclamation-triangle fs-4"></i>
      <div> : ${escapeHtml(e.message)}</div>
     </div>
    </td></tr>`;
  } finally {
   state.loading = false;
   els.selectAll.checked = false;
   updateBulkButtons();
   if (state.needsReload) {
    state.needsReload = false;
    loadTasks();
   }
  }
 }

 function renderTasks(data) {
  const tasks = data.tasks || [];
  const totalCount = data.total || 0;
  state.lastCheckedTaskBox = null;

  els.taskCount.textContent = `Total ${totalCount.toLocaleString()}items`;
  renderPagination(data.page || 1, data.total_pages || 1, totalCount);

  if (tasks.length === 0) {
   els.taskList.innerHTML = `
    <tr><td colspan="8" class="text-center py-5 text-muted bg-light">
     <div class="worklog-empty-state">
      <i class="bi bi-inbox fs-4"></i>
      <div>${state.search ? "No search results." : "Display Task none."}</div>
     </div>
    </td></tr>`;
   return;
  }

  els.taskList.innerHTML = tasks.map(task => {
   const isCompleted = task.status === "completed" || task.status === "abandoned";
   const statusBadge = getStatusBadge(task.status);
   const categoryType = (task.category_type || "").toString().trim().toLowerCase();
   const categoryLabel = (task.category_display || "").toString().trim();
   const safeCategoryLabel = escapeHtml(
    categoryLabel || (categoryType === "mgmt" ? "" : categoryType === "hybrid" ? "HYBRID" : "")
   );
   const categoryBadge = categoryType === "mgmt"
    ? `<span class="badge bg-info bg-opacity-10 text-info border border-info">${safeCategoryLabel}</span>`
    : categoryType === "work"
     ? `<span class="badge bg-secondary bg-opacity-10 text-secondary border">${safeCategoryLabel}</span>`
     : `<span class="badge bg-dark-subtle text-dark border">${safeCategoryLabel || "HYBRID"}</span>`;
   const completionRecommended = Boolean(task.completion_recommendation);
   const recommendationTitle = escapeHtml(
    task.completion_recommendation_text || "Suggested client-notice completion."
   );
   const recommendationUrl = buildPageUrl({ filter: "todo", bucket: "recommended", page: 1 });
   const recommendationBadge = completionRecommended
    ? `<a href="${recommendationUrl}" class="text-decoration-none" title="View suggested completions"><span class="badge bg-success bg-opacity-10 text-success border border-success"><i class="bi bi-stars me-1"></i>Suggested completion</span></a>`
    : "";
   const assignmentBadges = renderAssignmentPendingBadges(task);
   const completeBtnClass = completionRecommended ? "btn-success" : "btn-outline-success";
   const completeBtnTitle = completionRecommended ? "Complete suggested task" : "Complete task";

   // Escape all user content
   const safeRef = escapeHtml(task.our_ref || '-');
   const safeName = escapeHtml(task.task_name || '-');
   const rawDesc = (task.worklog_description || '').toString();
   const safeDesc = escapeHtml(truncateText(rawDesc, 120));
   const safeDescTitle = escapeHtml(
    rawDesc
     .replace(/\s+/g, ' ')
     .trim()
   );
   const safeDueDate = escapeHtml(task.due_date || '');
   const safeMatterId = encodeURIComponent(String(task.matter_id || ''));
   const caseUrl = safeMatterId ? `/case/${safeMatterId}` : "#";
   const actionId = String(task.id || "");
   const wfToken = String(task.workflow_link_id || actionId);
   const wfId = encodeURIComponent(wfToken);
   const wfNumeric = wfToken.startsWith("wf_") ? wfToken.slice(3) : "";
   const wfUrl = safeMatterId ? `/case/${safeMatterId}?workflow_id=${wfId}#sec-workflow` : "#";
   const wfDetailUrl = wfNumeric ? `/workflow/${encodeURIComponent(wfNumeric)}` : wfUrl;
   const wfCaseLink = (wfUrl && wfUrl !== "#")
    ? `<a href="${wfUrl}" class="link-muted ms-1" title="Matter (Task )"><i class="bi bi-link-45deg"></i></a>`
    : "";
   const staffCell = renderStaffCell(task);
   const dueCellHtml = renderDueCell(task);

   const categoryUrl = (categoryType === "mgmt" || categoryType === "work")
    ? buildPageUrl({ category: categoryType, page: 1 })
    : "";
   const statusUrl = (() => {
    const s = (task.status || "").toString().toLowerCase();
    if (s === "urgent") return buildPageUrl({ filter: "todo", bucket: "urgent", days: "7", page: 1 });
    if (s === "overdue") return buildPageUrl({ filter: "todo", bucket: "overdue", page: 1 });
    if (s === "completed" || s === "abandoned") return buildPageUrl({ filter: "completed", bucket: "", page: 1 });
    return buildPageUrl({ filter: "todo", bucket: "", page: 1 });
   })();
   const statusKey = (task.status || "pending").toString().toLowerCase().replace(/[^a-z0-9_-]/g, "") || "pending";

   return `
    <tr class="worklog-task-row worklog-task-row--${statusKey} ${isCompleted ? 'table-light opacity-75' : ''}">
     <td class="text-center" data-label="Select">
      ${!isCompleted ? `
      <input type="checkbox" class="form-check-input task-row-checkbox"
       data-id="${actionId}"
       data-our-ref="${safeRef}"
       data-task-name="${safeName}"
       data-due-date="${safeDueDate}">
      ` : ''}
     </td>
     <td data-label="Matter reference">
      ${caseUrl !== "#"
       ? `<a href="${caseUrl}" class="fw-semibold text-decoration-none">${safeRef}</a>`
       : `<span class="fw-semibold">${safeRef}</span>`}
      <div class="worklog-meta-line text-muted mt-1">
       <a href="${wfDetailUrl}" class="text-decoration-none">Task</a>${wfCaseLink}
      </div>
     </td>
     <td class="small" data-label="Applicant">
      ${renderApplicantCell(task)}
     </td>
     <td class="worklog-task-cell" data-label="Task">
      <div class="d-flex flex-wrap align-items-center gap-2">
       <a href="${wfDetailUrl}" class="${isCompleted ? 'text-decoration-line-through text-muted' : 'fw-medium text-decoration-none'}">${safeName}</a>
       ${categoryUrl
        ? `<a href="${categoryUrl}" class="text-decoration-none" title="View category">${categoryBadge}</a>`
        : categoryBadge}
       ${recommendationBadge}
       ${assignmentBadges}
      </div>
      ${safeDesc ? `<div class="worklog-meta-line text-muted mt-1 worklog-desc-preview" title="${safeDescTitle}"><i class="bi bi-chat-left-text"></i><span class="worklog-desc-preview__text">${safeDesc}</span></div>` : ''}
     </td>
     <td class="small" data-label="Responsible ">${staffCell}</td>
     <td class="small" data-label="Due date">
      ${dueCellHtml}
     </td>
     <td data-label="Status">
      <a href="${statusUrl}" class="text-decoration-none" title="View status">${statusBadge}</a>
      ${completionRecommended ? `<div class="small text-success mt-1" title="${recommendationTitle}"><i class="bi bi-info-circle me-1"></i>Suggested completion</div>` : ""}
     </td>
     <td data-label="">
      ${isCompleted ? `
       <button class="btn btn-sm btn-outline-secondary reopen-btn" data-id="${actionId}" title=" Open" aria-label="Task Open">
        <i class="bi bi-arrow-counterclockwise"></i>
       </button>
      ` : `
       <div class="btn-group btn-group-sm">
        <button class="btn ${completeBtnClass} complete-btn" data-id="${actionId}"
         data-our-ref="${safeRef}" data-task-name="${safeName}" data-due-date="${safeDueDate}" title="${completeBtnTitle}" aria-label="${completeBtnTitle}">
         <i class="bi bi-check-lg"></i>
        </button>
        <button class="btn btn-outline-secondary abandon-btn" data-id="${actionId}"
         data-our-ref="${safeRef}" data-task-name="${safeName}" title="Task " aria-label="Task ">
         <i class="bi bi-x-lg"></i>
        </button>
        <button class="btn btn-outline-primary note-btn" data-id="${actionId}"
         data-our-ref="${safeRef}" data-task-name="${safeName}" title="Notes" aria-label="Notes Add">
         <i class="bi bi-pencil"></i>
        </button>
       </div>
      `}
     </td>
    </tr>
   `;
  }).join("");
 }

 function getStatusBadge(status) {
  const badges = {
   completed: '<span class="badge bg-success"><i class="bi bi-check-circle me-1"></i>Done</span>',
   abandoned: '<span class="badge bg-secondary"><i class="bi bi-slash-circle me-1"></i></span>',
   overdue: '<span class="badge bg-danger"><i class="bi bi-exclamation-octagon me-1"></i></span>',
   urgent: '<span class="badge bg-warning text-dark"><i class="bi bi-lightning-fill me-1"></i></span>',
   default: '<span class="badge bg-light text-dark border"></span>'
  };
  return badges[status] || badges.default;
 }

 function renderPagination(page, totalPages, totalCount) {
  const container = els.pagination.container;
  container.innerHTML = "";

  if (totalCount === 0) {
   els.pagination.info.textContent = "Search ";
   return;
  }

  els.pagination.info.textContent = `Total ${totalCount.toLocaleString()}items (page ${page} / ${totalPages})`;
  window.AppPagination.render(container, {
   page,
   totalPages,
   ariaLabel: "Task Pagination",
   onPageChange: (newPage) => {
    if (newPage && newPage !== state.page) {
     state.page = newPage;
     syncUrl();
     loadTasks();
    }
   },
  });
 }

 // --- Event Handling ---

 function resetFilters() {
  if (els.filters.status) els.filters.status.value = DEFAULTS.filter;
  if (els.filters.category) els.filters.category.value = DEFAULTS.category;
  if (els.filters.ownerRole) els.filters.ownerRole.value = DEFAULTS.owner_role;
  if (els.filters.owner) els.filters.owner.value = DEFAULTS.owner;
  if (els.filters.days) els.filters.days.value = DEFAULTS.days;
  if (els.filters.dueFrom) els.filters.dueFrom.value = DEFAULTS.due_from;
  if (els.filters.dueTo) els.filters.dueTo.value = DEFAULTS.due_to;
  if (els.search.input) els.search.input.value = DEFAULTS.search;

  state.search = DEFAULTS.search;
  state.sort = DEFAULTS.sort;
  state.order = DEFAULTS.order;
  state.page = DEFAULTS.page;
  state.bucket = DEFAULTS.bucket;
  state.dueAxis = DEFAULTS.due_axis;
  state.mineOnly = DEFAULTS.mine;

  applySortIcons();
  void ensureOwnersLoaded(true);
  refreshFilterUi();
  syncUrl();
  loadTasks();
  loadSummary();
 }

 // Filter Changes
 const handleFilterChange = (e) => {
  if (e && e.target === els.filters.ownerRole) {
   if (els.filters.owner) els.filters.owner.value = "";
   ensureOwnersLoaded(true);
  }
  if (e && e.target === els.filters.status) {
   state.bucket = "";
  }
  state.page = 1;
  refreshFilterUi();
  syncUrl();
  loadTasks();
  loadSummary();
 };
 Object.values(els.filters).forEach(el => { if (el) el.addEventListener("change", handleFilterChange); });
 els.refreshBtn.addEventListener("click", () => {
  refreshFilterUi();
  syncUrl();
  loadTasks();
  loadSummary();
  loadAssignmentRequests(state.assignmentScope);
 });

 (els.assignment.tabs || []).forEach(btn => {
  btn.addEventListener("click", () => {
   const scope = (btn.dataset.assignmentScope || "inbox").trim();
   loadAssignmentRequests(scope);
  });
 });

 if (els.assignment.list) {
  els.assignment.list.addEventListener("click", (e) => {
   const acceptBtn = e.target.closest(".assignment-accept-btn");
   const rejectBtn = e.target.closest(".assignment-reject-btn");
   if (acceptBtn) {
    respondAssignmentRequest(acceptBtn.dataset.requestId, "accept");
   } else if (rejectBtn) {
    respondAssignmentRequest(rejectBtn.dataset.requestId, "reject");
   }
  });
 }

 (els.quickStatusTabs || []).forEach(btn => {
  btn.addEventListener("click", () => {
   const targetFilter = (btn.dataset.filter || "todo").trim();
   const targetBucket = (btn.dataset.bucket || "").trim();
   const targetDays = (btn.dataset.days || "all").trim();

   if (els.filters.status) els.filters.status.value = targetFilter;
   state.bucket = targetBucket;
   if (els.filters.days && Array.from(els.filters.days.options).some(o => o.value === targetDays)) {
    els.filters.days.value = targetDays;
   }
   state.page = 1;
   refreshFilterUi();
   syncUrl();
   loadTasks();
   loadSummary();
  });
 });

 (els.daysPresetTabs || []).forEach(btn => {
  btn.addEventListener("click", () => {
   const targetDays = (btn.dataset.days || DEFAULTS.days).trim();
   if (!els.filters.days || !Array.from(els.filters.days.options).some(o => o.value === targetDays)) {
    return;
   }
   els.filters.days.value = targetDays;
   state.page = 1;
   refreshFilterUi();
   syncUrl();
   loadTasks();
   loadSummary();
  });
 });

 (els.dueAxisTabs || []).forEach(btn => {
  btn.addEventListener("click", () => {
   const nextAxis = (btn.dataset.dueAxis || DEFAULTS.due_axis).trim().toLowerCase();
   if (!["all", "final", "internal"].includes(nextAxis) || nextAxis === state.dueAxis) return;
   state.dueAxis = nextAxis;
   state.page = 1;
   refreshFilterUi();
   syncUrl();
   loadTasks();
   loadSummary();
  });
 });

 if (els.mineToggle) {
  els.mineToggle.addEventListener("change", () => {
   state.mineOnly = Boolean(els.mineToggle.checked) && Boolean(CURRENT_USER_OWNER_VALUE);
   state.page = 1;
   refreshFilterUi();
   syncUrl();
   loadTasks();
   loadSummary();
  });
 }

 if (els.bucket.clear) {
  els.bucket.clear.addEventListener("click", (e) => {
   e.preventDefault();
   state.bucket = "";
   state.page = 1;
   refreshFilterUi();
   syncUrl();
   loadTasks();
   loadSummary();
  });
 }

 if (els.resetFiltersBtn) {
  els.resetFiltersBtn.addEventListener("click", () => {
   resetFilters();
  });
 }

 // Search (Debounced)
 const handleSearch = debounce(() => {
  state.search = els.search.input.value.trim();
  state.page = 1;
  refreshFilterUi();
  syncUrl();
  loadTasks();
  loadSummary();
 }, 400); // 400ms debounce
 els.search.input.addEventListener("input", handleSearch);
 els.search.input.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  state.search = els.search.input.value.trim();
  state.page = 1;
  refreshFilterUi();
  syncUrl();
  loadTasks();
  loadSummary();
 });
 els.search.btn.addEventListener("click", () => {
  state.search = els.search.input.value.trim();
  state.page = 1;
  refreshFilterUi();
  syncUrl();
  loadTasks();
  loadSummary();
 });

	 // Sorting
 document.querySelectorAll(".sortable").forEach(th => {
  th.addEventListener("click", function() {
   const col = this.dataset.sort;
   if (state.sort === col) {
    state.order = state.order === "asc" ? "desc" : "asc";
   } else {
    state.sort = col;
    state.order = "asc";
   }

   state.page = 1;
   applySortIcons();
   syncUrl();
   loadTasks();
  });
 });

 // Checkbox & Bulk Logic
 function getTaskCheckboxes() {
  return Array.from(document.querySelectorAll(".task-row-checkbox"));
 }

 function syncSelectAllCheckboxState() {
  const all = getTaskCheckboxes();
  const checked = all.filter(cb => cb.checked);
  els.selectAll.checked = all.length> 0 && all.length === checked.length;
  els.selectAll.indeterminate = checked.length> 0 && checked.length < all.length;
 }

 function applyShiftRangeSelection(anchorBox, targetBox) {
  if (!anchorBox || !targetBox) return;
  const boxes = getTaskCheckboxes();
  const start = boxes.indexOf(anchorBox);
  const end = boxes.indexOf(targetBox);
  if (start < 0 || end < 0) return;
  const [from, to] = start <= end ? [start, end] : [end, start];
  for (let i = from; i <= to; i++) {
   boxes[i].checked = targetBox.checked;
  }
 }

 function updateBulkButtons() {
  const checked = document.querySelectorAll(".task-row-checkbox:checked");
  const count = checked.length;
  const targetSelected = Boolean((els.bulk.transferTarget?.value || "").trim());

  if (count> 0) {
   els.bulk.complete.removeAttribute("disabled");
   els.bulk.abandon.removeAttribute("disabled");
   if (targetSelected) {
    els.bulk.transfer.removeAttribute("disabled");
   } else {
    els.bulk.transfer.setAttribute("disabled", "disabled");
   }
  } else {
   els.bulk.complete.setAttribute("disabled", "disabled");
   els.bulk.abandon.setAttribute("disabled", "disabled");
   els.bulk.transfer.setAttribute("disabled", "disabled");
  }

  els.bulk.complete.innerHTML = count> 0
   ? `<i class="bi bi-check-all"></i> Complete selected (${count})`
   : `<i class="bi bi-check-all"></i> Complete selected`;

  els.bulk.abandon.innerHTML = count> 0
   ? `<i class="bi bi-x-lg"></i> Close selected (${count})`
   : `<i class="bi bi-x-lg"></i> Close selected`;

  els.bulk.transfer.innerHTML = count> 0
   ? `<i class="bi bi-arrow-left-right"></i> Reassign selected (${count})`
   : `<i class="bi bi-arrow-left-right"></i> Reassign selected`;
 }

 els.selectAll.addEventListener("change", (e) => {
  getTaskCheckboxes().forEach(cb => cb.checked = e.target.checked);
  els.selectAll.indeterminate = false;
  state.lastCheckedTaskBox = null;
  updateBulkButtons();
 });
 if (els.filters.owner) {
  ["focus", "mousedown", "touchstart"].forEach(evtName => {
   els.filters.owner.addEventListener(evtName, () => {
    ensureOwnersLoaded();
   }, { passive: evtName === "touchstart" });
  });
 }
 if (els.bulk.transferTarget) {
  ["focus", "mousedown", "touchstart"].forEach(evtName => {
   els.bulk.transferTarget.addEventListener(evtName, () => {
    ensureTransferTargetsLoaded();
   }, { passive: evtName === "touchstart" });
  });
 }
 if (els.bulk.transferTarget) {
  els.bulk.transferTarget.addEventListener("change", () => updateBulkButtons());
 }

 // Shift + to select currency
 els.taskList.addEventListener("click", (e) => {
  const cb = e.target.closest(".task-row-checkbox");
  if (!cb) return;
  if (e.shiftKey) {
   applyShiftRangeSelection(state.lastCheckedTaskBox, cb);
  }
  state.lastCheckedTaskBox = cb;
  updateBulkButtons();
  syncSelectAllCheckboxState();
 });

 els.taskList.addEventListener("change", (e) => {
  if (e.target.classList.contains("task-row-checkbox")) {
   updateBulkButtons();
   syncSelectAllCheckboxState();
   if (!state.lastCheckedTaskBox) {
    state.lastCheckedTaskBox = e.target;
   }
  }
 });

 // --- Actions (Delegation) ---
 const modals = {
  complete: new bootstrap.Modal(document.getElementById("completeModal")),
  note: new bootstrap.Modal(document.getElementById("noteModal")),
  abandon: new bootstrap.Modal(document.getElementById("abandonModal")),
 };

 function isTypingTarget(el) {
  if (!el) return false;
  const tag = (el.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return Boolean(el.isContentEditable);
 }

 function firstActionButton(selector) {
  const checked = document.querySelectorAll(".task-row-checkbox:checked");
  if (!checked.length) return null;
  const row = checked[0].closest("tr");
  return row ? row.querySelector(selector) : null;
 }

 document.addEventListener("keydown", (e) => {
  const key = (e.key || "").toLowerCase();
  const isCmd = e.ctrlKey || e.metaKey;
  if (!isCmd || isTypingTarget(e.target)) return;

  const completeModal = document.getElementById("completeModal");
  const noteModal = document.getElementById("noteModal");
  const abandonModal = document.getElementById("abandonModal");

  if (key === "enter") {
   if (completeModal && completeModal.classList.contains("show")) {
    e.preventDefault();
    document.getElementById("confirm-complete-btn")?.click();
    return;
   }
   if (noteModal && noteModal.classList.contains("show")) {
    e.preventDefault();
    document.getElementById("confirm-note-btn")?.click();
    return;
   }
   if (abandonModal && abandonModal.classList.contains("show")) {
    e.preventDefault();
    document.getElementById("confirm-abandon-btn")?.click();
    return;
   }
   const btn = firstActionButton(".complete-btn");
   if (btn) {
    e.preventDefault();
    btn.click();
   }
  }

  if (key === "m") {
   const btn = firstActionButton(".note-btn");
   if (btn) {
    e.preventDefault();
    btn.click();
   }
  }
 });

 els.taskList.addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;

  const { id, ourRef, taskName, dueDate } = btn.dataset;

  if (btn.classList.contains("complete-btn")) {
   document.getElementById("modal-docket-id").value = id;
   document.getElementById("modal-our-ref").textContent = ourRef;
   document.getElementById("modal-task-name").textContent = taskName;
   document.getElementById("modal-due-date").textContent = dueDate ? `Due date: ${dueDate}` : '';
   document.getElementById("complete-evidence-type").value = "memo";
   document.getElementById("complete-description").value = "";
   modals.complete.show();
  }
  else if (btn.classList.contains("abandon-btn")) {
   document.getElementById("abandon-modal-docket-id").value = id;
   document.getElementById("abandon-modal-our-ref").textContent = ourRef;
   document.getElementById("abandon-modal-task-name").textContent = taskName;
   document.getElementById("abandon-reason").value = "";
   modals.abandon.show();
  }
  else if (btn.classList.contains("note-btn")) {
   document.getElementById("note-modal-docket-id").value = id;
   document.getElementById("note-modal-our-ref").textContent = ourRef;
   document.getElementById("note-modal-task-name").textContent = taskName;
   document.getElementById("note-description").value = "";
   modals.note.show();
  }
  else if (btn.classList.contains("reopen-btn")) {
   const ok = await window.AppConfirm(" Task active ? ");
   if (!ok) return;
   try {
    await apiCall(`/tasks/${id}/reopen`, "POST");
    showToast("Task active.");
    loadTasks();
    loadSummary();
   } catch (err) {
    showToast(err.message, "danger");
   }
  }
 });

 // --- Modal Confirm Actions ---

 // 1. Complete
 document.getElementById("confirm-complete-btn").addEventListener("click", async () => {
  const id = document.getElementById("modal-docket-id").value;
  const desc = document.getElementById("complete-description").value;
  const evidenceType = document.getElementById("complete-evidence-type").value || "memo";
  if (!desc.trim()) {
   showToast("Enter completion details.", "warning");
   return;
  }
  try {
   await apiCall(`/tasks/${id}/complete`, "POST", { description: desc, evidence_type: evidenceType });
   modals.complete.hide();
   showToast("Task marked complete.");
   loadTasks();
   loadSummary();
  } catch (e) {
   showToast(e.message || "Could not complete the task.", "danger");
  }
 });

 // 2. Note
 document.getElementById("confirm-note-btn").addEventListener("click", async () => {
  const id = document.getElementById("note-modal-docket-id").value;
  const desc = document.getElementById("note-description").value;
  if (!desc.trim()) {
   showToast("Content enter.", "warning");
   return;
  }
  try {
   await apiCall(`/tasks/${id}/note`, "POST", { description: desc });
   modals.note.hide();
   showToast("Note saved.");
   loadTasks();
  } catch (e) {
   showToast(e.message || "Could not save the note.", "danger");
  }
 });

 // 3. Abandon
 document.getElementById("confirm-abandon-btn").addEventListener("click", async () => {
  const id = document.getElementById("abandon-modal-docket-id").value;
  const reason = document.getElementById("abandon-reason").value;
  try {
   await apiCall(`/tasks/${id}/abandon`, "POST", { reason });
   modals.abandon.hide();
   showToast("Task closed.", "warning");
   loadTasks();
   loadSummary();
  } catch (e) {
   showToast(e.message || "Could not close the task.", "danger");
  }
 });

 // --- Bulk Actions (Optimized) ---

 async function performBulkAction(kind, dataBuilder, successMsg) {
  const selected = Array.from(document.querySelectorAll(".task-row-checkbox:checked"));
  if (selected.length === 0) return;
  const actionLabel = kind === "complete" ? "complete" : "close";

  const ok = await window.AppConfirm(`${actionLabel} ${selected.length} selected task(s)?`);
  if (!ok) return;

  const btn = kind === "complete" ? els.bulk.complete : els.bulk.abandon;
  const originalText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner-border spinner-border-sm"></span> Processing...`;

  try {
   const taskIds = selected
    .map(cb => (cb.dataset.id || "").trim())
    .filter(Boolean);
   const data = await apiCall(`/tasks/bulk-${kind}`, "POST", {
    task_ids: taskIds,
    ...dataBuilder(selected),
   });
   const successCount = Number(data?.processed_count || 0);
   const failureCount = Number(data?.missing_count || 0);

   if (failureCount === 0) {
    showToast(`${successCount} task(s) ${successMsg}.`);
   } else {
    showToast(`${successCount} task(s) processed; ${failureCount} could not be processed.`, "warning");
   }
  } catch (err) {
   showToast(err.message || "Bulk action failed.", "danger");
  } finally {
   btn.innerHTML = originalText;
   btn.disabled = false;
   els.selectAll.checked = false;
  }

  loadTasks();
  loadSummary();
 }

els.bulk.complete.addEventListener("click", () => {
  performBulkAction("complete", () => ({ description: "Bulk completion", evidence_type: "memo" }), "marked complete");
});

els.bulk.abandon.addEventListener("click", () => {
  performBulkAction("abandon", () => ({ reason: "Bulk close" }), "closed");
});

 async function performBulkTransfer() {
  const selected = Array.from(document.querySelectorAll(".task-row-checkbox:checked"));
  if (selected.length === 0) return;

  const targetId = (els.bulk.transferTarget?.value || "").trim();
  if (!targetId) {
   showToast("Select an assignee.", "warning");
   return;
  }

  const targetName = (
   els.bulk.transferTarget?.selectedOptions?.[0]?.textContent || targetId
  ).trim();
  const ok = await window.AppConfirm(`Reassign ${selected.length} selected task(s) to ${targetName}?`);
  if (!ok) return;

  const btn = els.bulk.transfer;
  const originalText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner-border spinner-border-sm"></span> ...`;

  try {
   const taskIds = selected
    .map(cb => (cb.dataset.id || "").trim())
    .filter(Boolean);

   const result = await apiCall("/tasks/bulk-transfer", "POST", {
    task_ids: taskIds,
    target_user_id: Number(targetId),
   });

   const transferred = Number(result.transferred_count || 0);
   const skipped = Number(result.skipped_count || 0);
   const forbidden = Number(result.forbidden_count || 0);
   const missing = Number(result.missing_count || 0);

   if (transferred> 0 && skipped === 0 && forbidden === 0 && missing === 0) {
    showToast(`${transferred} task(s) reassigned to ${targetName}.`);
   } else {
    showToast(
     ` ${transferred}items, items ${skipped}items, Permissions ${forbidden}items, ${missing}items`,
     "warning"
    );
   }

   els.selectAll.checked = false;
   state.lastCheckedTaskBox = null;
   await ensureOwnersLoaded(true);
   await ensureTransferTargetsLoaded(true);
   await loadTasks();
   await loadSummary();
   await loadAssignmentRequests(state.assignmentScope);
  } catch (e) {
   showToast(e.message || "Bulk reassignment failed.", "danger");
  } finally {
   btn.innerHTML = originalText;
   updateBulkButtons();
  }
 }

 els.bulk.transfer.addEventListener("click", () => {
  performBulkTransfer();
 });

 // --- Initialization ---
 (async function init() {
  const urlState = parseUrlState();

  // Apply URL state (except owner; applied after owner list loads)
  if (els.filters.status) els.filters.status.value = urlState.filter;
  if (els.filters.category) els.filters.category.value = urlState.category;
  if (els.filters.ownerRole && Array.from(els.filters.ownerRole.options).some(o => o.value === urlState.ownerRole)) {
   els.filters.ownerRole.value = urlState.ownerRole;
  }
  if (els.filters.days && Array.from(els.filters.days.options).some(o => o.value === urlState.days)) {
   els.filters.days.value = urlState.days;
  }
  if (els.filters.dueFrom) els.filters.dueFrom.value = urlState.dueFrom || "";
  if (els.filters.dueTo) els.filters.dueTo.value = urlState.dueTo || "";

  state.search = urlState.search;
  if (els.search.input) els.search.input.value = urlState.search;

  state.sort = urlState.sort;
  state.order = urlState.order;
  state.page = urlState.page;
  state.bucket = urlState.bucket;
  state.dueAxis = urlState.dueAxis;
  state.mineOnly = Boolean(urlState.mine) && Boolean(CURRENT_USER_OWNER_VALUE);
  applySortIcons();
  const ownersPromise = (urlState.owner || urlState.ownerRole !== DEFAULTS.owner_role)
   ? ensureOwnersLoaded()
   : Promise.resolve();

  if (urlState.owner && els.filters.owner) {
   const select = els.filters.owner;
   const exists = Array.from(select.options).some(opt => opt.value === urlState.owner);
   if (!exists) {
    const opt = document.createElement("option");
    opt.value = urlState.owner;
    opt.textContent = urlState.ownerName || urlState.owner;
    select.appendChild(opt);
   }
   select.value = urlState.owner;
  }

  refreshFilterUi();
  const initialTasksPromise = loadTasks();
  const initialAssignmentPromise = loadAssignmentRequests("inbox");
  scheduleInitialSummaryLoad();

  await ownersPromise;
  if (urlState.owner && els.filters.owner) {
   const select = els.filters.owner;
   const exists = Array.from(select.options).some(opt => opt.value === urlState.owner);
   if (!exists) {
    const opt = document.createElement("option");
    opt.value = urlState.owner;
    opt.textContent = urlState.ownerName || urlState.owner;
    select.appendChild(opt);
   }
   select.value = urlState.owner;
  }

  refreshFilterUi();
  await initialTasksPromise;
  await initialAssignmentPromise;
  syncUrl();
 })();
});
