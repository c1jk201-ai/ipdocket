/* Save (list filters) */
(function () {
 function $(sel, root) { return (root || document).querySelector(sel); }
 function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

 function csrfToken() {
  const m = document.querySelector('meta[name="csrf-token"]');
  return m ? (m.getAttribute("content") || "") : "";
 }

 function ipmAlert(message, opts) {
  try {
   if (window.AppAlert) return window.AppAlert(message, opts);
  } catch (e) {}
  return Promise.resolve();
 }

 function ipmConfirm(message) {
  try {
   if (window.AppConfirm) return window.AppConfirm(message);
  } catch (e) {}
  return Promise.resolve(false);
 }

 function ipmPrompt(message, defaultValue, opts) {
  try {
   if (window.AppPrompt) return window.AppPrompt(message, defaultValue, opts);
  } catch (e) {}
  return Promise.resolve(null);
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

 function notify(message, opts) {
  const wrap = $("#ipmToastWrap");
  const text = (message === null || message === undefined) ? "" : String(message);
  if (!wrap) {
   ipmAlert(text);
   return;
  }
  const div = document.createElement("div");
  div.className = "card p-2 shadow-sm mb-2";
  const line = document.createElement("div");
  line.className = "small";
  if (opts && opts.className) line.className += " " + opts.className;
  line.textContent = text;
  div.appendChild(line);
  wrap.appendChild(div);
  setTimeout(() => { try { div.remove(); } catch (e) {} }, 8000);
 }

 function confirmAction(message) {
  return ipmConfirm(message);
 }

const MODULE_BY_PATH = [
  { prefix: "/deadline/calendar", module: "deadline_calendar_month" },
  { prefix: "/case", module: "case_list" },
  { prefix: "/accounting/invoice-system/clients", module: "invoice_client_list" },
  { prefix: "/deadline", module: "docket_list" },
  { prefix: "/accounting/invoice-system/invoices", module: "invoice_list" },
  { prefix: "/worklog", module: "worklog" },
  { prefix: "/renewal/calendar", module: "renewal_calendar_month" },
  { prefix: "/renewal/giveup", module: "renewal_giveup" },
  { prefix: "/renewal", module: "renewal_fees" },
 ];

 function resolveModule() {
  const bar = $("[data-view-module]");
  if (bar && bar.dataset.viewModule) return bar.dataset.viewModule;
  const path = location.pathname || "";
  for (const row of MODULE_BY_PATH) {
   if (path.startsWith(row.prefix)) return row.module;
  }
  return "";
 }

 function collectPayload() {
  const params = new URLSearchParams(location.search || "");
  const filters = {};
  params.forEach((value, key) => {
   if (key === "view_id" || key === "page") return;
   if (filters[key] !== undefined) {
    if (!Array.isArray(filters[key])) filters[key] = [filters[key]];
    filters[key].push(value);
   } else {
    filters[key] = value;
   }
  });
  const payload = {
   path: location.pathname,
   filters,
   sort: params.get("sort") || null,
   columns: params.get("columns") || null,
   per_page: params.get("per_page") || null,
  };
  return payload;
 }

 async function fetchViews(module) {
  if (!module) return [];
  const data = await api("/api/views?module=" + encodeURIComponent(module), { method: "GET" });
  return (data && data.items) ? data.items : [];
 }

 function renderSelect(select, views, currentId) {
  if (!select) return;
  select.innerHTML = '<option value="">Default</option>';
  views.forEach(v => {
   const opt = document.createElement("option");
   opt.value = v.id;
   const tags = [];
   if (v.scope === "system") tags.push("");
   if (v.scope === "team") tags.push("");
   if (v.is_default) tags.push("Default");
   opt.textContent = v.name + (tags.length ? ` (${tags.join(",")})` : "");
   if (currentId && String(currentId) === String(v.id)) opt.selected = true;
   select.appendChild(opt);
  });
 }

 function shouldAutoApplyDefault() {
  const params = new URLSearchParams(location.search || "");
  if (params.get("view_id")) return false;
  for (const key of params.keys()) {
   if (key === "page" || key === "view_id") continue;
   return false;
  }
  return true;
 }

 async function initViewBar(bar) {
  const module = bar.dataset.viewModule || "";
  const auto = bar.dataset.viewAuto !== "0";
  const select = bar.querySelector("[data-view-select]");
  const saveBtn = bar.querySelector("[data-view-save]");
  const defBtn = bar.querySelector("[data-view-default]");
  const delBtn = bar.querySelector("[data-view-delete]");

  const params = new URLSearchParams(location.search || "");
  const currentId = params.get("view_id");
  let views = [];

  try {
   views = await fetchViews(module);
   renderSelect(select, views, currentId);
  } catch (e) {
   notify(` List : ${e.message || e}`, { className: "text-danger" });
  }

  if (auto && shouldAutoApplyDefault()) {
   const def = views.find(v => v.is_default);
   if (def && def.url) {
    if (window.AppDrilldown && typeof window.AppDrilldown.navigate === "function") {
     window.AppDrilldown.navigate(def.url);
    } else {
     location.href = def.url;
    }
    return;
   }
  }

  function selectedView() {
   const val = select ? select.value : "";
   if (!val) return null;
   return views.find(v => String(v.id) === String(val)) || null;
  }

  function syncActions() {
   const chosen = selectedView();
   const isManaged = !!(chosen && (chosen.scope === "team" || chosen.scope === "system"));
   if (delBtn) delBtn.disabled = !chosen || isManaged;
   if (defBtn) defBtn.disabled = !chosen || isManaged;
  }

  if (select) {
   select.addEventListener("change", () => {
    const val = select.value;
    syncActions();
    if (!val) {
     if (window.AppDrilldown && typeof window.AppDrilldown.navigate === "function") {
      window.AppDrilldown.navigate(location.pathname);
     } else {
      location.href = location.pathname;
     }
     return;
    }
    const chosen = views.find(v => String(v.id) === String(val));
    if (chosen && chosen.url && window.AppDrilldown && typeof window.AppDrilldown.navigate === "function") {
     window.AppDrilldown.navigate(chosen.url);
    } else if (chosen && chosen.url) {
     location.href = chosen.url;
    }
   });
  }

  if (saveBtn) {
   saveBtn.addEventListener("click", async () => {
    const raw = await ipmPrompt(" Name enter.", "");
    const name = (raw || "").trim();
    if (!name) return;
    const payload = collectPayload();
    const teamOk = await confirmAction("  Save\n( User . Cancel items Save.)");
    try {
     const res = await api("/api/views", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
       module,
       name,
       scope: teamOk ? "team" : "private",
       payload,
       set_default: false,
      })
     });
     notify(" Save.");
     views = await fetchViews(module);
     renderSelect(select, views, res && res.item ? res.item.id : null);
    } catch (e) {
     notify(` Save failed: ${e.message || e}`, { className: "text-danger" });
    }
   });
  }

  if (defBtn) {
   defBtn.addEventListener("click", async () => {
    const val = select ? select.value : "";
    if (!val) {
     notify("Defaultto  Select.", { className: "text-muted" });
     return;
    }
    const chosen = selectedView();
    if (chosen && chosen.scope === "team") {
     notify(" Defaultto  none.", { className: "text-muted" });
     return;
    }
    if (chosen && chosen.scope === "system") {
     notify(" Defaultto  none. Current items items Save .", { className: "text-muted" });
     return;
    }
    try {
     await api(`/api/views/${encodeURIComponent(val)}/set-default`, { method: "POST" });
     notify("Default .");
     views = await fetchViews(module);
     renderSelect(select, views, val);
    } catch (e) {
     notify(`Default : ${e.message || e}`, { className: "text-danger" });
    }
   });
  }

  if (delBtn) {
   delBtn.addEventListener("click", async () => {
    const val = select ? select.value : "";
    if (!val) {
     notify("Delete Select.", { className: "text-muted" });
     return;
    }
    const chosen = views.find(v => String(v.id) === String(val));
    const label = chosen ? chosen.name : "selected ";
    if (chosen && chosen.scope === "team") {
     notify(" ( Administrator) Delete exists.", { className: "text-muted" });
     return;
    }
    if (chosen && chosen.scope === "system") {
     notify(" cannot be deleted.", { className: "text-muted" });
     return;
    }
    const ok = await confirmAction(`"${label}" Delete ? `);
    if (!ok) return;
    try {
     await api(`/api/views/${encodeURIComponent(val)}`, { method: "DELETE" });
     notify(" Delete.");
     views = await fetchViews(module);
     renderSelect(select, views, "");
     syncActions();
     if (currentId && String(currentId) === String(val)) {
      if (window.AppDrilldown && typeof window.AppDrilldown.navigate === "function") {
       window.AppDrilldown.navigate(location.pathname);
      } else {
       location.href = location.pathname;
      }
     }
    } catch (e) {
     notify(` Delete : ${e.message || e}`, { className: "text-danger" });
    }
   });
  }

  syncActions();
 }

 async function promptSaveCurrent() {
  const module = resolveModule();
  if (!module) {
   notify("Current from Save  none.", { className: "text-muted" });
   return;
  }
  const raw = await ipmPrompt(" Name enter.", "");
  const name = (raw || "").trim();
  if (!name) return;
  const payload = collectPayload();
  const teamOk = await confirmAction("  Save\n( User . Cancel items Save.)");
  try {
   await api("/api/views", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ module, name, scope: teamOk ? "team" : "private", payload, set_default: false })
   });
   notify(" Save.");
  } catch (e) {
   notify(` Save failed: ${e.message || e}`, { className: "text-danger" });
  }
 }

 function boot() {
  $all("[data-view-module]").forEach(el => {
   initViewBar(el);
  });
 }

 window.AppViews = { promptSaveCurrent };

 if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
 else boot();
})();
