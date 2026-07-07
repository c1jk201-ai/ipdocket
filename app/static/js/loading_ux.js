(function () {
 if (window.AppLoading && window.AppLoading._initialized) {
  return;
 }

 const BLOCKING_DELAY_MS = 180;
 const BLOCKING_AUTO_HIDE_MS = 15000;
 const FETCH_DELAY_MS = 320;

 let blockingTimer = null;
 let blockingAutoHideTimer = null;
 let blockingOverlay = null;
 let blockingMessageEl = null;
 let blockingDetailEl = null;

 let fetchPending = 0;
 let fetchTimer = null;
 let fetchIndicator = null;
 let fetchIndicatorText = null;

 function ensureBlockingOverlay() {
  if (blockingOverlay) return blockingOverlay;
  const overlay = document.createElement("div");
  overlay.id = "ipmBlockingLoading";
  overlay.className = "app-loading-overlay";
  overlay.hidden = true;
  overlay.setAttribute("aria-live", "polite");
  overlay.innerHTML =
   '<div class="app-loading-card" role="status" aria-busy="true">' +
   ' <div class="spinner-border text-primary" role="presentation"></div>' +
   ' <div class="app-loading-copy">' +
   '  <div class="app-loading-message"></div>' +
   '  <div class="app-loading-detail"></div>' +
   " </div>" +
   "</div>";
  document.body.appendChild(overlay);
  blockingOverlay = overlay;
  blockingMessageEl = overlay.querySelector(".app-loading-message");
  blockingDetailEl = overlay.querySelector(".app-loading-detail");
  return overlay;
 }

 function ensureFetchIndicator() {
  if (fetchIndicator) return fetchIndicator;
  const indicator = document.createElement("div");
  indicator.id = "ipmFetchIndicator";
  indicator.className = "app-fetch-indicator";
  indicator.setAttribute("role", "status");
  indicator.setAttribute("aria-live", "polite");
  indicator.innerHTML =
   '<span class="spinner-border spinner-border-sm" role="presentation"></span>' +
   '<span class="app-fetch-indicator__text"></span>';
  document.body.appendChild(indicator);
  fetchIndicator = indicator;
  fetchIndicatorText = indicator.querySelector(".app-fetch-indicator__text");
  return indicator;
 }

 function setBlockingMessage(message, detail) {
  ensureBlockingOverlay();
  if (blockingMessageEl) {
   blockingMessageEl.textContent = message || "Loading...";
  }
  if (blockingDetailEl) {
   blockingDetailEl.textContent = detail || " .";
  }
 }

 function showBlockingNow(message, detail) {
  clearTimeout(blockingTimer);
  setBlockingMessage(message, detail);
  ensureBlockingOverlay().hidden = false;
  document.body.classList.add("app-loading-active");
  clearTimeout(blockingAutoHideTimer);
  blockingAutoHideTimer = window.setTimeout(hideBlocking, BLOCKING_AUTO_HIDE_MS);
 }

 function scheduleBlocking(message, detail, delayMs) {
  clearTimeout(blockingTimer);
  const delay = Math.max(0, Number.isFinite(delayMs) ? Number(delayMs) : BLOCKING_DELAY_MS);
  blockingTimer = window.setTimeout(function () {
   showBlockingNow(message, detail);
  }, delay);
 }

 function hideBlocking() {
  clearTimeout(blockingTimer);
  clearTimeout(blockingAutoHideTimer);
  if (blockingOverlay) {
   blockingOverlay.hidden = true;
  }
  document.body.classList.remove("app-loading-active");
 }

 function setFetchMessage(message) {
  ensureFetchIndicator();
  if (fetchIndicatorText) {
   fetchIndicatorText.textContent = message || " Loading...";
  }
 }

 function showFetchIndicator(message) {
  setFetchMessage(message);
  ensureFetchIndicator().classList.add("is-visible");
 }

 function hideFetchIndicator() {
  if (fetchIndicator) {
   fetchIndicator.classList.remove("is-visible");
  }
 }

 function getHeaderValue(headers, key) {
  if (!headers || !key) return "";
  const target = String(key).toLowerCase();
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
   return (headers.get(key) || headers.get(target) || "").toString();
  }
  if (Array.isArray(headers)) {
   for (let i = 0; i < headers.length; i += 1) {
    const row = headers[i];
    if (!Array.isArray(row) || row.length < 2) continue;
    if (String(row[0]).toLowerCase() === target) {
     return String(row[1] || "");
    }
   }
   return "";
  }
  if (typeof headers === "object") {
   const keys = Object.keys(headers);
   for (let i = 0; i < keys.length; i += 1) {
    const name = keys[i];
    if (String(name).toLowerCase() === target) {
     return String(headers[name] || "");
    }
   }
  }
  return "";
 }

 function shouldSkipFetchLoading(input, init) {
  if (init && Object.prototype.hasOwnProperty.call(init, "ipmLoading")) {
   return init.ipmLoading === false;
  }
  const initHeaders = init ? init.headers : null;
  const reqHeaders = input && input.headers ? input.headers : null;
  const xLoading = (getHeaderValue(initHeaders, "X-App-Loading") ||
   getHeaderValue(reqHeaders, "X-App-Loading") ||
   "")
   .trim()
   .toLowerCase();
  if (xLoading === "off" || xLoading === "false" || xLoading === "0") {
   return true;
  }
  const xBg = (getHeaderValue(initHeaders, "X-App-Background") ||
   getHeaderValue(reqHeaders, "X-App-Background") ||
   "")
   .trim()
   .toLowerCase();
  return xBg === "1" || xBg === "true" || xBg === "yes";
 }

 function resolveFetchMethod(input, init) {
  let method = "";
  if (init && init.method) {
   method = String(init.method || "");
  } else if (input && typeof input === "object" && input.method) {
   method = String(input.method || "");
  }
  method = method.trim().toUpperCase();
  return method || "GET";
 }

 function beginFetch(method) {
  fetchPending += 1;
  const label =
   (method || "").toUpperCase() === "GET"
    ? "Loading..."
    : "Processing...";
  if (fetchPending === 1) {
   clearTimeout(fetchTimer);
   fetchTimer = window.setTimeout(function () {
    if (fetchPending> 0) {
     showFetchIndicator(label);
    }
   }, FETCH_DELAY_MS);
   return;
  }
  if ((method || "").toUpperCase() !== "GET") {
   setFetchMessage("Processing...");
  }
 }

 function endFetch() {
  fetchPending = Math.max(0, fetchPending - 1);
  if (fetchPending === 0) {
   clearTimeout(fetchTimer);
   hideFetchIndicator();
  }
 }

 function installFetchWrapper() {
  if (typeof window.fetch !== "function") return;
  if (window.fetch._ipmLoadingWrapped) return;

  const originalFetch = window.fetch;
  const wrappedFetch = function () {
   const input = arguments[0];
   const init = arguments[1];
   const method = resolveFetchMethod(input, init);
   const skip = shouldSkipFetchLoading(input, init);
   if (!skip) {
    beginFetch(method);
   }

   try {
    const result = originalFetch.apply(this, arguments);
    return Promise.resolve(result).finally(function () {
     if (!skip) endFetch();
    });
   } catch (err) {
    if (!skip) endFetch();
    throw err;
   }
  };
  wrappedFetch._ipmLoadingWrapped = true;
  wrappedFetch._ipmLoadingOriginal = originalFetch;
  window.fetch = wrappedFetch;
 }

 function hasSelectedFileInput(form) {
  const inputs = form.querySelectorAll('input[type="file"]');
  for (let i = 0; i < inputs.length; i += 1) {
   const input = inputs[i];
   if (input.disabled) continue;
   if (input.files && input.files.length> 0) {
    return true;
   }
  }
  return false;
 }

 function _isTrueFlag(value) {
  const raw = String(value || "").trim().toLowerCase();
  return raw === "1" || raw === "true" || raw === "yes" || raw === "on";
 }

 function _isOffFlag(value) {
  const raw = String(value || "").trim().toLowerCase();
  return raw === "0" || raw === "false" || raw === "off";
 }

 const DOWNLOAD_FILE_EXT_RE =
  /\.(?:csv|xlsx?|zip|pdf|docx?|pptx?|txt|json|xml|eml|msg|hwp|hwpx|png|jpe?g|gif|webp|tiff?)$/i;

 function _isDownloadFormat(value) {
  const raw = String(value || "").trim().toLowerCase();
  return [
   "csv",
   "xls",
   "xlsx",
   "zip",
   "pdf",
   "doc",
   "docx",
   "ppt",
   "pptx",
   "txt",
   "json",
   "xml",
  ].includes(raw);
 }

 function isLikelyDownloadUrl(targetUrl) {
  if (!targetUrl) return false;
  const path = String(targetUrl.pathname || "").toLowerCase();
  const lastSegment = path.split("/").pop() || "";

  if (/(^|\/)(download|shared-download|download-file)(\/|$)/.test(path)) {
   return true;
  }
  if (DOWNLOAD_FILE_EXT_RE.test(lastSegment)) {
   return true;
  }

  const params = targetUrl.searchParams;
  const format = params.get("format") || params.get("export_format") || "";
  if (_isDownloadFormat(format)) {
   return true;
  }

  const exportFlag = params.get("export") || params.get("download") || params.get("dl") || "";
  return _isTrueFlag(exportFlag);
 }

 function isLikelyDownloadLink(link, targetUrl) {
  if (!link) return false;
  if (link.hasAttribute("download")) return true;

  const explicit = (link.dataset.download || link.dataset.fileDownload || "")
   .trim()
   .toLowerCase();
  if (_isTrueFlag(explicit)) return true;
  if (_isOffFlag(explicit)) return false;

  if (targetUrl && isLikelyDownloadUrl(targetUrl)) {
   return true;
  }

  const label = [
   link.textContent || "",
   link.getAttribute("aria-label") || "",
   link.getAttribute("title") || "",
  ]
   .join(" ")
   .toLowerCase();
  return (
   label.includes("Download") ||
   label.includes("download") ||
   Boolean(link.querySelector(".bi-download, .fa-download, .fa-file-download"))
  );
 }

 function buildGetFormTargetUrl(form, submitter) {
  const targetUrl = new URL(form.getAttribute("action") || window.location.href, window.location.href);
  let formData = null;
  try {
   formData = submitter ? new FormData(form, submitter) : new FormData(form);
  } catch (_err) {
   formData = new FormData(form);
   if (submitter && submitter.name && !submitter.disabled) {
    formData.append(submitter.name, submitter.value || "");
   }
  }

  formData.forEach(function (value, key) {
   if (typeof value === "string") {
    targetUrl.searchParams.set(key, value);
   }
  });
  return targetUrl;
 }

 function isLikelyDownloadFormSubmit(form, submitter) {
  const method = (form.getAttribute("method") || "GET").trim().toUpperCase();
  if (method !== "GET") return false;
  try {
   return isLikelyDownloadUrl(buildGetFormTargetUrl(form, submitter));
  } catch (_err) {
   return false;
  }
 }

 function shouldHandleFormSubmit(form, submitter) {
  const submitterMode = submitter && submitter.dataset ? submitter.dataset.loading : "";
  if (_isOffFlag(submitterMode)) {
   return false;
  }

  const mode = (form.dataset.loading || "").trim().toLowerCase();
  if (mode === "off" || mode === "false" || mode === "0") {
   return false;
  }

  if (isLikelyDownloadFormSubmit(form, submitter)) {
   return false;
  }

  const submitterExplicit = submitter && submitter.dataset ? submitter.dataset.showLoading : "";
  if (_isTrueFlag(submitterExplicit)) {
   return true;
  }

  if (submitter && submitter.dataset && submitter.dataset.loadingText) {
   return true;
  }

  const explicit = (form.dataset.showLoading || "").trim().toLowerCase();
  if (explicit === "true" || explicit === "1" || explicit === "yes") {
   return true;
  }
  const method = (form.getAttribute("method") || "GET").trim().toUpperCase();
  if (method === "GET") {
   return true;
  }
  return hasSelectedFileInput(form);
 }

 function inferFormLoadingMessage(form, submitter) {
  const submitterMessage =
   submitter && submitter.dataset ? (submitter.dataset.loadingText || "").trim() : "";
  const submitterDetail =
   submitter && submitter.dataset ? (submitter.dataset.loadingDetail || "").trim() : "";

  const customMessage = submitterMessage || (form.dataset.loadingText || "").trim();
  const customDetail = submitterDetail || (form.dataset.loadingDetail || "").trim();
  if (customMessage) {
   return {
    message: customMessage,
    detail: customDetail || "Please wait.",
   };
  }

  if (hasSelectedFileInput(form)) {
   return {
    message: "Uploading and analyzing file...",
    detail: "Large files may take a moment.",
   };
  }

  const method = (form.getAttribute("method") || "GET").trim().toUpperCase();
  if (method === "GET") {
   return {
    message: "Loading results...",
    detail: "Applying filters and sorting.",
   };
  }

  return {
   message: "Processing...",
   detail: "Please wait.",
  };
 }

 function shouldHandleLinkClick(link) {
  const mode = (link.dataset.loading || "").trim().toLowerCase();
  if (mode === "off" || mode === "false" || mode === "0") {
   return false;
  }

  if (link.getAttribute("role") === "button") return false;
  if (link.dataset.bsToggle) return false;

  const target = (link.getAttribute("target") || "").trim().toLowerCase();
  if (target && target !== "_self") return false;

  const rawHref = (link.getAttribute("href") || "").trim();
  if (!rawHref) return false;
  if (rawHref.startsWith("#")) return false;

  const lowerHref = rawHref.toLowerCase();
  if (
   lowerHref.startsWith("javascript:") ||
   lowerHref.startsWith("mailto:") ||
   lowerHref.startsWith("tel:")
  ) {
   return false;
  }

  try {
   const targetUrl = new URL(link.href, window.location.href);
   if (targetUrl.origin !== window.location.origin) return false;
   if (isLikelyDownloadLink(link, targetUrl)) return false;
   if (
    targetUrl.pathname === window.location.pathname &&
    targetUrl.search === window.location.search &&
    targetUrl.hash
   ) {
    return false;
   }
  } catch (_err) {
   return false;
  }
  return true;
 }

 function shouldForceHardNavigation(link) {
  if (!link) return false;
  const target = (link.getAttribute("target") || "").trim().toLowerCase();
  if (target && target !== "_self") return false;

  const explicit = (link.dataset.fullNavigation || link.dataset.fullNav || "")
   .trim()
   .toLowerCase();
  if (_isTrueFlag(explicit)) return true;
  if (_isOffFlag(explicit)) return false;

  try {
   const targetUrl = new URL(link.href, window.location.href);
   if (targetUrl.origin !== window.location.origin) return false;
   const path = targetUrl.pathname || "";
   if (path === "/case/matter/create" || path === "/case/matter/intake") {
    return true;
   }
   if (path.startsWith("/case/matter/") && path.endsWith("/edit")) {
    return true;
   }
  } catch (_err) {
   return false;
  }
  return false;
 }

 function inferLinkLoadingMessage(link) {
  const customMessage = (link.dataset.loadingText || "").trim();
  const customDetail = (link.dataset.loadingDetail || "").trim();
  if (customMessage) {
   return {
    message: customMessage,
    detail: customDetail || "Please wait.",
   };
  }

  const href = (link.getAttribute("href") || "").toLowerCase();
  if (href.includes("/upload") || href.includes("/import")) {
   return {
    message: "Opening upload...",
    detail: "Preparing the upload workflow.",
   };
  }
  if (
   href.includes("/statistics") ||
   href.includes("/deadline") ||
   href.includes("/renewal") ||
   href.includes("/accounting")
  ) {
   return {
    message: "Loading workspace...",
    detail: "This may take a moment for large reports.",
   };
  }
  return {
   message: "Loading page...",
   detail: "Please wait.",
  };
 }

 function patchAppFormGuardRestore() {
  const guard = window.AppFormGuard;
  if (!guard || typeof guard.restore !== "function") return;
  const originalRestore = guard.restore;
  if (originalRestore._ipmLoadingWrapped) return;
  const wrappedRestore = function (form) {
   hideBlocking();
   return originalRestore.call(this, form);
  };
  wrappedRestore._ipmLoadingWrapped = true;
  guard.restore = wrappedRestore;
 }

 // Capture-phase hard navigation guard for full-page form routes.
 // Prevents partial/boosted navigation from skipping page-level JS initialization.
 document.addEventListener(
  "click",
  function (event) {
   if (event.defaultPrevented) return;
   if (event.button !== 0) return;
   if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

   const target = event.target;
   if (!(target instanceof Element)) return;
   const link = target.closest("a[href]");
   if (!link || !shouldForceHardNavigation(link)) return;

   event.preventDefault();
   window.location.assign(link.href);
  },
  true
 );

 document.addEventListener("click", function (event) {
  if (event.defaultPrevented) return;
  if (event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

  const target = event.target;
  if (!(target instanceof Element)) return;
  const link = target.closest("a[href]");
  if (!link || !shouldHandleLinkClick(link)) return;

  const msg = inferLinkLoadingMessage(link);
  scheduleBlocking(msg.message, msg.detail, BLOCKING_DELAY_MS);
  const checkPrevented = function () {
   if (event.defaultPrevented) {
    hideBlocking();
   }
  };
  if (typeof queueMicrotask === "function") {
   queueMicrotask(checkPrevented);
  } else {
   Promise.resolve().then(checkPrevented);
  }
 });

 document.addEventListener("submit", function (event) {
  if (event.defaultPrevented) return;
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const submitter = event.submitter || null;
  if (!shouldHandleFormSubmit(form, submitter)) return;

  const msg = inferFormLoadingMessage(form, submitter);
  scheduleBlocking(msg.message, msg.detail, BLOCKING_DELAY_MS);

  const checkPrevented = function () {
   if (event.defaultPrevented) {
    hideBlocking();
   }
  };
  if (typeof queueMicrotask === "function") {
   queueMicrotask(checkPrevented);
  } else {
   Promise.resolve().then(checkPrevented);
  }
 });

 window.addEventListener("pageshow", hideBlocking);
 window.addEventListener("load", hideBlocking);

 installFetchWrapper();
 patchAppFormGuardRestore();

 window.AppLoading = {
  _initialized: true,
  show: showBlockingNow,
  schedule: scheduleBlocking,
  hide: hideBlocking,
 };
})();
