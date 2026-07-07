/* P1 Productivity:
 * - Tasks (matter-view context): /api/productivity/todos
 * - Ctrl+K Search: /api/productivity/quick-search
 * - Upload docket/task suggestions: /api/productivity/doc-suggest + doc-apply
 * - Undo: /api/productivity/undo/<token>
 */

(function () {
 function $(sel, root) { return (root || document).querySelector(sel); }
 function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }
 function text(el) { return (el && el.textContent ? el.textContent : "").trim(); }
 function normalizeSearchText(value) {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, " ");
 }
 function compactSearchText(value) {
  return normalizeSearchText(value).replace(/[^\w\u3131-\u3163\uac00-\ud7a3]+/g, "");
 }
 function matchesSearchText(haystack, needle) {
  const q = normalizeSearchText(needle);
  if (!q) return true;
  const h = normalizeSearchText(haystack);
  if (h.includes(q)) return true;
  const qCompact = compactSearchText(q);
  return !!qCompact && compactSearchText(h).includes(qCompact);
 }

 function csrfToken() {
  const m = document.querySelector('meta[name="csrf-token"]');
  return m ? (m.getAttribute("content") || "") : "";
 }

 async function api(url, opts) {
  const headers = Object.assign(
   { "Accept": "application/json" },
   (opts && opts.headers) ? opts.headers : {}
  );
  const tok = csrfToken();
  if (tok) headers["X-CSRFToken"] = tok;
  const res = await fetch(url, Object.assign({ credentials: "same-origin" }, opts || {}, { headers }));
  if (window.AppFetch && typeof window.AppFetch.parseJsonResponse === "function") {
   return window.AppFetch.parseJsonResponse(res);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
   const msg = (data && data.error) ? data.error : (res.status + " " + res.statusText);
   throw new Error(msg);
  }
  return data;
 }

 // -------------------------
 // Recent / Pins (localStorage MVP)
 // -------------------------
 const LS_RECENTS = "app.recents.v1";
 const LS_PINS = "app.pins.v1";

 function loadList(key) {
  try { return JSON.parse(localStorage.getItem(key) || "[]") || []; } catch (e) { return []; }
 }
 function saveList(key, arr) {
  try { localStorage.setItem(key, JSON.stringify(arr.slice(0, 50))); } catch (e) {}
 }
 function addRecent(item) {
  if (!item || !item.url) return;
  const arr = loadList(LS_RECENTS);
  const filtered = arr.filter(x => x && x.url !== item.url);
  filtered.unshift(Object.assign({ ts: Date.now() }, item));
  saveList(LS_RECENTS, filtered.slice(0, 10));
 }
 function togglePin(item) {
  const arr = loadList(LS_PINS);
  const exists = arr.find(x => x && x.url === item.url);
  const next = exists ? arr.filter(x => x.url !== item.url) : [item].concat(arr);
  saveList(LS_PINS, next.slice(0, 10));
  return !exists;
 }

 const CMDK_ACTIONS = [
  { type: "action", id: "quickadd:docket", title: "Add docket item", subtitle: "Create a deadline or docket entry", action: { key: "quickadd:docket" } },
  { type: "action", id: "quickadd:workflow", title: "Add task", subtitle: "Create a workflow task", action: { key: "quickadd:workflow" } },
  { type: "action", id: "quickadd:invoice", title: "Create invoice", subtitle: "Start an invoice from a matter", action: { key: "quickadd:invoice" } },
  { type: "action", id: "view:save_current", title: "Save current view", subtitle: "Save the current filters", action: { key: "view:save_current" } },
  { type: "action", id: "view:open", title: "Open saved views", subtitle: "Show saved filters and shortcuts", action: { key: "view:open" } }
 ];
 const cmdkState = { items: [], index: -1 };

 // Page meta hook for matter views.
 function harvestPageMeta() {
  const meta = $("#app-page-meta");
  if (!meta) return null;
  return {
   type: meta.dataset.type || "page",
   id: meta.dataset.id || "",
   title: meta.dataset.title || document.title || "",
   subtitle: meta.dataset.subtitle || "",
   url: meta.dataset.url || location.pathname
  };
 }

 // -------------------------
 // Floating + Home panel UI
 // -------------------------
 function isDesktopViewport() {
  if (!window.matchMedia) return true;
  return window.matchMedia("(min-width: 992px)").matches;
 }

 function todoTargets() {
  const desktop = isDesktopViewport();
  return [
   { list: $("#ipmTodoList"), badge: $("#ipmTodoBadge"), scope: $("#ipmTodoScopeLabel") },
   { list: $("#ipmTodoListMain"), badge: $("#ipmTodoBadgeMain"), scope: $("#ipmTodoScopeLabelMain") },
  ].filter(t => t.list && (desktop || t.list.id !== "ipmTodoList"));
 }

 function buildTodoItem(it) {
  const refLine = it.case_ref
   ? `<div class="small text-muted">${escapeHtml(it.case_ref || "")}</div>`
   : "";
  const dday = it.dday>= 0 ? "-" + it.dday : "+" + Math.abs(it.dday);
  return `
   <div class="me-2">
    <div class="small fw-semibold">${escapeHtml(it.title || "")}</div>
    ${refLine}
    <div class="small text-muted">D${dday} · ${escapeHtml(it.due_date || "")}</div>
   </div>
   <span class="badge bg-${escapeHtml(it.badge || "secondary")}">${it.statutory ? "Statutory" : "Deadline"}</span>
  `;
 }

 function renderTodos(items) {
  const targets = todoTargets();
  if (!targets.length) return;
  targets.forEach((target) => {
   if (target.badge) target.badge.textContent = String(items.length || 0);
   target.list.innerHTML = "";
   if (!items.length) {
    target.list.innerHTML = '<div class="text-muted small">No tasks to show.</div>';
    return;
   }
   for (const it of items) {
    const a = document.createElement("a");
    a.href = it.url || "#";
    a.className = "list-group-item list-group-item-action d-flex justify-content-between align-items-center";
    a.innerHTML = buildTodoItem(it);
    a.addEventListener("click", function () {
     addRecent(harvestPageMeta() || { type: "page", title: document.title, subtitle: "", url: location.pathname });
    });
    target.list.appendChild(a);
   }
  });
 }

 function escapeHtml(s) {
  return String(s || "")
   .replaceAll("&", "&amp;")
   .replaceAll("<", "&lt;")
   .replaceAll(">", "&gt;")
   .replaceAll('"', "&quot;")
   .replaceAll("'", "&#039;");
 }

 async function refreshTodos() {
  const targets = todoTargets();
  if (!targets.length) return;
  targets.forEach((target) => {
   if (target.scope) target.scope.textContent = "(all)";
  });
  const data = await api("/api/productivity/todos", { method: "GET" });
  renderTodos((data && data.items) ? data.items : []);
 }

 function hasInlineTodoList() {
  return Boolean($("#ipmTodoListMain"));
 }

 function scheduleInitialTodoRefresh() {
  if (!hasInlineTodoList()) return;
  const run = () => refreshTodos().catch(() => {});
  if (typeof window.requestIdleCallback === "function") {
   window.requestIdleCallback(run, { timeout: 1200 });
   return;
  }
  window.setTimeout(run, 450);
 }

 function showToast(html) {
  const wrap = $("#ipmToastWrap");
  if (!wrap) return;
  const div = document.createElement("div");
  div.className = "card p-2 shadow-sm mb-2";
  div.innerHTML = html;
  wrap.appendChild(div);
  setTimeout(() => { try { div.remove(); } catch (e) {} }, 12000);
  return div;
 }

 function bindUndoButton(token) {
  if (!token) return;
  const wrap = $("#ipmToastWrap");
  if (!wrap) return;
  const btn = wrap.querySelector('button[data-undo="' + token + '"]');
  if (!btn) return;
  btn.addEventListener("click", async () => {
   try {
    const r = await api("/api/productivity/undo/" + encodeURIComponent(token), { method: "POST" });
    showToast(`<div class="small">Undo: ${escapeHtml(JSON.stringify(r.deleted || r))}</div>`);
   } catch (e) {
    showToast(`<div class="small text-danger">Undo : ${escapeHtml(e.message || String(e))}</div>`);
   }
  });
 }

 // -------------------------
 // Ctrl+K Command palette
 // -------------------------
 function openCmdk() {
  const box = $("#ipmCmdk");
  if (!box) return;
  box.classList.add("show");
  const input = $("#ipmCmdkInput");
  if (input) {
   input.value = "";
   input.focus();
   renderCmdkRecents();
  }
 }
 function closeCmdk() {
  const box = $("#ipmCmdk");
  if (!box) return;
  box.classList.remove("show");
 }

 function isCmdkOpen() {
  const box = $("#ipmCmdk");
  return Boolean(box && box.classList.contains("show"));
 }

 function renderCmdkItems(items) {
  const list = $("#ipmCmdkResults");
  if (!list) return;
  if (!items.length) {
   list.innerHTML = '<div class="text-muted small p-2">No search results.</div>';
   return;
  }
  list.innerHTML = items.map((it, idx) => `
   <button type="button" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center" data-idx="${idx}">
    <div class="text-start">
     ${it._section ? `<div class="small text-muted">${escapeHtml(it._section || "")}</div>` : ""}
     <div class="fw-semibold">${escapeHtml(it.title || "")}</div>
     ${it.subtitle ? `<div class="small text-muted">${escapeHtml(it.subtitle || "")}</div>` : ""}
    </div>
    <span class="badge bg-secondary">${escapeHtml(it.type || "item")}</span>
   </button>
  `).join("");
  $all("button[data-idx]", list).forEach(btn => {
   btn.addEventListener("click", () => {
    const i = parseInt(btn.dataset.idx || "0", 10);
    const it = cmdkState.items[i];
    if (it) executeCmdkItem(it, { newTab: false });
   });
  });
  cmdkState.index = items.length ? 0 : -1;
  highlightCmdk(cmdkState.index);
 }

 function highlightCmdk(idx) {
  const list = $("#ipmCmdkResults");
  if (!list) return;
  $all("button[data-idx]", list).forEach(btn => {
   const i = parseInt(btn.dataset.idx || "-1", 10);
   if (i === idx) btn.classList.add("active");
   else btn.classList.remove("active");
  });
 }

 function moveCmdkIndex(delta) {
  if (!cmdkState.items.length) return;
  let next = cmdkState.index + delta;
  if (next < 0) next = cmdkState.items.length - 1;
  if (next>= cmdkState.items.length) next = 0;
  cmdkState.index = next;
  highlightCmdk(cmdkState.index);
 }

 function parseCmdkInput(raw) {
  let query = (raw || "").trim();
  let mode = "search";
  let typeFilter = "";
  if (query.startsWith(">")) {
   mode = "action";
   query = query.slice(1).trim();
  } else if (query.startsWith("#")) {
   mode = "view";
   typeFilter = "view";
   query = query.slice(1).trim();
  } else if (query.startsWith("@")) {
   const parts = query.split(/\s+/);
   const first = parts.shift() || "";
   typeFilter = first.replace(/^@/, "").toLowerCase();
   query = parts.join(" ").trim();
  }
  return { mode, query, typeFilter };
 }

 function filterCmdkActions(q) {
  const needle = (q || "").trim();
  if (!needle) return CMDK_ACTIONS.slice();
  return CMDK_ACTIONS.filter(it => {
   const hay = `${it.title || ""} ${it.subtitle || ""} ${it.id || ""}`;
   return matchesSearchText(hay, needle);
  });
 }

 function executeCmdkItem(it, opts) {
  if (!it) return;
  const newTab = Boolean(opts && opts.newTab);
  if (it.action || it.type === "action") {
   const shouldClose = runCmdkAction((it.action || {}).key || it.id || "");
   if (shouldClose) closeCmdk();
   return;
  }
  if (it.url) {
   addRecent({ type: it.type, id: it.id, title: it.title, subtitle: it.subtitle, url: it.url });
   if (newTab) window.open(it.url, "_blank", "noopener");
   else if (window.AppDrilldown && typeof window.AppDrilldown.navigate === "function") {
    window.AppDrilldown.navigate(it.url);
   } else {
    location.href = it.url;
   }
  }
 }

 function runCmdkAction(key) {
  const k = (key || "").trim();
  if (!k) return true;
  if (k === "quickadd:docket") { openQuickAdd("docket"); return true; }
  if (k === "quickadd:workflow") { openQuickAdd("workflow"); return true; }
  if (k === "quickadd:invoice") { openQuickAdd("invoice"); return true; }
  if (k === "view:save_current" && window.AppViews && window.AppViews.promptSaveCurrent) {
   window.AppViews.promptSaveCurrent();
   return true;
  }
  if (k === "view:open") {
   openCmdk();
   const input = $("#ipmCmdkInput");
   if (input) {
    input.value = "#";
    runCmdkSearch("#").catch(() => {});
   }
   return false;
  }
  return true;
 }

 function renderCmdkRecents() {
  const pins = loadList(LS_PINS);
  const recents = loadList(LS_RECENTS);
  const items = ([]).concat(
   pins.map(x => Object.assign({ _section: "" }, x)),
   recents.map(x => Object.assign({ _section: "Recent" }, x))
  );
  cmdkState.items = items;
  if (!items.length) {
   const list = $("#ipmCmdkResults");
   if (list) list.innerHTML = '<div class="text-muted small p-2">No recent or pinned items.</div>';
   cmdkState.index = -1;
   return;
  }
  renderCmdkItems(items);
  hydrateRecentSubtitles(items).catch(() => {});
 }

 function _matterIdFromUrl(url) {
  const raw = (url || "").toString().trim();
  if (!raw) return "";
  const m = raw.match(/\/case\/([^?#/]+)/i);
  return m ? (m[1] || "") : "";
 }

 async function hydrateRecentSubtitles(items) {
  // Improve UX: recents/pins that only had our_ref and URL now show matter/client context.
  const targets = (items || []).filter(it => it && it.type === "matter" && !it.subtitle);
  if (!targets.length) return;

  const updatesByUrl = new Map();
  for (const it of targets.slice(0, 10)) {
   const mid = (it.id || _matterIdFromUrl(it.url) || "").toString().trim();
   if (!mid) continue;
   try {
    const data = await api("/api/productivity/quick-search?type=matter&limit=1&q=" + encodeURIComponent(mid), { method: "GET" });
    const found = (data && data.items && data.items[0]) ? data.items[0] : null;
    const subtitle = found && found.subtitle ? String(found.subtitle || "").trim() : "";
    if (!subtitle) continue;
    it.subtitle = subtitle;
    if (it.url) updatesByUrl.set(it.url, subtitle);
   } catch (e) {}
  }

  if (!updatesByUrl.size) return;

  function applyUpdates(key) {
   const arr = loadList(key);
   let changed = false;
   for (const row of arr) {
    if (!row || !row.url) continue;
    const sub = updatesByUrl.get(row.url);
    if (sub && !row.subtitle) {
     row.subtitle = sub;
     changed = true;
    }
   }
   if (changed) saveList(key, arr);
  }
  applyUpdates(LS_RECENTS);
  applyUpdates(LS_PINS);

  // Rerender if we're still showing recents.
  renderCmdkItems(cmdkState.items);
 }

 let cmdkTimer = null;
 async function runCmdkSearch(raw) {
  const list = $("#ipmCmdkResults");
  if (!list) return;
  const parsed = parseCmdkInput(raw);
  if (!raw || !raw.trim()) {
   renderCmdkRecents();
   return;
  }

  if (parsed.mode === "action") {
   const items = filterCmdkActions(parsed.query);
   cmdkState.items = items;
   renderCmdkItems(items);
   return;
  }

  list.innerHTML = '<div class="text-muted small p-2">Search …</div>';
  const qs = new URLSearchParams();
  qs.set("q", parsed.query || "");
  if (parsed.typeFilter) qs.set("type", parsed.typeFilter);
  const data = await api("/api/productivity/quick-search?" + qs.toString(), { method: "GET" });
  const items = (data && data.items) ? data.items : [];
  cmdkState.items = items;
  if (!items.length) {
   cmdkState.index = -1;
   list.innerHTML = '<div class="text-muted small p-2">No search results.</div>';
   return;
  }
  renderCmdkItems(items);
 }

 // -------------------------
 // Quick Add modal
 // -------------------------
 const quickAddState = { matter: null, tab: "docket", searchTimer: null };

 function openQuickAdd(tabKey) {
  const modal = $("#ipmQuickAdd");
  if (!modal) return;
  modal.classList.add("show");
  modal.setAttribute("aria-hidden", "false");
  syncQuickAddMatterFromMeta();
  const results = $("#ipmQuickAddMatterResults");
  if (results) results.innerHTML = "";
  showQuickAddError("#ipmQuickAddDocketError", "");
  showQuickAddError("#ipmQuickAddWorkflowError", "");
  showQuickAddError("#ipmQuickAddInvoiceError", "");
  if (tabKey) setQuickAddTab(tabKey);
  focusQuickAddField();
 }

 function closeQuickAdd() {
  const modal = $("#ipmQuickAdd");
  if (!modal) return;
  modal.classList.remove("show");
  modal.setAttribute("aria-hidden", "true");
 }

 function setQuickAddTab(key) {
  quickAddState.tab = key || "docket";
  const tabs = $all("#ipmQuickAddTabs .nav-link");
  tabs.forEach(btn => {
   const active = btn.dataset.pane === quickAddState.tab;
   btn.classList.toggle("active", active);
  });
  $all(".app-quickadd-pane").forEach(pane => {
   pane.classList.toggle("active", pane.dataset.pane === quickAddState.tab);
  });
 }

 function focusQuickAddField() {
  const pane = $(`.app-quickadd-pane[data-pane="${quickAddState.tab}"]`);
  if (!pane) return;
  const field = pane.querySelector("input, select, textarea");
  if (field) field.focus();
 }

 function syncQuickAddMatterFromMeta() {
  if (quickAddState.matter) {
   renderQuickAddMatter();
   return;
  }
  const meta = $("#app-page-meta");
  const isMatter = meta && meta.dataset.type === "matter";
  if (isMatter && meta.dataset.id) {
   quickAddState.matter = {
    id: meta.dataset.id,
    title: meta.dataset.title || meta.dataset.id
   };
  }
  renderQuickAddMatter();
 }

 function renderQuickAddMatter() {
  const input = $("#ipmQuickAddMatterInput");
  const selected = $("#ipmQuickAddMatterSelected");
  const hidden = $("#ipmQuickAddMatterId");
  if (!input || !selected || !hidden) return;
  const matter = quickAddState.matter;
  hidden.value = matter ? matter.id : "";
  if (matter) {
   input.value = matter.title || matter.id;
   selected.textContent = `Selected matter: ${matter.title || matter.id} (#${matter.id})`;
  } else {
   selected.textContent = "Select a matter.";
  }
 }

 function clearQuickAddMatter() {
  quickAddState.matter = null;
  const results = $("#ipmQuickAddMatterResults");
  if (results) results.innerHTML = "";
  const input = $("#ipmQuickAddMatterInput");
  if (input) input.value = "";
  renderQuickAddMatter();
 }

 function selectQuickAddMatter(item) {
  if (!item || !item.id) return;
  quickAddState.matter = { id: item.id, title: item.title || item.id };
  const results = $("#ipmQuickAddMatterResults");
  if (results) results.innerHTML = "";
  renderQuickAddMatter();
 }

 async function searchQuickAddMatter(q) {
  const results = $("#ipmQuickAddMatterResults");
  if (!results) return;
  const query = (q || "").trim();
  if (!query || query.length < 2) {
   results.innerHTML = "";
   return;
  }
  results.innerHTML = '<div class="text-muted small p-2">Search …</div>';
  const data = await api("/api/productivity/quick-search?type=matter&q=" + encodeURIComponent(query), { method: "GET" });
  const items = (data && data.items) ? data.items : [];
  if (!items.length) {
   results.innerHTML = '<div class="text-muted small p-2">No search results.</div>';
   return;
  }
  results.innerHTML = items.map((it, idx) => `
   <button type="button" class="list-group-item list-group-item-action" data-idx="${idx}">
    <div class="fw-semibold">${escapeHtml(it.title || "")}</div>
    <div class="small text-muted">${escapeHtml(it.subtitle || "")}</div>
   </button>
  `).join("");
  $all("button[data-idx]", results).forEach(btn => {
   btn.addEventListener("click", () => {
    const i = parseInt(btn.dataset.idx || "0", 10);
    const it = items[i];
    selectQuickAddMatter(it);
   });
  });
 }

 function showQuickAddError(id, msg) {
  const el = $(id);
  if (!el) return;
  if (msg) {
   el.textContent = msg;
   el.classList.remove("d-none");
  } else {
   el.textContent = "";
   el.classList.add("d-none");
  }
 }

 async function submitQuickAddDocket(form) {
  const matterId = (quickAddState.matter && quickAddState.matter.id) || $("#ipmQuickAddMatterId")?.value;
  if (!matterId) {
   showQuickAddError("#ipmQuickAddDocketError", "Select a matter first.");
   return;
  }
  const title = (form.querySelector('input[name="title"]')?.value || "").trim();
  const due = (form.querySelector('input[name="due_date"]')?.value || "").trim();
  const priority = (form.querySelector('select[name="priority"]')?.value || "").trim();
  const assignee = (form.querySelector('input[name="assignee_id"]')?.value || "").trim();
  if (!title || !due) {
   showQuickAddError("#ipmQuickAddDocketError", "Enter a docket item and due date.");
   return;
  }
  showQuickAddError("#ipmQuickAddDocketError", "");
  const data = await api("/api/quickadd/docket", {
   method: "POST",
   headers: { "Content-Type": "application/json" },
   body: JSON.stringify({
    matter_id: matterId,
    title,
    due_date: due,
    priority,
    assignee_id: assignee || null
   })
  });
  const undo = data.undo_token;
  const msg = `<div class="small">
   Docket item created.
   ${data.url ? `<a class="btn btn-sm btn-outline-primary ms-2" href="${escapeHtml(data.url)}"> Open</a>` : ""}
   ${undo ? `<button type="button" class="btn btn-sm btn-outline-secondary ms-2" data-undo="${escapeHtml(undo)}">Undo</button>` : ""}
  </div>`;
  showToast(msg);
  bindUndoButton(undo);
  closeQuickAdd();
 }

 async function submitQuickAddWorkflow(form) {
  const matterId = (quickAddState.matter && quickAddState.matter.id) || $("#ipmQuickAddMatterId")?.value;
  if (!matterId) {
   showQuickAddError("#ipmQuickAddWorkflowError", "Select a matter first.");
   return;
  }
  const title = (form.querySelector('input[name="title"]')?.value || "").trim();
  const legalDue = (form.querySelector('input[name="legal_due_date"]')?.value || "").trim();
  const templateKey = (form.querySelector('select[name="template_key"]')?.value || "").trim();
  const assignee = (form.querySelector('input[name="assignee_id"]')?.value || "").trim();
  const managerAssignee = (
   form.querySelector('input[name="manager_assignee_id"]')?.value ||
   form.querySelector('input[name="reviewer_id"]')?.value ||
   ""
  ).trim();
  const priority = (form.querySelector('select[name="priority"]')?.value || "").trim();
  if (!title) {
   showQuickAddError("#ipmQuickAddWorkflowError", "Enter a task title.");
   return;
  }
  showQuickAddError("#ipmQuickAddWorkflowError", "");
  const data = await api("/api/quickadd/workflow", {
   method: "POST",
   headers: { "Content-Type": "application/json" },
   body: JSON.stringify({
    matter_id: matterId,
    title,
    legal_due_date: legalDue || null,
    template_key: templateKey || null,
    assignee_id: assignee || null,
    manager_assignee_id: managerAssignee || null,
    priority: priority || null
   })
  });
  const undo = data.undo_token;
  const c = data.created || {};
  const msg = `<div class="small">
   Created ${c.workflows || 0} task(s) and ${c.dockets || 0} docket item(s).
   ${undo ? `<button type="button" class="btn btn-sm btn-outline-secondary ms-2" data-undo="${escapeHtml(undo)}">Undo</button>` : ""}
  </div>`;
  showToast(msg);
  bindUndoButton(undo);
  closeQuickAdd();
 }

 async function submitQuickAddInvoice(form) {
  const matterId = (quickAddState.matter && quickAddState.matter.id) || $("#ipmQuickAddMatterId")?.value;
  if (!matterId) {
   showQuickAddError("#ipmQuickAddInvoiceError", "Select a matter first.");
   return;
  }
  showQuickAddError("#ipmQuickAddInvoiceError", "");
  const data = await api("/api/quickadd/invoice", {
   method: "POST",
   headers: { "Content-Type": "application/json" },
   body: JSON.stringify({ matter_id: matterId })
  });
  if (data && data.url) {
   if (window.AppDrilldown && typeof window.AppDrilldown.navigate === "function") {
    window.AppDrilldown.navigate(data.url);
   } else {
    location.href = data.url;
   }
  }
  closeQuickAdd();
 }

 // -------------------------
 // Doc suggest modal
 // -------------------------
 function openDocSuggest() {
  const modal = $("#ipmDocSuggest");
  if (!modal) return;
  modal.classList.remove("d-none");
  modal.classList.add("show");
  modal.setAttribute("aria-hidden", "false");
  const fileInput = $("#ipmDocSuggestFile");
  if (fileInput) fileInput.value = "";
  const results = $("#ipmDocSuggestResults");
  if (results) {
   results.innerHTML = '<div class="text-muted small">Upload a PDF, text, or email file to suggest docket items and tasks.</div>';
   results.dataset.suggestions = "[]";
  }
  const applyBtn = $("#ipmDocApplyBtn");
  if (applyBtn) applyBtn.disabled = true;
 }
 function closeDocSuggest() {
  const modal = $("#ipmDocSuggest");
  if (!modal) return;
  modal.classList.remove("show");
  modal.setAttribute("aria-hidden", "true");
 }

 async function runDocSuggest() {
  const fileInput = $("#ipmDocSuggestFile");
  const results = $("#ipmDocSuggestResults");
  const applyBtn = $("#ipmDocApplyBtn");
  if (!fileInput || !results) return;
  const f = fileInput.files && fileInput.files[0];
  if (!f) { results.innerHTML = '<div class="text-danger small">Select a file first.</div>'; return; }

  const meta = $("#app-page-meta");
  const matterId = meta && meta.dataset.type === "matter" ? meta.dataset.id : "";

  results.innerHTML = '<div class="text-muted small">Analyzing...</div>';
  if (applyBtn) applyBtn.disabled = true;

  const fd = new FormData();
  fd.append("file", f);
  if (matterId) fd.append("matter_id", matterId);

  const data = await api("/api/productivity/doc-suggest", { method: "POST", body: fd });
  const suggestions = (data && data.suggestions) ? data.suggestions : [];
  if (!suggestions.length) {
   results.innerHTML = '<div class="text-muted small">No suggestions found.</div>';
   return;
  }
  results.innerHTML = `
   <div class="list-group">
    ${suggestions.map((s, idx) => `
     <label class="list-group-item d-flex gap-2 align-items-start">
      <input class="form-check-input mt-1" type="checkbox" data-idx="${idx}" checked>
      <div>
       <div class="fw-semibold">${escapeHtml(s.kind || "")}: ${escapeHtml(s.title || "")}</div>
       <div class="small text-muted">${escapeHtml(s.due_date || s.legal_due_date || "")}</div>
      </div>
     </label>
    `).join("")}
   </div>
  `;
  results.dataset.suggestions = JSON.stringify(suggestions);
  const hasMatter = Boolean(matterId);
  if (applyBtn) applyBtn.disabled = !hasMatter;
  if (!hasMatter) {
   const note = document.createElement("div");
   note.className = "text-muted small mt-2";
   note.textContent = "Open a matter before applying suggestions.";
   results.appendChild(note);
  }
 }

 async function applyDocSuggest() {
  const results = $("#ipmDocSuggestResults");
  const applyBtn = $("#ipmDocApplyBtn");
  if (!results) return;

  const meta = $("#app-page-meta");
  const matterId = meta && meta.dataset.type === "matter" ? meta.dataset.id : "";
  if (!matterId) {
   showToast('<div class="small text-danger">Open a matter before applying suggestions.</div>');
   return;
  }

  let suggestions = [];
  try { suggestions = JSON.parse(results.dataset.suggestions || "[]"); } catch (e) { suggestions = []; }
  if (!suggestions.length) return;

  const enabled = new Set();
  $all('input[type="checkbox"][data-idx]', results).forEach(chk => {
   if (chk.checked) enabled.add(parseInt(chk.dataset.idx || "0", 10));
  });
  const filtered = suggestions.filter((_, idx) => enabled.has(idx));
  if (!filtered.length) {
   showToast('<div class="small text-muted">No suggestions selected.</div>');
   return;
  }

  if (applyBtn) applyBtn.disabled = true;
  const data = await api("/api/productivity/doc-apply", {
   method: "POST",
   headers: { "Content-Type": "application/json" },
   body: JSON.stringify({ matter_id: matterId, suggestions: filtered })
  });

  const undo = data.undo_token;
  const c = data.created || {};
  const msg = `<div class="small">
    Applied: ${c.workflows || 0} task(s) / ${c.dockets || 0} docket item(s)
   ${undo ? `<button type="button" class="btn btn-sm btn-outline-secondary ms-2" data-undo="${escapeHtml(undo)}">Undo</button>` : ""}
  </div>`;
  showToast(msg);
  bindUndoButton(undo);

  closeDocSuggest();
 }

 function isTypingTarget(el) {
  if (!el) return false;
  const tag = (el.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  if (el.isContentEditable) return true;
  return false;
 }

 function triggerShortcut(name) {
  const el = document.querySelector(`[data-shortcut="${name}"]`);
  if (!el) return false;
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  return true;
 }

 // -------------------------
 // Boot
 // -------------------------
 function boot() {
  // track recent
  const pm = harvestPageMeta();
  if (pm && pm.type && pm.url) addRecent(pm);

  // buttons
  const openBtn = $("#ipmProdOpenBtn");
  const panel = $("#ipmProdPanel");
  if (openBtn && panel) {
   openBtn.addEventListener("click", async () => {
    panel.classList.toggle("d-none");
    if (!panel.classList.contains("d-none")) {
     try { await refreshTodos(); } catch (e) {}
    }
   });
  }
  $all("[data-todo-refresh]").forEach((btn) => {
   btn.addEventListener("click", () => refreshTodos().catch(() => {}));
  });

  const cmdkBtn = $("#ipmCmdkBtn");
  if (cmdkBtn) cmdkBtn.addEventListener("click", openCmdk);
  const docBtn = $("#ipmDocSuggestBtn");
  if (docBtn) docBtn.addEventListener("click", openDocSuggest);
  const quickAddBtn = $("#ipmQuickAddBtn");
  if (quickAddBtn) quickAddBtn.addEventListener("click", () => openQuickAdd("docket"));

  // cmdk events + shortcuts
  document.addEventListener("keydown", (e) => {
   /* : Add   
   if ((e.altKey || false) && (e.key || "").toLowerCase() === "n") {
    e.preventDefault();
    openQuickAdd("docket");
    return;
   }
   */
   const key = (e.key || "").toLowerCase();
   const isCmd = e.ctrlKey || e.metaKey;
   const isK = (e.key || "").toLowerCase() === "k";
   if (isCmd && isK) { e.preventDefault(); openCmdk(); return; }
   if (key === "escape") { closeCmdk(); closeDocSuggest(); closeQuickAdd(); return; }
   if (isCmd && isTypingTarget(e.target)) return;
   if (isCmd && key === "u") {
    e.preventDefault();
    triggerShortcut("upload");
    return;
   }
   if (isCmd && key === "m") {
    e.preventDefault();
    triggerShortcut("memo");
    return;
   }
  });
  const cmdkBackdrop = $("#ipmCmdkBackdrop");
  if (cmdkBackdrop) cmdkBackdrop.addEventListener("click", closeCmdk);
  document.addEventListener("click", (e) => {
   const trigger = e.target.closest?.("[data-cmdk-open]");
   if (!trigger) return;
   e.preventDefault();
   openCmdk();
  });
  const cmdkInput = $("#ipmCmdkInput");
  if (cmdkInput) {
   cmdkInput.addEventListener("input", () => {
    const raw = cmdkInput.value;
    if (cmdkTimer) clearTimeout(cmdkTimer);
    cmdkTimer = setTimeout(() => runCmdkSearch(raw).catch(() => {}), 150);
   });
   cmdkInput.addEventListener("keydown", (e) => {
    if (!isCmdkOpen()) return;
    if (e.key === "ArrowDown") { e.preventDefault(); moveCmdkIndex(1); }
    if (e.key === "ArrowUp") { e.preventDefault(); moveCmdkIndex(-1); }
    if (e.key === "Enter") {
     e.preventDefault();
     const it = cmdkState.items[cmdkState.index];
     executeCmdkItem(it, { newTab: e.ctrlKey || e.metaKey });
    }
    if ((e.ctrlKey || e.metaKey) && (e.key || "").toLowerCase() === "p") {
     const it = cmdkState.items[cmdkState.index];
     if (it && it.url) {
      e.preventDefault();
      const pinned = togglePin({ type: it.type, id: it.id, title: it.title, url: it.url });
      showToast(`<div class="small">${pinned ? "Pinned" : "Unpinned"}</div>`);
      if (!cmdkInput.value.trim()) renderCmdkRecents();
     }
    }
   });
  }

  // quick add modal events
  const qaBackdrop = $("#ipmQuickAddBackdrop");
  if (qaBackdrop) qaBackdrop.addEventListener("click", closeQuickAdd);
  const qaClose = $("#ipmQuickAddCloseBtn");
  if (qaClose) qaClose.addEventListener("click", closeQuickAdd);
  $all("#ipmQuickAddTabs .nav-link").forEach(btn => {
   btn.addEventListener("click", () => {
    setQuickAddTab(btn.dataset.pane || "docket");
    focusQuickAddField();
   });
  });

  const qaMatterInput = $("#ipmQuickAddMatterInput");
  if (qaMatterInput) {
   qaMatterInput.addEventListener("input", () => {
    const val = qaMatterInput.value || "";
    if (quickAddState.matter && val !== (quickAddState.matter.title || quickAddState.matter.id)) {
     quickAddState.matter = null;
     renderQuickAddMatter();
    }
    if (quickAddState.searchTimer) clearTimeout(quickAddState.searchTimer);
    quickAddState.searchTimer = setTimeout(() => searchQuickAddMatter(val).catch(() => {}), 180);
   });
  }
  const qaClear = $("#ipmQuickAddMatterClearBtn");
  if (qaClear) qaClear.addEventListener("click", clearQuickAddMatter);

  const docketForm = $("#ipmQuickAddDocketForm");
  if (docketForm) docketForm.addEventListener("submit", (e) => {
   e.preventDefault();
   submitQuickAddDocket(docketForm).catch(err => {
    showQuickAddError("#ipmQuickAddDocketError", err.message || "Could not create the docket item. Check the selected matter and due date.");
   });
  });
  const wfForm = $("#ipmQuickAddWorkflowForm");
  if (wfForm) wfForm.addEventListener("submit", (e) => {
   e.preventDefault();
   submitQuickAddWorkflow(wfForm).catch(err => {
    showQuickAddError("#ipmQuickAddWorkflowError", err.message || "Could not create the task. Check the selected matter and required fields.");
   });
  });
  const invForm = $("#ipmQuickAddInvoiceForm");
  if (invForm) invForm.addEventListener("submit", (e) => {
   e.preventDefault();
   submitQuickAddInvoice(invForm).catch(err => {
    showQuickAddError("#ipmQuickAddInvoiceError", err.message || "Could not create the invoice. Check the selected matter and permissions.");
   });
  });

  // doc suggest modal events
  const docClose = $("#ipmDocCloseBtn");
  if (docClose) docClose.addEventListener("click", closeDocSuggest);
  const docBackdrop = $("#ipmDocBackdrop");
  if (docBackdrop) docBackdrop.addEventListener("click", closeDocSuggest);
  const docRun = $("#ipmDocRunBtn");
  if (docRun) docRun.addEventListener("click", () => runDocSuggest().catch(e => {
   showToast(`<div class="small text-danger">Analysis failed: ${escapeHtml(e.message || String(e))}</div>`);
  }));
  const docApply = $("#ipmDocApplyBtn");
  if (docApply) docApply.addEventListener("click", () => applyDocSuggest().catch(e => {
   showToast(`<div class="small text-danger">Apply : ${escapeHtml(e.message || String(e))}</div>`);
  }));

  // Only pages with an inline todo list need data during initial load.
  // Floating-panel pages fetch on open, keeping normal navigation lighter.
  scheduleInitialTodoRefresh();
 }

 if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
 else boot();

 window.AppCmdk = { open: openCmdk, close: closeCmdk };
})();
