(function () {
 function qs(sel, root) {
  return (root || document).querySelector(sel);
 }
 function qsa(sel, root) {
  return Array.from((root || document).querySelectorAll(sel));
 }
 function text(el) {
  try {
   return (el && el.textContent ? el.textContent : "").trim();
  } catch (e) {
   return "";
  }
 }
 function getCsrf() {
  const m = qs('meta[name="csrf-token"]');
  return m ? (m.getAttribute("content") || "") : "";
 }

 function ipmAlert(message, opts) {
  try {
   if (window.AppAlert) return window.AppAlert(message, opts);
  } catch (e) {}
  return Promise.resolve();
 }

 function ipmPrompt(message, defaultValue, opts) {
  try {
   if (window.AppPrompt) return window.AppPrompt(message, defaultValue, opts);
  } catch (e) {}
  return Promise.resolve(null);
 }

 async function patchJson(url, data) {
  const csrf = getCsrf();
  const res = await fetch(url, {
   method: "PATCH",
   headers: {
    "Content-Type": "application/json",
    "X-CSRFToken": csrf,
   },
   body: JSON.stringify(data || {}),
   credentials: "same-origin",
  });
  if (window.AppFetch && typeof window.AppFetch.parseJsonResponse === "function") {
   return window.AppFetch.parseJsonResponse(res);
  }
  let out = null;
  try {
   out = await res.json();
  } catch (e) {
   out = null;
  }
  if (!res.ok) {
   const msg = (out && out.error) ? out.error : (res.status + " " + res.statusText);
   throw new Error(msg);
  }
  return out;
 }

 function selectedIds(table, kind) {
  return qsa(`input[type="checkbox"][data-qe="${kind}"]:checked`, table)
   .map((x) => x.value)
   .filter((v) => v && /^[a-zA-Z0-9-]+$/.test(v));
 }

 function bindBulk(table) {
  const wfBtn = qs('[data-qe-bulk="wf"]', table);
  const dkBtn = qs('[data-qe-bulk="dk"]', table);

  async function doBulk(kind) {
   const ids = selectedIds(table, kind);
   if (!ids.length) {
    await ipmAlert("Select item none.");
    return;
   }
   const field = await ipmPrompt("change target people Input (: status, priority, assignee_id, due_date)", "", { title: "Bulk change" });
   if (!field) return;
   const statusHint = "Status value: Pending, In Progress, Completed, Task Abandoned";
   const promptMessage = String(field).trim().toLowerCase() === "status"
    ? `"status" value Input\n${statusHint}`
    : `"${field}" value Input`;
   const value = await ipmPrompt(promptMessage, "", { title: "Bulk change" });
   if (value === null) return;

   const patch = {};
   patch[field] = value;

   const url = kind === "wf" ? "/case/api/workflows/bulk" : "/case/api/dockets/bulk";
   try {
    await patchJson(url, { ids: ids, patch: patch });
    location.reload();
   } catch (e) {
    await ipmAlert("Bulk change : " + (e && e.message ? e.message : e), { title: "Error" });
   }
  }

  if (wfBtn) wfBtn.addEventListener("click", () => doBulk("wf"));
  if (dkBtn) dkBtn.addEventListener("click", () => doBulk("dk"));
 }

 function bindInlineEdits(table) {
  qsa("[data-qe-edit]", table).forEach((cell) => {
   cell.style.cursor = "pointer";
   cell.title = " Change";
   cell.addEventListener("click", async () => {
    const kind = cell.getAttribute("data-qe-kind"); // wf|dk
    const id = cell.getAttribute("data-qe-id");
    const field = cell.getAttribute("data-qe-field");
    if (!kind || !id || !field) return;
    const cur = text(cell);
    const statusHint = "Status value: Pending, In Progress, Completed, Task Abandoned";
    const promptMessage = String(field).trim().toLowerCase() === "status"
     ? `${field} Change\n${statusHint}`
     : `${field} Change`;
    const next = await ipmPrompt(promptMessage, cur, { title: "Change" });
    if (next === null) return;

    const url = kind === "wf" ? "/case/api/workflows/bulk" : "/case/api/dockets/bulk";
    const patch = {};
    patch[field] = next;
    try {
     await patchJson(url, { ids: [id], patch: patch });
     location.reload();
    } catch (e) {
     await ipmAlert("Change : " + (e && e.message ? e.message : e), { title: "Error" });
    }
   });
  });
 }

 function boot() {
  qsa("[data-qe-table]", document).forEach((tbl) => {
   bindBulk(tbl);
   bindInlineEdits(tbl);
  });
 }

 if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
 } else {
  boot();
 }
})();
