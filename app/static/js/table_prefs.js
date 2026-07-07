(function () {
 function $(sel, root) { return (root || document).querySelector(sel); }
 function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

 function csrfToken() {
  const m = document.querySelector('meta[name="csrf-token"]');
  return m ? (m.getAttribute("content") || "") : "";
 }

 async function fetchJson(url, opts) {
  const headers = Object.assign({ "Accept": "application/json" }, (opts && opts.headers) || {});
  const tok = csrfToken();
  if (tok) headers["X-CSRFToken"] = tok;
  const res = await fetch(url, Object.assign({ credentials: "same-origin" }, opts || {}, { headers }));
  if (window.AppFetch && typeof window.AppFetch.parseJsonResponse === "function") {
   return window.AppFetch.parseJsonResponse(res);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
   const msg = (data && (data.error || data.message)) || (`HTTP ${res.status}`);
   throw new Error(msg);
  }
  return data;
 }

 function readTableColumns(table) {
  const ths = $all("thead th[data-col]", table);
  return ths.map((th, idx) => ({
   key: th.getAttribute("data-col") || String(idx),
   label: (th.textContent || "").trim() || (th.getAttribute("data-col") || `col${idx}`),
   defaultWidth: th.style.width || "",
  }));
 }

 function collectRowCellsByKey(row) {
  const map = {};
  Array.from(row.children || []).forEach((cell) => {
   const k = cell.getAttribute("data-col");
   if (k) map[k] = cell;
  });
  return map;
 }

 function applyPrefs(table, prefs) {
  if (!table) return;
  const headerRow = $("thead tr", table);
  const bodyRows = $all("tbody tr", table);
  const cols = readTableColumns(table);

  const order = (prefs && Array.isArray(prefs.order)) ? prefs.order.slice() : cols.map(c => c.key);
  const hidden = (prefs && prefs.hidden) ? prefs.hidden : {};
  const widths = (prefs && prefs.widths) ? prefs.widths : {};

  // Reorder header
  if (headerRow) {
   const byKey = collectRowCellsByKey(headerRow);
   const next = [];
   order.forEach((k) => { if (byKey[k]) next.push(byKey[k]); });
   // append unknown columns at end
   cols.forEach((c) => { if (!order.includes(c.key) && byKey[c.key]) next.push(byKey[c.key]); });
   next.forEach((el) => headerRow.appendChild(el));
  }

  // Reorder body rows
  bodyRows.forEach((r) => {
   const byKey = collectRowCellsByKey(r);
   const next = [];
   order.forEach((k) => { if (byKey[k]) next.push(byKey[k]); });
   cols.forEach((c) => { if (!order.includes(c.key) && byKey[c.key]) next.push(byKey[c.key]); });
   next.forEach((el) => r.appendChild(el));
  });

  // Hide/show
  const allCells = $all("[data-col]", table);
  allCells.forEach((cell) => {
   const k = cell.getAttribute("data-col");
   const isHidden = !!(hidden && hidden[k]);
   cell.style.display = isHidden ? "none" : "";
  });

  // Widths (header only)
  const ths = $all("thead th[data-col]", table);
  ths.forEach((th) => {
   const k = th.getAttribute("data-col");
   const w = widths && widths[k];
   th.style.width = w ? String(w) : (th.dataset.defaultWidth || th.style.width || "");
  });
 }

 function buildModalHtml(cols, prefs) {
  const order = (prefs && Array.isArray(prefs.order)) ? prefs.order : cols.map(c => c.key);
  const hidden = (prefs && prefs.hidden) ? prefs.hidden : {};
  const widths = (prefs && prefs.widths) ? prefs.widths : {};

  function isHidden(k) { return !!hidden[k]; }
  function widthVal(k) { return widths[k] ? String(widths[k]) : ""; }

  const orderedCols = [];
  order.forEach((k) => {
   const c = cols.find(x => x.key === k);
   if (c) orderedCols.push(c);
  });
  cols.forEach((c) => { if (!order.includes(c.key)) orderedCols.push(c); });

  return `
   <div class="modal fade" id="tablePrefModal" tabindex="-1" aria-hidden="true">
    <div class="modal-dialog modal-lg">
     <div class="modal-content">
      <div class="modal-header">
       <h5 class="modal-title"> User</h5>
       <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
       <div class="small text-muted mb-2"> =Hidden · ↑↓=Order · (px/%)</div>
       <div class="table-responsive">
        <table class="table table-sm align-middle">
         <thead class="table-light">
          <tr><th style="width:50px">Display</th><th></th><th style="width:160px"></th><th style="width:120px">Order</th></tr>
         </thead>
         <tbody>
          ${orderedCols.map((c) => `
           <tr data-key="${c.key}">
            <td><input class="form-check-input col-visible" type="checkbox" ${isHidden(c.key) ? "" : "checked"}></td>
            <td class="small">${c.label}</td>
            <td><input class="form-control form-control-sm col-width" placeholder=": 120px 12%" value="${widthVal(c.key)}"></td>
            <td>
             <div class="btn-group btn-group-sm" role="group">
              <button type="button" class="btn btn-outline-secondary col-up">↑</button>
              <button type="button" class="btn btn-outline-secondary col-down">↓</button>
             </div>
            </td>
           </tr>
          `).join("")}
         </tbody>
        </table>
       </div>
      </div>
      <div class="modal-footer">
       <button type="button" class="btn btn-outline-secondary" id="tablePrefReset">Reset</button>
       <button type="button" class="btn btn-primary" id="tablePrefSave">Save</button>
      </div>
     </div>
    </div>
   </div>
  `;
 }

 async function init(opts) {
  const table = document.getElementById(opts.tableId);
  const openBtn = document.getElementById(opts.openBtnId);
  if (!table || !openBtn) return;
  const cols = readTableColumns(table);
  const storageKey = `app.tablepref.${opts.prefKey || opts.tableId}.v1`;

  let prefs = null;
  // Load prefs (remote -> local)
  try {
   if (opts.remoteUrl) {
    const data = await fetchJson(`${opts.remoteUrl}?key=${encodeURIComponent(storageKey)}`);
    prefs = (data && data.value) ? data.value : null;
   }
  } catch (e) {}
  if (!prefs) {
   try { prefs = JSON.parse(localStorage.getItem(storageKey) || "null"); } catch (e) { prefs = null; }
  }
  if (!prefs) prefs = { order: cols.map(c => c.key), hidden: {}, widths: {} };

  // Keep defaults for widths
  $all("thead th[data-col]", table).forEach((th) => {
   if (!th.dataset.defaultWidth) th.dataset.defaultWidth = th.style.width || "";
  });

  applyPrefs(table, prefs);

  function persist(next) {
   prefs = next;
   applyPrefs(table, prefs);
   try { localStorage.setItem(storageKey, JSON.stringify(prefs)); } catch (e) {}
   if (opts.remoteUrl) {
    fetchJson(`${opts.remoteUrl}?key=${encodeURIComponent(storageKey)}`, {
     method: "POST",
     headers: { "Content-Type": "application/json" },
     body: JSON.stringify({ value: prefs }),
    }).catch(() => {});
   }
  }

  function openModal() {
   // Remove any existing modal
   document.getElementById("tablePrefModal")?.remove();
   const wrap = document.createElement("div");
   wrap.innerHTML = buildModalHtml(cols, prefs);
   document.body.appendChild(wrap);

   const modalEl = document.getElementById("tablePrefModal");
   const modal = window.bootstrap ? new bootstrap.Modal(modalEl) : null;
   if (!modal) {
    try { if (window.AppAlert) window.AppAlert("Bootstrap JS required."); } catch (e) {}
    return;
   }

   function moveRow(tr, dir) {
    const sib = dir < 0 ? tr.previousElementSibling : tr.nextElementSibling;
    if (!sib) return;
    if (dir < 0) tr.parentElement.insertBefore(tr, sib);
    else tr.parentElement.insertBefore(sib, tr);
   }

   modalEl.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-key]");
    if (!tr) return;
    if (e.target.classList.contains("col-up")) moveRow(tr, -1);
    if (e.target.classList.contains("col-down")) moveRow(tr, +1);
   });

   $("#tablePrefReset").addEventListener("click", () => {
    persist({ order: cols.map(c => c.key), hidden: {}, widths: {} });
    modal.hide();
   });

   $("#tablePrefSave").addEventListener("click", () => {
    const rows = $all("tbody tr[data-key]", modalEl);
    const nextOrder = rows.map(r => r.getAttribute("data-key"));
    const nextHidden = {};
    const nextWidths = {};
    rows.forEach((r) => {
     const k = r.getAttribute("data-key");
     const visible = r.querySelector(".col-visible")?.checked;
     if (!visible) nextHidden[k] = true;
     const w = (r.querySelector(".col-width")?.value || "").trim();
     if (w) nextWidths[k] = w;
    });
    persist({ order: nextOrder, hidden: nextHidden, widths: nextWidths });
    modal.hide();
   });

   modal.show();
  }

  openBtn.addEventListener("click", openModal);
 }

 window.TablePrefs = { init };
})();
