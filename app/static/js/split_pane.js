(function () {
 const instances = new WeakMap();

 function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
 }

 function init(container) {
  if (!container) return null;
  if (instances.has(container)) return instances.get(container);

  const handle = container.querySelector(".split-handle");
  const panes = container.querySelectorAll(".split-pane");
  if (!handle || panes.length < 2) return null;

  const key = container.getAttribute("data-split-key") || container.id || "split";
  const lsKey = `app.split.${key}.leftPx`;
  const minLeft = parseInt(container.getAttribute("data-min-left") || "320", 10);
  const minRight = parseInt(container.getAttribute("data-min-right") || "420", 10);
  const disabledMedia = container.getAttribute("data-split-disabled-media") || "(max-width: 991.98px)";
  const disabledMq = window.matchMedia ? window.matchMedia(disabledMedia) : null;

  let dragging = false;
  let activePointerId = null;
  let startX = 0;
  let startLeft = 0;
  let resizeRaf = 0;

  function isTruthyAttr(value) {
   const normalized = String(value || "").trim().toLowerCase();
   return normalized === "1" || normalized === "true" || normalized === "yes";
  }

  function isDisabled() {
   return isTruthyAttr(container.getAttribute("data-disable-split")) || Boolean(disabledMq && disabledMq.matches);
  }

  function getHandleWidth() {
   return handle.getBoundingClientRect().width || 8;
  }

  function getCurrentLeft() {
   const cols = (container.style.gridTemplateColumns || "").split(" ");
   const curLeft = parseInt((cols[0] || "").replace("px", ""), 10);
   return Number.isNaN(curLeft) ? (panes[0].getBoundingClientRect().width || minLeft) : curLeft;
  }

  function getBounds() {
   const rect = container.getBoundingClientRect();
   const w = rect.width || 0;
   const handleW = getHandleWidth();
   return {
    width: w,
    handleW,
    maxLeft: Math.max(minLeft, w - minRight - handleW),
   };
  }

  function getDefaultLeft() {
   const bounds = getBounds();
   if (!bounds.width) return minLeft;
   return clamp(Math.round((bounds.width - bounds.handleW) * 0.56), minLeft, bounds.maxLeft);
  }

  function persistLeft(left) {
   if (!Number.isFinite(left)) return;
   try {
    localStorage.setItem(lsKey, String(Math.round(left)));
   } catch (e) {}
  }

  function updateHandleAria(left) {
   const bounds = getBounds();
   handle.setAttribute("aria-disabled", "false");
   handle.setAttribute("aria-orientation", "vertical");
   handle.setAttribute("aria-valuemin", String(minLeft));
   handle.setAttribute("aria-valuemax", String(Math.round(bounds.maxLeft)));
   handle.setAttribute("aria-valuenow", String(Math.round(left)));
  }

  function clearLayout() {
   container.style.gridTemplateColumns = "";
   handle.setAttribute("aria-disabled", "true");
   handle.removeAttribute("aria-valuemin");
   handle.removeAttribute("aria-valuemax");
   handle.removeAttribute("aria-valuenow");
  }

  function applyLeft(px) {
   if (isDisabled()) {
    clearLayout();
    return getCurrentLeft();
   }
   const bounds = getBounds();
   const left = clamp(px, minLeft, bounds.maxLeft);
   container.style.gridTemplateColumns = `${left}px ${bounds.handleW}px 1fr`;
   updateHandleAria(left);
   return left;
  }

  function resetToDefault() {
   if (isDisabled()) {
    clearLayout();
    return getCurrentLeft();
   }
   const left = applyLeft(getDefaultLeft());
   persistLeft(left);
   return left;
  }

  function updateDrag(clientX) {
   if (!dragging) return;
   const dx = (clientX || 0) - startX;
   applyLeft(startLeft + dx);
  }

  function onPointerMove(e) {
   if (activePointerId !== null && e.pointerId !== activePointerId) return;
   updateDrag(e.clientX || 0);
  }

  function onMouseMove(e) {
   updateDrag(e.clientX || 0);
  }

  function onTouchMove(e) {
   if (!dragging) return;
   const touch = e.touches && e.touches[0];
   if (!touch) return;
   e.preventDefault();
   updateDrag(touch.clientX || 0);
  }

  function stopDrag() {
   if (!dragging) return;
   dragging = false;
   activePointerId = null;
   document.body.style.cursor = "";
   document.body.style.userSelect = "";
   container.querySelectorAll("iframe").forEach((f) => {
    f.style.pointerEvents = "";
   });
   persistLeft(getCurrentLeft());
   window.removeEventListener("pointermove", onPointerMove);
   window.removeEventListener("pointerup", stopDrag);
   window.removeEventListener("pointercancel", stopDrag);
   window.removeEventListener("mousemove", onMouseMove);
   window.removeEventListener("mouseup", stopDrag);
   window.removeEventListener("touchmove", onTouchMove);
   window.removeEventListener("touchend", stopDrag);
   window.removeEventListener("touchcancel", stopDrag);
  }

  function startDrag(clientX) {
   if (isDisabled()) {
    clearLayout();
    return false;
   }
   dragging = true;
   startX = clientX || 0;
   startLeft = getCurrentLeft();
   document.body.style.cursor = "col-resize";
   document.body.style.userSelect = "none";
   container.querySelectorAll("iframe").forEach((f) => {
    f.style.pointerEvents = "none";
   });
   return true;
  }

  function onResize() {
   if (resizeRaf) cancelAnimationFrame(resizeRaf);
   resizeRaf = requestAnimationFrame(() => {
    resizeRaf = 0;
    if (isDisabled()) {
     stopDrag();
     clearLayout();
     return;
    }
    applyLeft(getCurrentLeft());
   });
  }

  if (window.PointerEvent) {
   handle.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    if (!startDrag(e.clientX || 0)) return;
    activePointerId = e.pointerId;
    try {
     handle.setPointerCapture(e.pointerId);
    } catch (err) {}
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stopDrag);
    window.addEventListener("pointercancel", stopDrag);
   });
  } else {
   handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    if (!startDrag(e.clientX || 0)) return;
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", stopDrag);
   });
   handle.addEventListener(
    "touchstart",
    (e) => {
     const touch = e.touches && e.touches[0];
     if (!touch) return;
     e.preventDefault();
     if (!startDrag(touch.clientX || 0)) return;
     window.addEventListener("touchmove", onTouchMove, { passive: false });
     window.addEventListener("touchend", stopDrag);
     window.addEventListener("touchcancel", stopDrag);
    },
    { passive: false },
   );
  }

  handle.addEventListener("dblclick", (e) => {
   e.preventDefault();
   resetToDefault();
  });

  handle.addEventListener("keydown", (e) => {
   if (isDisabled()) return;
   const step = e.shiftKey ? 40 : 12;
   const curLeft = getCurrentLeft();
   if (e.key === "ArrowLeft") {
    e.preventDefault();
    persistLeft(applyLeft(curLeft - step));
   } else if (e.key === "ArrowRight") {
    e.preventDefault();
    persistLeft(applyLeft(curLeft + step));
   } else if (e.key === "Home") {
    e.preventDefault();
    persistLeft(applyLeft(minLeft));
   } else if (e.key === "End") {
    e.preventDefault();
    persistLeft(applyLeft(getBounds().maxLeft));
   }
  });

  window.addEventListener("resize", onResize);
  if (disabledMq) {
   if (disabledMq.addEventListener) {
    disabledMq.addEventListener("change", onResize);
   } else if (disabledMq.addListener) {
    disabledMq.addListener(onResize);
   }
  }

  let initialized = false;
  if (isDisabled()) {
   clearLayout();
  } else {
   try {
    const saved = parseInt(localStorage.getItem(lsKey) || "", 10);
    if (!Number.isNaN(saved)) {
     applyLeft(saved);
     initialized = true;
    }
   } catch (e) {}
   if (!initialized) applyLeft(getDefaultLeft());
  }

  const api = {
   reflow: onResize,
   reset: resetToDefault,
   getLeft: getCurrentLeft,
  };
  instances.set(container, api);
  return api;
 }

 window.SplitPane = { init };
})();
