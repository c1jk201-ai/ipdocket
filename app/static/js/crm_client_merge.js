(function () {
 const root = document.getElementById("mergePage");
 if (!root) return;

 const alertEl = document.getElementById("mergeAlert");
 const hiddenFields = document.getElementById("mergeHiddenFields");
 const targetPreview = document.getElementById("targetPreview");
 const sourcesPreview = document.getElementById("sourcesPreview");
 const mergeSubmitBtn = document.getElementById("mergeSubmitBtn");
 const targetLabel = document.getElementById("targetLabel");
 const sourcesCountLabel = document.getElementById("sourcesCountLabel");
 const filterEl = document.getElementById("clientFilter");
 const filterCountEl = document.getElementById("clientFilterCount");
 const clearGroupFilterBtn = document.getElementById("clearGroupFilter");
 const table = document.getElementById("clientTable");
 const form = document.getElementById("mergeForm");

 if (!table || !hiddenFields || !targetPreview || !sourcesPreview || !mergeSubmitBtn) return;

 const unifiedInvoice = root.dataset.unifiedInvoice === "1";

 const parseIntOrNull = (value) => {
  const n = Number.parseInt(String(value || ""), 10);
  return Number.isFinite(n) ? n : null;
 };

 const escapeHtml = (value) =>
  String(value || "")
   .replaceAll("&", "&amp;")
   .replaceAll("<", "&lt;")
   .replaceAll(">", "&gt;")
   .replaceAll('"', "&quot;")
   .replaceAll("'", "&#39;");

 const showAlert = (message) => {
  if (!alertEl) return;
  alertEl.textContent = message || "";
  alertEl.classList.toggle("d-none", !message);
  if (!message) return;
  window.clearTimeout(showAlert._timer);
  showAlert._timer = window.setTimeout(() => {
   alertEl.classList.add("d-none");
  }, 3200);
 };

 const digitsOnly = (value) => String(value || "").replace(/\D+/g, "");
 const lower = (value) => String(value || "").trim().toLowerCase();
 const parseIdList = (raw) => {
  let ids = [];
  try {
   ids = JSON.parse(raw || "[]");
   if (!Array.isArray(ids)) ids = [];
  } catch (e) {
   ids = [];
  }
  return ids.map((v) => parseIntOrNull(v)).filter((v) => v);
 };

 let preselectedTargetId = parseIntOrNull(root.dataset.preselectedTarget);
 let preselectedSources = [];
 try {
  preselectedSources = JSON.parse(root.dataset.preselectedSources || "[]");
  if (!Array.isArray(preselectedSources)) preselectedSources = [];
 } catch (e) {
  preselectedSources = [];
 }
 preselectedSources = preselectedSources.map((v) => parseIntOrNull(v)).filter((v) => v);

 const rows = Array.from(table.querySelectorAll("tbody tr[data-client-id]"));
 const clients = new Map();
 const rowById = new Map();

 rows.forEach((row) => {
  const id = parseIntOrNull(row.dataset.clientId);
  if (!id) return;
  const client = {
   id,
   name: row.dataset.clientName || "",
   type: row.dataset.clientType || "",
   email: row.dataset.clientEmail || "",
   phone: row.dataset.clientPhone || "",
   registration: row.dataset.clientRegistration || "",
   bizReg: row.dataset.clientBizreg || "",
   address: row.dataset.clientAddress || "",
   caseCount: parseIntOrNull(row.dataset.caseCount) || 0,
   contactCount: parseIntOrNull(row.dataset.contactCount) || 0,
   opportunityCount: parseIntOrNull(row.dataset.opportunityCount) || 0,
   activityCount: parseIntOrNull(row.dataset.activityCount) || 0,
   externalInvoiceClientId: parseIntOrNull(row.dataset.externalInvoiceClientId),
   invoiceLabel: row.dataset.invoiceLabel || "",
   viewUrl: row.dataset.viewUrl || "",
  };
  client.searchText = lower(
   [
    "#" + client.id,
    String(client.id),
    client.name,
    client.type,
    client.email,
    client.phone,
    client.registration,
    client.bizReg,
    client.address,
   ].join(" ")
  );
  client.searchDigits = digitsOnly(
   [client.phone, client.registration, client.bizReg, String(client.id)].join(" ")
  );
  clients.set(id, client);
  rowById.set(id, row);
 });

 const scoreTarget = (client) => {
  if (!client) return 0;
  let score = 0;
  if (!unifiedInvoice && client.externalInvoiceClientId) score += 1_000_000;
  score += (client.caseCount || 0) * 10_000;
  score += (client.opportunityCount || 0) * 1_000;
  score += (client.contactCount || 0) * 100;
  if (client.email) score += 500;
  if (client.phone) score += 300;
  if (client.registration) score += 200;
  if (client.bizReg) score += 200;
  if (client.address) score += 50;
  score += client.activityCount || 0;
  score += client.id / 10_000;
  return score;
 };

 const pickBestTarget = (ids) => {
  const candidates = ids.map((id) => clients.get(id)).filter(Boolean);
  if (!candidates.length) return null;
  candidates.sort((a, b) => scoreTarget(b) - scoreTarget(a));
  return candidates[0]?.id || null;
 };

 let targetId = preselectedTargetId && clients.has(preselectedTargetId) ? preselectedTargetId : null;
 const sourceIds = new Set();
 let validSources = Array.from(new Set(preselectedSources)).filter((id) => clients.has(id));
 if (!targetId && validSources.length>= 2) {
  const best = pickBestTarget(validSources);
  if (best) {
   targetId = best;
   validSources = validSources.filter((id) => id !== best);
  }
 }
 validSources.forEach((id) => {
  if (targetId && id === targetId) return;
  sourceIds.add(id);
 });
 let activeGroupIds = null;
 const initialGroupIds =
  targetId && sourceIds.size ? [targetId, ...Array.from(sourceIds)] : [];
 const shouldInitGroupFilter = initialGroupIds.length>= 2;

 const buildHiddenInputs = () => {
  hiddenFields.innerHTML = "";

  if (targetId) {
   const input = document.createElement("input");
   input.type = "hidden";
   input.name = "target_client";
   input.value = String(targetId);
   hiddenFields.appendChild(input);
  }

  Array.from(sourceIds.values())
   .sort((a, b) => a - b)
   .forEach((sid) => {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "source_clients";
    input.value = String(sid);
    hiddenFields.appendChild(input);
   });
 };

 const renderClientSummary = (client) => {
  const wrap = document.createElement("div");

  const title = document.createElement("div");
  title.className = "d-flex justify-content-between align-items-start gap-2";

  const name = document.createElement("div");
  name.className = "fw-semibold";
  name.textContent = `#${client.id} ${client.name || ""}`.trim();
  title.appendChild(name);

  if (client.viewUrl) {
   const link = document.createElement("a");
   link.href = client.viewUrl;
   link.target = "_blank";
   link.rel = "noopener";
   link.className = "btn btn-sm btn-outline-secondary";
   link.textContent = "View";
   title.appendChild(link);
  }

  wrap.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "small text-muted mt-2";
  const parts = [];
  if (client.type) parts.push(client.type.toUpperCase());
  if (client.invoiceLabel) parts.push(`Invoice: ${client.invoiceLabel}`);
  meta.textContent = parts.join(" · ") || " ";
  wrap.appendChild(meta);

  const info = document.createElement("div");
  info.className = "small mt-2";
  const line1 = [
   client.email ? `Email: ${client.email}` : null,
   client.phone ? `Phone: ${client.phone}` : null,
  ].filter(Boolean);
  const line2 = [
   client.registration ? `Registration No.: ${client.registration}` : null,
   client.bizReg ? `Tax ID / EIN: ${client.bizReg}` : null,
  ].filter(Boolean);
  const line3 = client.address ? [`Address: ${client.address}`] : [];
  const textLines = [...line1, ...line2, ...line3].filter(Boolean);
  info.textContent = textLines.length ? textLines.join(" · ") : "-";
  wrap.appendChild(info);

  const stats = document.createElement("div");
  stats.className = "d-flex flex-wrap gap-1 mt-2";
  const mkBadge = (label, value) => {
   const b = document.createElement("span");
   b.className = "badge bg-light text-secondary border";
   b.textContent = `${label} ${value}`;
   return b;
  };
  stats.appendChild(mkBadge("Matter", client.caseCount || 0));
  stats.appendChild(mkBadge("Phone", client.contactCount || 0));
  stats.appendChild(mkBadge("times", client.opportunityCount || 0));
  stats.appendChild(mkBadge("", client.activityCount || 0));
  wrap.appendChild(stats);

  return wrap;
 };

 const renderSelection = () => {
  buildHiddenInputs();

  const ok = Boolean(targetId) && sourceIds.size> 0;
  mergeSubmitBtn.disabled = !ok;

  const target = targetId ? clients.get(targetId) : null;
  if (targetLabel) targetLabel.textContent = target ? `#${target.id} ${target.name || ""}`.trim() : "Select";
  if (sourcesCountLabel) sourcesCountLabel.textContent = `${sourceIds.size}people`;

  targetPreview.innerHTML = "";
  if (!target) {
   const p = document.createElement("div");
   p.className = "text-muted small";
   p.textContent = "below tablefrom target  Select.";
   targetPreview.appendChild(p);
  } else {
   targetPreview.appendChild(renderClientSummary(target));
  }

  sourcesPreview.innerHTML = "";
  if (sourceIds.size === 0) {
   const p = document.createElement("div");
   p.className = "text-muted small";
   p.textContent = "below tablefrom Original  Add.";
   sourcesPreview.appendChild(p);
  } else {
   const list = document.createElement("div");
   list.className = "list-group";
   Array.from(sourceIds.values())
    .map((id) => clients.get(id))
    .filter(Boolean)
    .sort((a, b) => scoreTarget(b) - scoreTarget(a))
    .forEach((c) => {
     const item = document.createElement("div");
     item.className = "list-group-item d-flex justify-content-between align-items-start gap-2";

     const left = document.createElement("div");
     left.className = "flex-grow-1";

     const t = document.createElement("div");
     t.className = "fw-semibold";
     t.textContent = `#${c.id} ${c.name || ""}`.trim();
     left.appendChild(t);

     const m = document.createElement("div");
     m.className = "small text-muted";
     const bits = [];
     if (c.invoiceLabel) bits.push(`Invoice: ${c.invoiceLabel}`);
     bits.push(`Matter ${c.caseCount || 0}`);
     m.textContent = bits.join(" · ");
     left.appendChild(m);

     item.appendChild(left);

     const btn = document.createElement("button");
     btn.type = "button";
     btn.className = "btn btn-sm btn-outline-danger";
     btn.textContent = "";
     btn.dataset.action = "remove-source";
     btn.dataset.clientId = String(c.id);
     item.appendChild(btn);

     list.appendChild(item);
    });
   sourcesPreview.appendChild(list);
  }

  rowById.forEach((row, id) => {
   row.classList.toggle("table-primary", targetId === id);
   row.classList.toggle("table-danger", sourceIds.has(id));
   const targetBtn = row.querySelector('button[data-action="set-target"]');
   const sourceBtn = row.querySelector('button[data-action="toggle-source"]');
   if (targetBtn) {
    const isTarget = targetId === id;
    targetBtn.classList.toggle("btn-primary", isTarget);
    targetBtn.classList.toggle("btn-outline-primary", !isTarget);
    targetBtn.textContent = isTarget ? "target✓" : "target";
   }
   if (sourceBtn) {
    const isTarget = targetId === id;
    const isSource = sourceIds.has(id);
    sourceBtn.disabled = isTarget;
    sourceBtn.classList.toggle("btn-danger", isSource);
    sourceBtn.classList.toggle("btn-outline-danger", !isSource);
    sourceBtn.textContent = isTarget ? "Original" : isSource ? "Original✓" : "Original";
   }
  });

  if (ok && target) {
   const sourceNames = Array.from(sourceIds.values())
    .map((id) => clients.get(id))
    .filter(Boolean)
    .map((c) => `#${c.id} ${escapeHtml(c.name || "")}`.trim())
    .join(", ");
   const msg = [
    `target(): <b>#${target.id}</b> ${escapeHtml(target.name || "")}`.trim(),
    `Original(Delete): ${sourceNames || "-"}`,
    "",
    "Source client  Delete. Open ? ",
   ].join("<br>");
   mergeSubmitBtn.dataset.confirm = msg;
  } else {
   mergeSubmitBtn.dataset.confirm = " Client ? Actions  none.";
  }
 };

 const setTarget = (id) => {
  if (!clients.has(id)) return;
  targetId = id;
  sourceIds.delete(id);
  renderSelection();
 };

 const toggleSource = (id) => {
  if (!clients.has(id)) return;
  if (targetId && id === targetId) {
   showAlert("target Original Clientdays none.");
   return;
  }
  if (sourceIds.has(id)) sourceIds.delete(id);
  else sourceIds.add(id);
  renderSelection();
 };

 const clearSelection = () => {
  targetId = null;
  sourceIds.clear();
  clearGroupFilter();
  renderSelection();
 };

 const applyFilter = () => {
  const q = lower(filterEl?.value || "");
  const qDigits = digitsOnly(q);
  const groupActive = activeGroupIds && activeGroupIds.size;
  const baseTotal = groupActive ? activeGroupIds.size : rows.length;
  let visible = 0;

  rows.forEach((row) => {
   const id = parseIntOrNull(row.dataset.clientId);
   const client = id ? clients.get(id) : null;
   if (!client) return;

   let match = true;
   if (q) {
    match = client.searchText.includes(q);
    if (!match && qDigits) match = client.searchDigits.includes(qDigits);
   }
   if (match && groupActive && !activeGroupIds.has(id)) {
    match = false;
   }

   row.dataset.rowHidden = match ? "0" : "1";
   if (match) visible += 1;
  });

  if (filterCountEl) {
   if (q || groupActive) {
    filterCountEl.textContent = `Display ${visible} / ${baseTotal}people`;
   } else {
    filterCountEl.textContent = `Total ${rows.length}people`;
   }
  }
 };

 const setGroupFilter = (ids) => {
  if (!Array.isArray(ids) || ids.length < 2) return;
  activeGroupIds = new Set(ids);
  if (clearGroupFilterBtn) clearGroupFilterBtn.classList.remove("d-none");
  applyFilter();
 };

 const clearGroupFilter = () => {
  activeGroupIds = null;
  if (clearGroupFilterBtn) clearGroupFilterBtn.classList.add("d-none");
  applyFilter();
 };

 const updateGroupRecommendations = () => {
  document.querySelectorAll('button[data-action="load-group"]').forEach((btn) => {
   const recEl = btn.closest("li")?.querySelector('[data-role="group-recommend"]');
   if (!recEl) return;
   const ids = parseIdList(btn.dataset.ids || "[]").filter((id) => clients.has(id));
   if (ids.length < 2) {
    recEl.textContent = "";
    return;
   }
   const best = pickBestTarget(ids);
   if (!best) {
    recEl.textContent = "";
    return;
   }
   const client = clients.get(best);
   recEl.textContent = client
    ? ` target: #${client.id} ${client.name || ""}`.trim()
    : ` target: #${best}`;
  });
 };

 document.addEventListener("click", (e) => {
  const btn = e.target.closest?.("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  const id = parseIntOrNull(btn.dataset.clientId);

  if (action === "set-target" && id) {
   setTarget(id);
   return;
  }
  if (action === "toggle-source" && id) {
   toggleSource(id);
   return;
  }
  if (action === "remove-source" && id) {
   sourceIds.delete(id);
   renderSelection();
   return;
  }
  if (action === "clear-selection") {
   clearSelection();
   return;
  }
  if (action === "clear-target") {
   targetId = null;
   renderSelection();
   return;
  }
  if (action === "clear-sources") {
   sourceIds.clear();
   renderSelection();
   return;
  }
  if (action === "load-group") {
   let ids = parseIdList(btn.dataset.ids || "[]").filter((v) => clients.has(v));
   ids = Array.from(new Set(ids));
   if (ids.length < 2) {
    showAlert("from Select Client does not.");
    return;
   }

   const best = pickBestTarget(ids);
   if (!best) return;
   targetId = best;
   sourceIds.clear();
   ids.forEach((cid) => {
    if (cid !== best) sourceIds.add(cid);
   });
   renderSelection();
   if (filterEl) filterEl.value = "";
   setGroupFilter(ids);
   targetPreview?.scrollIntoView?.({ behavior: "smooth", block: "start" });
   return;
  }
 });

 if (filterEl) {
  filterEl.addEventListener("input", applyFilter);
  filterEl.addEventListener("keydown", (e) => {
   if (e.key === "Escape") {
    filterEl.value = "";
    applyFilter();
   }
  });
 }
 if (clearGroupFilterBtn) {
  clearGroupFilterBtn.addEventListener("click", () => {
   clearGroupFilter();
  });
 }

 form?.addEventListener("submit", () => {
  buildHiddenInputs();
 });

 updateGroupRecommendations();
 applyFilter();
 renderSelection();
 if (shouldInitGroupFilter) {
  setGroupFilter(initialGroupIds);
 }
})();
