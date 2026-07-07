(function () {
 "use strict";

 function onReady(fn) {
  if (document.readyState === "loading") {
   document.addEventListener("DOMContentLoaded", fn, { once: true });
   return;
  }
  fn();
 }

 function initMobileNav() {
  const openToggleBtns = Array.from(document.querySelectorAll("[data-mobile-nav-toggle]"));
  const closeBtns = Array.from(document.querySelectorAll("[data-mobile-nav-close]"));
  const legacyToggle = document.getElementById("mobileNavToggle");
  if (!openToggleBtns.length && legacyToggle) openToggleBtns.push(legacyToggle);
  const toggleBtns = openToggleBtns.concat(closeBtns);
  const primaryToggle = openToggleBtns[0] || toggleBtns[0] || null;
  const backdrop = document.getElementById("mobileNavBackdrop");
  if (!primaryToggle || !backdrop) return;

  const body = document.body;
  const desktopMq = window.matchMedia("(min-width: 992px)");
  const sidebarPanels = [
   document.getElementById("primarySidebar"),
   document.getElementById("secondarySidebar"),
  ].filter(Boolean);
  let lastFocus = null;

  const focusableSelector = [
   'a[href]',
   'button:not([disabled])',
   'input:not([disabled])',
   'select:not([disabled])',
   'textarea:not([disabled])',
   '[tabindex]:not([tabindex="-1"])',
  ].join(",");

  const isOpen = () => body.classList.contains("mobile-nav-open");

  const getFocusableNavItems = () =>
   sidebarPanels
    .flatMap((panel) => Array.from(panel.querySelectorAll(focusableSelector)))
    .filter((element) => {
     const style = window.getComputedStyle(element);
     return style.visibility !== "hidden" && style.display !== "none";
    });

  const setMenuControlsExpanded = (expanded) => {
   toggleBtns.forEach((toggleBtn) => {
    toggleBtn.setAttribute("aria-expanded", expanded ? "true" : "false");
    toggleBtn.setAttribute("aria-label", expanded ? "Close menu" : "Open menu");
   });
  };

  const closeMenu = (options = {}) => {
   const wasOpen = isOpen();
   body.classList.remove("mobile-nav-open");
   setMenuControlsExpanded(false);
   backdrop.setAttribute("aria-hidden", "true");
   if (wasOpen && options.restoreFocus !== false) {
    const restoreTarget =
     lastFocus && typeof lastFocus.focus === "function" && document.contains(lastFocus)
      ? lastFocus
      : primaryToggle;
    try {
     restoreTarget.focus({ preventScroll: true });
    } catch (e) {
     restoreTarget.focus();
    }
   }
  };

  const openMenu = () => {
   lastFocus = document.activeElement;
   body.classList.add("mobile-nav-open");
   setMenuControlsExpanded(true);
   backdrop.setAttribute("aria-hidden", "false");
   window.setTimeout(() => {
    const firstItem = getFocusableNavItems()[0];
    if (!firstItem) return;
    try {
     firstItem.focus({ preventScroll: true });
    } catch (e) {
     firstItem.focus();
    }
   }, 0);
  };

  closeMenu({ restoreFocus: false });

  openToggleBtns.forEach((toggleBtn) => {
   toggleBtn.addEventListener("click", () => {
    if (isOpen()) {
     closeMenu();
     return;
    }
    openMenu();
   });
  });
  closeBtns.forEach((closeBtn) => {
   closeBtn.addEventListener("click", () => closeMenu());
  });

  backdrop.addEventListener("click", closeMenu);

  document.addEventListener("keydown", (event) => {
   if (!isOpen()) return;
   if (event.key === "Escape") {
    closeMenu();
    return;
   }
   if (event.key !== "Tab") return;

   const focusableItems = getFocusableNavItems();
   if (!focusableItems.length) {
    event.preventDefault();
    primaryToggle.focus();
    return;
   }

   const firstItem = focusableItems[0];
   const lastItem = focusableItems[focusableItems.length - 1];
   if (event.shiftKey && document.activeElement === firstItem) {
    event.preventDefault();
    lastItem.focus();
   } else if (!event.shiftKey && document.activeElement === lastItem) {
    event.preventDefault();
    firstItem.focus();
   }
  });

  document.addEventListener("click", (event) => {
   if (!event.target.closest(".sidebar-primary a, .sidebar-secondary a")) return;
   closeMenu({ restoreFocus: false });
  });

  const handleDesktop = (event) => {
   if (event.matches) {
    closeMenu({ restoreFocus: false });
   }
  };
  if (desktopMq.addEventListener) {
   desktopMq.addEventListener("change", handleDesktop);
  } else if (desktopMq.addListener) {
   desktopMq.addListener(handleDesktop);
  }
 }

 function initSecondarySidebarToggle() {
  const sidebar = document.getElementById("secondarySidebar");
  const toggleBtn = document.getElementById("secondarySidebarToggle");
  if (!sidebar || !toggleBtn) return;

  const body = document.body;
  const desktopMq = window.matchMedia("(min-width: 992px)");
  const storageKey = "app.secondarySidebarCollapsed.v2";
  const icon = toggleBtn.querySelector("i");

  const getStoredCollapsed = () => {
   try {
    const storedValue = window.localStorage.getItem(storageKey);
    if (storedValue === "1") return true;
    if (storedValue === "0") return false;
    return null;
   } catch (e) {
    return null;
   }
  };

  const storeCollapsed = (collapsed) => {
   try {
    window.localStorage.setItem(storageKey, collapsed ? "1" : "0");
   } catch (e) {
    // Ignore storage failures; the current page state still updates.
   }
  };

  const applyCollapsed = (collapsed, options = {}) => {
   const shouldApply = desktopMq.matches && Boolean(collapsed);
   body.classList.toggle("secondary-sidebar-collapsed", shouldApply);
   toggleBtn.setAttribute("aria-expanded", shouldApply ? "false" : "true");
   toggleBtn.setAttribute("aria-label", shouldApply ? "Expand secondary menu" : "Collapse secondary menu");
   toggleBtn.setAttribute("title", shouldApply ? "Expand secondary menu" : "Collapse secondary menu");
   if (icon) {
    icon.className = shouldApply ? "bi bi-chevron-right" : "bi bi-chevron-left";
   }
   if (desktopMq.matches && options.persist !== false) {
    storeCollapsed(shouldApply);
   }
  };

  const getPreferredCollapsed = () => {
   const storedCollapsed = getStoredCollapsed();
   return desktopMq.matches && storedCollapsed === true;
  };

  let isCollapsed = getPreferredCollapsed();
  applyCollapsed(isCollapsed, { persist: false });

  toggleBtn.addEventListener("click", () => {
   isCollapsed = !isCollapsed;
   applyCollapsed(isCollapsed);
  });

  const handleViewportChange = () => {
   isCollapsed = getPreferredCollapsed();
   applyCollapsed(isCollapsed, { persist: false });
  };
  if (desktopMq.addEventListener) {
   desktopMq.addEventListener("change", handleViewportChange);
  } else if (desktopMq.addListener) {
   desktopMq.addListener(handleViewportChange);
  }
 }

 function initMobileViewportGuards() {
  const root = document.documentElement;
  const body = document.body;
  const mobileMq = window.matchMedia("(max-width: 991.98px)");

  const syncViewport = () => {
   const visualViewport = window.visualViewport;
   const height = Math.round(
    (visualViewport && visualViewport.height) || window.innerHeight || 0,
   );
   if (height> 0) {
    root.style.setProperty("--app-visual-vh", height + "px");
   }

   const layoutHeight = window.innerHeight || height;
   const keyboardOpen =
    Boolean(visualViewport) && mobileMq.matches && layoutHeight - visualViewport.height> 120;
   body.classList.toggle("app-mobile-keyboard-open", keyboardOpen);
  };

  syncViewport();
  window.addEventListener("resize", syncViewport, { passive: true });
  window.addEventListener("orientationchange", syncViewport, { passive: true });
  if (window.visualViewport) {
   window.visualViewport.addEventListener("resize", syncViewport, { passive: true });
   window.visualViewport.addEventListener("scroll", syncViewport, { passive: true });
  }
  if (mobileMq.addEventListener) {
   mobileMq.addEventListener("change", syncViewport);
  } else if (mobileMq.addListener) {
   mobileMq.addListener(syncViewport);
  }
 }

 function initResponsiveTablesAndHeader() {
  const tableWrapperSkipSelector = [
   ".table-container",
   ".case-list-table-wrap",
   ".crm-table-wrap",
   ".overflow-auto",
   ".overflow-x-auto",
   ".table-card",
  ].join(", ");

  const mobileCardTableSkipSelector = [
   ".invoice-theme",
   ".case-list-table-wrap",
   ".crm-table-wrap",
   ".worklog-table-wrap",
   ".table-container",
   ".table-card",
   ".overflow-auto",
   ".overflow-x-auto",
   ".legacy-grid-wrap",
   ".fc",
  ].join(", ");

  const setHeaderHeightVar = () => {
   const header = document.querySelector("header.sticky-top");
   const height = header ? Math.ceil(header.getBoundingClientRect().height) : 0;
   document.documentElement.style.setProperty("--app-header-height", height + "px");
  };

  const wrapTables = () => {
   const main = document.getElementById("main");
   if (!main) return;

   const tables = Array.from(main.querySelectorAll("table"));
   for (const table of tables) {
    if (table.dataset.ipmTableResponsive === "0") continue;
    if (table.classList.contains("app-no-table-responsive")) continue;
    if (table.closest(".legacy-grid-wrap")) continue;
    if (table.closest(tableWrapperSkipSelector)) {
     continue;
    }
    if (table.closest(".table-responsive")) continue;

    const parent = table.parentElement;
    if (!parent) continue;
    if (parent.closest("table")) continue;

    const wrapper = document.createElement("div");
    wrapper.className = "table-responsive app-auto-table-responsive";
    parent.insertBefore(wrapper, table);
    wrapper.appendChild(table);
   }
  };

  const getHeaderLabels = (table) => {
   const headerRow =
    table.querySelector("thead tr:last-child") ||
    Array.from(table.rows || []).find((row) =>
     Array.from(row.cells || []).some(
      (cell) => (cell.tagName || "").toLowerCase() === "th",
     ),
    );
   if (!headerRow) return [];

   const headers = Array.from(headerRow.children).filter((cell) => {
    const tagName = (cell.tagName || "").toLowerCase();
    return tagName === "th" || tagName === "td";
   });

   if (!headers.length || headers.some((cell) => Number(cell.colSpan || 1) !== 1)) {
    return [];
   }

   return headers.map((cell) => (cell.textContent || "").replace(/\s+/g, " ").trim());
  };

  const shouldUseMobileCards = (table, labels) => {
   if (table.dataset.ipmMobileCards === "0") return false;
   if (table.closest(mobileCardTableSkipSelector)) return false;
   if (table.id === "main-table" || table.id === "task-table") return false;
   if (labels.length < 2 || labels.length> 6) return false;
   if (table.querySelector("tbody td[colspan], tbody td[rowspan]")) return false;
   return true;
  };

  const annotateTableCells = () => {
   const main = document.getElementById("main");
   if (!main) return;

   Array.from(main.querySelectorAll("table")).forEach((table) => {
    if (table.dataset.ipmMobileLabels === "0") return;

    const labels = getHeaderLabels(table);
    if (!labels.length) return;
    const useMobileCards = shouldUseMobileCards(table, labels);
    const actionLabelPattern = /^(Actions||Select|Actions|action|actions)$/i;

    Array.from(table.tBodies || []).forEach((tbody) => {
     Array.from(tbody.rows || []).forEach((row) => {
      const cells = Array.from(row.children).filter(
       (cell) => (cell.tagName || "").toLowerCase() === "td",
      );
      if (cells.length> labels.length) return;
      cells.forEach((cell, index) => {
       if (!cell.hasAttribute("data-label") && labels[index]) {
        cell.setAttribute("data-label", labels[index]);
       }
       if (!useMobileCards) return;

       const label = (cell.getAttribute("data-label") || labels[index] || "")
        .replace(/\s+/g, " ")
        .trim();
       const hasActionControls = Boolean(
        cell.querySelector(".btn, .btn-group, .dropdown, button, [role='button']"),
       );
       const likelyActionCell =
        actionLabelPattern.test(label) || (hasActionControls && index === cells.length - 1);
       if (likelyActionCell) {
        cell.classList.add("app-mobile-action-cell");
       }
      });
     });
    });

    if (useMobileCards) {
     table.classList.add("app-mobile-card-table");
    }
   });
  };

  setHeaderHeightVar();
  wrapTables();
  annotateTableCells();

  window.addEventListener("resize", setHeaderHeightVar, { passive: true });
  window.addEventListener("orientationchange", setHeaderHeightVar, { passive: true });

  document.addEventListener("htmx:afterSwap", () => {
   setHeaderHeightVar();
   wrapTables();
   annotateTableCells();
  });

  setTimeout(setHeaderHeightVar, 250);
 }

 function initCaseSidebarAccordion() {
  try {
   const accordion = document.getElementById("caseCategoryAccordion");
   if (!accordion) return;

   const activeLink = accordion.querySelector(".accordion-body .nav-link.active");
   const activePanel = activeLink
    ? activeLink.closest(".accordion-collapse")
    : accordion.querySelector(".accordion-collapse.show") ||
     accordion.querySelector(".accordion-collapse");
   if (!activePanel) return;

   activePanel.classList.add("show");
   const activeToggle = accordion.querySelector(`[data-bs-target="#${activePanel.id}"]`);
   if (!activeToggle) return;
   activeToggle.classList.remove("collapsed");
   activeToggle.setAttribute("aria-expanded", "true");
  } catch (err) {
   console.error("[ipm_layout] case sidebar accordion init failed", err);
  }
 }

 onReady(() => {
  initMobileViewportGuards();
  initMobileNav();
  initSecondarySidebarToggle();
  initResponsiveTablesAndHeader();
  initCaseSidebarAccordion();
 });
})();
