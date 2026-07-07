(function () {
 "use strict";

 function getCsrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
 }

 function injectCsrfIfMissing(form) {
  const token = getCsrfToken();
  if (!token) return;

  const method = (form.getAttribute("method") || "").toUpperCase();
  if (method !== "POST") return;

  if (!form.querySelector('input[name="csrf_token"]')) {
   const hidden = document.createElement("input");
   hidden.type = "hidden";
   hidden.name = "csrf_token";
   hidden.value = token;
   form.appendChild(hidden);
  }
 }

 (function patchProgrammaticSubmit() {
  const nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
   try {
    injectCsrfIfMissing(this);
   } catch (e) {}
   return nativeSubmit.call(this);
  };

  if (HTMLFormElement.prototype.requestSubmit) {
   const nativeRequestSubmit = HTMLFormElement.prototype.requestSubmit;
   HTMLFormElement.prototype.requestSubmit = function (submitter) {
    try {
     injectCsrfIfMissing(this);
    } catch (e) {}
    return nativeRequestSubmit.call(this, submitter);
   };
  }
 })();

 function getFocusableElements(root) {
  if (!root) return [];
  const selectors = [
   "a[href]",
   "button:not([disabled])",
   "input:not([disabled])",
   "select:not([disabled])",
   "textarea:not([disabled])",
   "[tabindex]:not([tabindex='-1'])",
  ].join(",");
  return Array.from(root.querySelectorAll(selectors)).filter((el) => {
   if (!(el instanceof HTMLElement)) return false;
   if (el.hidden) return false;
   if (el.getAttribute("aria-hidden") === "true") return false;
   return true;
  });
 }

 function trapFocusWithin(root, ev) {
  if (ev.key !== "Tab") return;
  const focusable = getFocusableElements(root);
  if (!focusable.length) {
   ev.preventDefault();
   root.focus();
   return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (ev.shiftKey && document.activeElement === first) {
   ev.preventDefault();
   last.focus();
   return;
  }
  if (!ev.shiftKey && document.activeElement === last) {
   ev.preventDefault();
   first.focus();
  }
 }

 function restorePreviousFocus(previouslyFocused) {
  try {
   if (previouslyFocused && document.contains(previouslyFocused)) {
    previouslyFocused.focus();
   }
  } catch (e) {}
 }

 function removeExistingDialog(id) {
  const existing = document.getElementById(id);
  if (!existing) return;
  try {
   if (existing._ipmOnKeyDown) {
    document.removeEventListener("keydown", existing._ipmOnKeyDown, true);
   }
  } catch (e) {}
  existing.remove();
 }

 function buildOverlay(id, titleId, messageId) {
  removeExistingDialog(id);
  const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const overlay = document.createElement("div");
  overlay.id = id;
  overlay.className = "app-dialog-overlay";
  overlay.setAttribute("role", "presentation");

  const panel = document.createElement("div");
  panel.className = "app-dialog-panel";
  panel.tabIndex = -1;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", titleId);
  panel.setAttribute("aria-describedby", messageId);

  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  return { overlay, panel, previouslyFocused };
 }

 function openConfirmModal(message) {
  return new Promise((resolve) => {
   try {
    const { overlay, panel, previouslyFocused } = buildOverlay(
     "ipmConfirmOverlay",
     "ipmConfirmTitle",
     "ipmConfirmMessage",
    );
    panel.innerHTML = `
     <h3 id="ipmConfirmTitle" class="app-dialog-title">Confirm</h3>
     <p id="ipmConfirmMessage" class="app-dialog-message"></p>
     <div class="app-dialog-actions">
      <button type="button" id="ipmConfirmCancelBtn" class="btn btn-sm btn-outline-secondary">Cancel</button>
      <button type="button" id="ipmConfirmOkBtn" class="btn btn-sm btn-danger">Confirm</button>
     </div>
    `;
    panel.querySelector("#ipmConfirmMessage").textContent = message || "";

    const cleanup = (result) => {
     try {
      overlay.remove();
     } catch (e) {}
     try {
      document.removeEventListener("keydown", onKeyDown, true);
     } catch (e) {}
     restorePreviousFocus(previouslyFocused);
     resolve(result);
    };
    const onKeyDown = (ev) => {
     if (ev.key === "Escape") {
      ev.preventDefault();
      cleanup(false);
      return;
     }
     trapFocusWithin(panel, ev);
    };

    const cancelBtn = document.getElementById("ipmConfirmCancelBtn");
    const okBtn = document.getElementById("ipmConfirmOkBtn");
    cancelBtn.addEventListener("click", (ev) => {
     ev.preventDefault();
     cleanup(false);
    });
    okBtn.addEventListener("click", (ev) => {
     ev.preventDefault();
     cleanup(true);
    });
    overlay.addEventListener("click", (ev) => {
     if (ev.target === overlay) cleanup(false);
    });
    overlay._ipmOnKeyDown = onKeyDown;
    document.addEventListener("keydown", onKeyDown, true);
    try {
     cancelBtn.focus();
    } catch (e) {
     panel.focus();
    }
   } catch (e) {
    resolve(false);
   }
  });
 }

 function openAlertModal(message, opts) {
  return new Promise((resolve) => {
   try {
    const { overlay, panel, previouslyFocused } = buildOverlay(
     "ipmAlertOverlay",
     "ipmAlertTitle",
     "ipmAlertMessage",
    );
    panel.innerHTML = `
     <h3 id="ipmAlertTitle" class="app-dialog-title"></h3>
     <p id="ipmAlertMessage" class="app-dialog-message"></p>
     <div class="app-dialog-actions">
      <button type="button" id="ipmAlertOkBtn" class="btn btn-sm btn-primary"></button>
     </div>
    `;

    const title = opts && opts.title ? String(opts.title) : "Notice";
    const okText = opts && opts.okText ? String(opts.okText) : "Confirm";
    panel.querySelector("#ipmAlertTitle").textContent = title;
    panel.querySelector("#ipmAlertMessage").textContent = message || "";
    panel.querySelector("#ipmAlertOkBtn").textContent = okText;

    const cleanup = () => {
     try {
      overlay.remove();
     } catch (e) {}
     try {
      document.removeEventListener("keydown", onKeyDown, true);
     } catch (e) {}
     restorePreviousFocus(previouslyFocused);
     resolve();
    };
    const onKeyDown = (ev) => {
     if (ev.key === "Escape") {
      ev.preventDefault();
      cleanup();
      return;
     }
     trapFocusWithin(panel, ev);
    };

    overlay.addEventListener("click", (ev) => {
     if (ev.target === overlay) cleanup();
    });
    const okBtn = document.getElementById("ipmAlertOkBtn");
    okBtn.addEventListener("click", (ev) => {
     ev.preventDefault();
     cleanup();
    });
    overlay._ipmOnKeyDown = onKeyDown;
    document.addEventListener("keydown", onKeyDown, true);
    try {
     okBtn.focus();
    } catch (e) {
     panel.focus();
    }
   } catch (e) {
    resolve();
   }
  });
 }

 function openPromptModal(message, defaultValue, opts) {
  return new Promise((resolve) => {
   try {
    const { overlay, panel, previouslyFocused } = buildOverlay(
     "ipmPromptOverlay",
     "ipmPromptTitle",
     "ipmPromptMessage",
    );
    panel.innerHTML = `
     <h3 id="ipmPromptTitle" class="app-dialog-title"></h3>
     <p id="ipmPromptMessage" class="app-dialog-message"></p>
     <div class="app-dialog-input-wrap">
      <input id="ipmPromptInput" class="form-control form-control-sm" />
     </div>
     <div class="app-dialog-actions">
      <button type="button" id="ipmPromptCancelBtn" class="btn btn-sm btn-outline-secondary"></button>
      <button type="button" id="ipmPromptOkBtn" class="btn btn-sm btn-primary"></button>
     </div>
    `;

    const title = opts && opts.title ? String(opts.title) : "Input";
    const okText = opts && opts.okText ? String(opts.okText) : "Confirm";
    const cancelText = opts && opts.cancelText ? String(opts.cancelText) : "Cancel";
    const inputType = opts && opts.type ? String(opts.type) : "text";
    const placeholder = opts && opts.placeholder ? String(opts.placeholder) : "";

    panel.querySelector("#ipmPromptTitle").textContent = title;
    panel.querySelector("#ipmPromptMessage").textContent = message || "";
    panel.querySelector("#ipmPromptCancelBtn").textContent = cancelText;
    panel.querySelector("#ipmPromptOkBtn").textContent = okText;

    const input = panel.querySelector("#ipmPromptInput");
    if (input) {
     try {
      input.type = inputType;
     } catch (e) {
      input.type = "text";
     }
     input.placeholder = placeholder;
     input.value = defaultValue === null || defaultValue === undefined ? "" : String(defaultValue);
    }

    const cleanup = (result) => {
     try {
      overlay.remove();
     } catch (e) {}
     try {
      document.removeEventListener("keydown", onKeyDown, true);
     } catch (e) {}
     restorePreviousFocus(previouslyFocused);
     resolve(result);
    };
    const ok = () => cleanup(input ? input.value || "" : "");
    const cancel = () => cleanup(null);
    const onKeyDown = (ev) => {
     if (ev.key === "Escape") {
      ev.preventDefault();
      cancel();
      return;
     }
     trapFocusWithin(panel, ev);
    };

    overlay._ipmOnKeyDown = onKeyDown;
    document.addEventListener("keydown", onKeyDown, true);
    overlay.addEventListener("click", (ev) => {
     if (ev.target === overlay) cancel();
    });
    panel.querySelector("#ipmPromptCancelBtn").addEventListener("click", (ev) => {
     ev.preventDefault();
     cancel();
    });
    panel.querySelector("#ipmPromptOkBtn").addEventListener("click", (ev) => {
     ev.preventDefault();
     ok();
    });
    if (input) {
     input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
       ev.preventDefault();
       ok();
      }
     });
     try {
      input.focus();
      input.select();
     } catch (e) {
      panel.focus();
     }
    } else {
     panel.focus();
    }
   } catch (e) {
    resolve(null);
   }
  });
 }

 window.AppConfirm = openConfirmModal;
 window.AppAlert = openAlertModal;
 window.AppPrompt = openPromptModal;
 for (const key of ["AppConfirm", "AppAlert", "AppPrompt"]) {
  try {
   Object.defineProperty(window, key, {
    value: window[key],
    writable: false,
    configurable: false,
   });
  } catch (e) {}
 }

 function extractPureReturnConfirm(code) {
  const src = (code || "").trim();
  const m = src.match(/^return\s+confirm\(\s*(['"`])([\s\S]*?)\1\s*\)\s*;?$/);
  return m ? m[2] : null;
 }

 function normalizeConfirm(form) {
  if (form.dataset.confirm) return;
  const onsubmit = form.getAttribute("onsubmit") || "";
  const msg = extractPureReturnConfirm(onsubmit);
  if (!msg) return;
  form.dataset.confirm = msg;
  form.removeAttribute("onsubmit");
  form.onsubmit = null;
 }

 function getSubmitter(e) {
  if (e.submitter) return e.submitter;
  const active = document.activeElement;
  if (
   active &&
   active.matches &&
   active.matches('button[type="submit"], input[type="submit"]')
  ) {
   return active;
  }
  return null;
 }

 function setLoading(form) {
  form.dataset.submitted = "1";
  form.setAttribute("aria-busy", "true");

  const buttons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
  buttons.forEach((btn) => {
   if (btn.tagName === "BUTTON") {
    if (btn.dataset.originalHtml === undefined) btn.dataset.originalHtml = btn.innerHTML;
    btn.innerHTML = "Saving...";
   } else {
    if (btn.dataset.originalValue === undefined) btn.dataset.originalValue = btn.value || "";
    btn.value = "Saving...";
   }
   btn.disabled = true;
  });
 }

 function restore(form) {
  delete form.dataset.submitted;
  form.removeAttribute("aria-busy");

  const buttons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
  buttons.forEach((btn) => {
   btn.disabled = false;
   if (btn.tagName === "BUTTON") {
    if (btn.dataset.originalHtml !== undefined) btn.innerHTML = btn.dataset.originalHtml;
   } else if (btn.dataset.originalValue !== undefined) {
    btn.value = btn.dataset.originalValue;
   }
  });
 }

 window.AppFormGuard = { restore };

 document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form").forEach(normalizeConfirm);
 });

 const mo = new MutationObserver((muts) => {
  for (const mut of muts) {
   mut.addedNodes.forEach((node) => {
    if (!(node instanceof Element)) return;
    if (node.matches?.("form")) normalizeConfirm(node);
    node.querySelectorAll?.("form").forEach(normalizeConfirm);
   });
  }
 });
 mo.observe(document.documentElement, { childList: true, subtree: true });

 document.addEventListener(
  "submit",
  function (e) {
   const form = e.target;
   if (!(form instanceof HTMLFormElement)) return;

   injectCsrfIfMissing(form);
   normalizeConfirm(form);

   const submitter = getSubmitter(e);
   if (form.dataset.confirmed === "1") {
    delete form.dataset.confirmed;
   } else {
    const msg = submitter?.dataset.confirm || form.dataset.confirm;
    if (msg) {
     e.preventDefault();
     e.stopImmediatePropagation();
     openConfirmModal(msg).then((ok) => {
      if (!ok) {
       restore(form);
       return;
      }
      form.dataset.confirmed = "1";
      if (form.requestSubmit) {
       form.requestSubmit(submitter || undefined);
      } else {
       form.submit();
      }
     });
     return;
    }
   }

   if (!form.noValidate && typeof form.checkValidity === "function" && !form.checkValidity()) {
    if (typeof form.reportValidity === "function") form.reportValidity();
    e.preventDefault();
    e.stopImmediatePropagation();
    restore(form);
    return;
   }

   const guard =
    form.dataset.preventDoubleSubmit === "true" ||
    submitter?.dataset.preventDoubleSubmit === "true";
   if (!guard) return;

   if (form.dataset.submitted === "1") {
    e.preventDefault();
    e.stopImmediatePropagation();
    return;
   }
   setLoading(form);
   const rollbackIfPrevented = () => {
    if (e.defaultPrevented) restore(form);
   };
   if (typeof queueMicrotask === "function") {
    queueMicrotask(rollbackIfPrevented);
   } else {
    Promise.resolve().then(rollbackIfPrevented);
   }
  },
  true,
 );

 document.addEventListener(
  "click",
  function (e) {
   const el = e.target.closest?.("[data-confirm]");
   if (!el) return;
   const tag = (el.tagName || "").toLowerCase();
   const isSubmit =
    (tag === "button" || tag === "input") &&
    (el.getAttribute("type") || "submit").toLowerCase() === "submit";
   if (isSubmit) return;

   const formId = el.getAttribute("data-confirm-form");
   const href = el.getAttribute("href");
   if (!formId && !href) return;

   e.preventDefault();
   e.stopImmediatePropagation();
   openConfirmModal(el.getAttribute("data-confirm") || "").then((ok) => {
    if (!ok) return;
    if (formId) {
     const form = document.getElementById(formId);
     if (form) {
      form.dataset.confirmed = "1";
      if (form.requestSubmit) {
       form.requestSubmit();
      } else {
       form.submit();
      }
     }
     return;
    }
    if (href && window.AppDrilldown && typeof window.AppDrilldown.navigate === "function") {
     window.AppDrilldown.navigate(href);
    } else if (href) {
     window.location.href = href;
    }
   });
  },
  true,
 );

 window.addEventListener("pageshow", (evt) => {
  if (!evt.persisted) return;
  document.querySelectorAll('form[data-prevent-double-submit="true"]').forEach(restore);
 });
})();
