(function () {
 var INTERACTIVE_SELECTOR = 'a, button, input, select, textarea, label, [role="button"]';
 var previewState = {
  selectedCaseId: '',
  activeTab: 'deadlines',
  summaryCache: new Map(),
  sectionCache: new Map(),
  summaryToken: 0,
  sectionToken: 0,
  restoredHighlightId: '',
  isCollapsed: false,
  lastSummaryData: null
 };

 function resetDetailedSearch() {
  var container = document.getElementById('detailedSearch');
  if (!container) return;
  container.querySelectorAll('input').forEach(function (el) {
   el.value = '';
  });
 }

 function saveCaseListPosition(caseId) {
  try {
   var scrollY = window.scrollY || document.documentElement.scrollTop || 0;
   localStorage.setItem('case_list_scroll_y', String(scrollY));
   localStorage.setItem('case_list_url', window.location.href);
   localStorage.setItem('case_list_highlight_id', String(caseId || ''));
  } catch (e) {}
 }

 function navigateWithDrilldown(url) {
  if (!url) return;
  if (window.AppDrilldown && typeof window.AppDrilldown.navigate === 'function') {
   window.AppDrilldown.navigate(url);
   return;
  }
  if (window.AppDrilldown && typeof window.AppDrilldown.save === 'function') {
   window.AppDrilldown.save();
  }
  window.location.href = url;
 }

 function restoreCaseListPositionAndHighlight() {
  var highlightId = '';
  try {
   var savedUrl = localStorage.getItem('case_list_url') || '';
   highlightId = localStorage.getItem('case_list_highlight_id') || '';
   var scrollY = parseInt(localStorage.getItem('case_list_scroll_y') || '0', 10);

   var currentBase = window.location.pathname;
   var savedBase = savedUrl ? new URL(savedUrl, window.location.origin).pathname : '';

   if (savedUrl && currentBase === savedBase) {
    if (!isNaN(scrollY) && scrollY> 0) {
     requestAnimationFrame(function () {
      window.scrollTo(0, scrollY);
     });
    }

    if (highlightId) {
     var row = document.getElementById('case-row-' + highlightId);
     if (row) {
      row.classList.add('case-row-highlight');
      setTimeout(function () {
       row.classList.remove('case-row-highlight');
      }, 2500);

      if (scrollY === 0) {
       requestAnimationFrame(function () {
        row.scrollIntoView({ block: 'center', behavior: 'smooth' });
       });
      }
     }
    }
   }
  } catch (e) {
   highlightId = '';
  }

  previewState.restoredHighlightId = highlightId || '';

  try {
   localStorage.removeItem('case_list_scroll_y');
   localStorage.removeItem('case_list_url');
   localStorage.removeItem('case_list_highlight_id');
  } catch (e2) {}
 }

 function escapeHtml(value) {
  return String(value == null ? '' : value)
   .replace(/&/g, '&amp;')
   .replace(/</g, '&lt;')
   .replace(/>/g, '&gt;')
   .replace(/"/g, '&quot;')
   .replace(/'/g, '&#39;');
 }

 function formatDateOnly(value) {
  var raw = String(value || '').trim();
  if (!raw) return '-';
  if (window.AppDate && typeof window.AppDate.formatDisplayDate === 'function') {
   return window.AppDate.formatDisplayDate(raw) || raw;
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw.slice(5, 7) + '/' + raw.slice(8, 10) + '/' + raw.slice(0, 4);
  var dt = new Date(raw);
  if (isNaN(dt.getTime())) return raw.slice(0, 10) || raw;
  var month = String(dt.getMonth() + 1).padStart(2, '0');
  var day = String(dt.getDate()).padStart(2, '0');
  var year = dt.getFullYear();
  return month + '/' + day + '/' + year;
 }

 function formatDateTime(value) {
  var raw = String(value || '').trim();
  if (!raw) return '-';
  var dt = new Date(raw);
  if (isNaN(dt.getTime())) {
   if (raw.indexOf('T')>= 0) return raw.replace('T', ' ').slice(0, 16);
   return raw;
  }
  if (window.AppDate && typeof window.AppDate.formatDisplayDateTime === 'function') {
   return window.AppDate.formatDisplayDateTime(dt);
  }
  return dt.toLocaleString('en-US', {
   timeZone: 'America/New_York',
   year: 'numeric',
   month: '2-digit',
   day: '2-digit',
   hour: '2-digit',
   minute: '2-digit',
  });
 }

 function formatCurrency(value, currency) {
  var amount = Number(value || 0);
  var code = String(currency || 'USD').toUpperCase();
  if (!isFinite(amount)) amount = 0;
  if (code === 'USD') return amount.toLocaleString('en-US') + 'USD';
  return amount.toLocaleString('en-US') + ' ' + code;
 }

 function setLinkState(el, url) {
  if (!el) return;
  var href = String(url || '').trim();
  if (href) {
   el.href = href;
   el.classList.remove('is-disabled');
   el.removeAttribute('aria-disabled');
   return;
  }
  el.href = '#';
  el.classList.add('is-disabled');
  el.setAttribute('aria-disabled', 'true');
 }

 function setButtonState(el, disabled) {
  if (!el) return;
  el.disabled = !!disabled;
  el.setAttribute('aria-disabled', disabled ? 'true' : 'false');
 }

 function setText(el, value) {
  if (!el) return;
  el.textContent = String(value == null || value === '' ? '-' : value);
 }

 function formatCountLabel(value, suffix) {
  return Number(value || 0).toLocaleString('en-US') + String(suffix || '');
 }

 function resetScrollTop(el) {
  if (!el) return;
  el.scrollTop = 0;
 }

 function isTypingElement(target) {
  if (!target || !target.tagName) return false;
  var tagName = String(target.tagName || '').toLowerCase();
  return tagName === 'input'
   || tagName === 'textarea'
   || tagName === 'select'
   || !!target.isContentEditable;
 }

 function getShortcutTab(key) {
  if (key === '1') return 'deadlines';
  if (key === '2') return 'history';
  if (key === '3') return 'memo';
  if (key === '4') return 'files';
  return '';
 }

 function isMobileListMode() {
  try {
   return window.matchMedia && window.matchMedia('(max-width: 767.98px)').matches;
  } catch (e) {
   return false;
  }
 }

 function renderPartyValue(el, label, primaryUrl, searchUrl) {
  if (!el) return;
  var name = String(label || '').trim();
  if (!name) {
   el.textContent = '-';
   return;
  }
  var html = [];
  if (primaryUrl) {
   html.push('<a href="' + escapeHtml(primaryUrl) + '">' + escapeHtml(name) + '</a>');
   if (searchUrl) {
    html.push('<a href="' + escapeHtml(searchUrl) + '" class="ms-1 text-muted small">Search</a>');
   }
  } else if (searchUrl) {
   html.push('<a href="' + escapeHtml(searchUrl) + '">' + escapeHtml(name) + '</a>');
  } else {
   html.push(escapeHtml(name));
  }
  el.innerHTML = html.join('');
 }

 function fetchJson(url) {
  return fetch(url, {
   credentials: 'same-origin',
   headers: {
    Accept: 'application/json'
   }
  }).then(function (res) {
   if (!res.ok) throw new Error('Request failed (' + res.status + ')');
   return res.json();
  });
 }

 function fetchHtml(url) {
  return fetch(url, {
   credentials: 'same-origin',
   headers: {
    'HX-Request': 'true'
   }
  }).then(function (res) {
   if (!res.ok) throw new Error('Request failed (' + res.status + ')');
   return res.text();
  });
 }

 function getShell() {
  return document.getElementById('caseListShell');
 }

 function getSplit() {
  return document.getElementById('caseListSplit');
 }

 function getPreviewCollapsedStorageKey() {
  var split = getSplit();
  var key = split ? (split.getAttribute('data-split-key') || split.id || 'case_list') : 'case_list';
  return 'app.case_preview.' + key + '.collapsed';
 }

 function loadPreviewCollapsedState() {
  try {
   return localStorage.getItem(getPreviewCollapsedStorageKey()) === '1';
  } catch (e) {
   return false;
  }
 }

 function savePreviewCollapsedState(collapsed) {
  try {
   localStorage.setItem(getPreviewCollapsedStorageKey(), collapsed ? '1' : '0');
  } catch (e) {}
 }

 function getSummaryUrl(caseId) {
  var shell = getShell();
  var template = shell ? shell.getAttribute('data-case-summary-url-template') || '' : '';
  return template ? template.replace('__CASE__', encodeURIComponent(String(caseId || ''))) : '';
 }

 function getRows() {
  return Array.prototype.slice.call(document.querySelectorAll('#caseListTableBody tr[data-case-id]'));
 }

 function getSelectedCaseIdFromUrl() {
  try {
   return new URL(window.location.href).searchParams.get('selected_case') || '';
  } catch (e) {
   return '';
  }
 }

 function updateUrlSelection(caseId) {
  try {
   var url = new URL(window.location.href);
   if (caseId) url.searchParams.set('selected_case', caseId);
   else url.searchParams.delete('selected_case');
   history.replaceState({}, '', url.toString());
  } catch (e) {}
 }

 function getPreviewRefs() {
  return {
   empty: document.getElementById('casePreviewEmpty'),
   content: document.getElementById('casePreviewContent'),
   refreshBtn: document.getElementById('casePreviewRefreshBtn'),
   summary: document.getElementById('caseListSelectionSummary'),
   selectionIndex: document.getElementById('casePreviewSelectionIndex'),
   selectionContext: document.getElementById('casePreviewSelectionContext'),
   prevBtn: document.getElementById('casePreviewPrevBtn'),
   nextBtn: document.getElementById('casePreviewNextBtn'),
   title: document.getElementById('casePreviewTitle'),
   ourRef: document.getElementById('casePreviewOurRef'),
   yourRefWrap: document.getElementById('casePreviewYourRefWrap'),
   yourRef: document.getElementById('casePreviewYourRef'),
   division: document.getElementById('casePreviewDivision'),
   type: document.getElementById('casePreviewType'),
   status: document.getElementById('casePreviewStatus'),
   focus: document.getElementById('casePreviewFocus'),
   focusValue: document.getElementById('casePreviewFocusValue'),
   openLink: document.getElementById('casePreviewOpenLink'),
   deadlineLink: document.getElementById('casePreviewDeadlineLink'),
   nextStep: document.getElementById('casePreviewNextStep'),
   nextStepTitle: document.getElementById('casePreviewNextStepTitle'),
   nextStepBody: document.getElementById('casePreviewNextStepBody'),
   nextStepLink: document.getElementById('casePreviewNextStepLink'),
   nextDeadlineCard: document.getElementById('casePreviewNextDeadlineCard'),
   nextDeadlineValue: document.getElementById('casePreviewNextDeadlineValue'),
   nextDeadlineMeta: document.getElementById('casePreviewNextDeadlineMeta'),
   openDeadlineCard: document.getElementById('casePreviewOpenDeadlineCard'),
   openDeadlineValue: document.getElementById('casePreviewOpenDeadlineValue'),
   openDeadlineMeta: document.getElementById('casePreviewOpenDeadlineMeta'),
   workflowCard: document.getElementById('casePreviewWorkflowCard'),
   workflowValue: document.getElementById('casePreviewWorkflowValue'),
   workflowMeta: document.getElementById('casePreviewWorkflowMeta'),
   financeCard: document.getElementById('casePreviewFinanceCard'),
   financeValue: document.getElementById('casePreviewFinanceValue'),
   financeMeta: document.getElementById('casePreviewFinanceMeta'),
   client: document.getElementById('casePreviewClient'),
   applicant: document.getElementById('casePreviewApplicant'),
   appNo: document.getElementById('casePreviewAppNo'),
   appDate: document.getElementById('casePreviewAppDate'),
   attorney: document.getElementById('casePreviewAttorney'),
   ops: document.getElementById('casePreviewOps'),
   recordCounts: document.getElementById('casePreviewRecordCounts'),
   lastActivity: document.getElementById('casePreviewLastActivity'),
   inlineDeadlines: document.getElementById('casePreviewInlineDeadlines'),
   inlineDeadlinesBadge: document.getElementById('casePreviewInlineDeadlinesBadge'),
   inlineDeadlinesMeta: document.getElementById('casePreviewInlineDeadlinesMeta'),
   inlineHistory: document.getElementById('casePreviewInlineHistory'),
   inlineHistoryBadge: document.getElementById('casePreviewInlineHistoryBadge'),
   inlineHistoryMeta: document.getElementById('casePreviewInlineHistoryMeta'),
   inlineMemo: document.getElementById('casePreviewInlineMemo'),
   inlineMemoBadge: document.getElementById('casePreviewInlineMemoBadge'),
   inlineMemoMeta: document.getElementById('casePreviewInlineMemoMeta'),
   inlineFiles: document.getElementById('casePreviewInlineFiles'),
   inlineFilesBadge: document.getElementById('casePreviewInlineFilesBadge'),
   inlineFilesMeta: document.getElementById('casePreviewInlineFilesMeta'),
   linkDeadlinesInline: document.getElementById('casePreviewLinkDeadlinesInline'),
   linkHistory: document.getElementById('casePreviewLinkHistory'),
   linkMemo: document.getElementById('casePreviewLinkMemo'),
   linkFiles: document.getElementById('casePreviewLinkFiles'),
   linkWorkflow: document.getElementById('casePreviewLinkWorkflow'),
   detailWorkflowBadge: document.getElementById('casePreviewDetailWorkflowBadge'),
   detailWorkflowMeta: document.getElementById('casePreviewDetailWorkflowMeta'),
   linkFinance: document.getElementById('casePreviewLinkFinance'),
   detailFinanceBadge: document.getElementById('casePreviewDetailFinanceBadge'),
   detailFinanceMeta: document.getElementById('casePreviewDetailFinanceMeta'),
   linkFamily: document.getElementById('casePreviewLinkFamily'),
   detailFamilyBadge: document.getElementById('casePreviewDetailFamilyBadge'),
   detailFamilyMeta: document.getElementById('casePreviewDetailFamilyMeta'),
   linkClient: document.getElementById('casePreviewLinkClient'),
   linkClientSearch: document.getElementById('casePreviewLinkClientSearch'),
   detailCrmBadge: document.getElementById('casePreviewDetailCrmBadge'),
   detailCrmMeta: document.getElementById('casePreviewDetailCrmMeta'),
   embedded: document.getElementById('casePreviewEmbedded')
  };
 }

 function getSelectionMetrics(caseId) {
  var rows = getRows();
  var index = rows.findIndex(function (row) {
   return String(row.getAttribute('data-case-id') || '') === String(caseId || '');
  });
  return {
   index: index,
   total: rows.length
  };
 }

 function updateSelectionPosition(caseId) {
  var refs = getPreviewRefs();
  if (!refs.selectionIndex || !refs.selectionContext) return;
  var metrics = getSelectionMetrics(caseId);
  if (!caseId || metrics.index < 0 || metrics.total <= 0) {
   refs.selectionIndex.textContent = 'No selection';
   refs.selectionContext.textContent = 'Current page to Previous/Next Matter  exists.';
   setButtonState(refs.prevBtn, true);
   setButtonState(refs.nextBtn, true);
   return;
  }

  refs.selectionIndex.textContent = 'Select ' + (metrics.index + 1).toLocaleString('en-US') + ' / ' + metrics.total.toLocaleString('en-US');
  refs.selectionContext.textContent = 'Use the current page shortcuts to jump to the needed section.';
  setButtonState(refs.prevBtn, metrics.index <= 0);
  setButtonState(refs.nextBtn, metrics.index>= (metrics.total - 1));
 }

 function updateSelectionSummary(data) {
  var refs = getPreviewRefs();
  if (!refs.summary) return;
  var total = parseInt(refs.summary.getAttribute('data-total') || '0', 10);
  var totalText = isNaN(total) || total <= 0 ? 'Current page' : total.toLocaleString('en-US') + ' items';
  var refText = data && data.our_ref ? data.our_ref : (previewState.selectedCaseId || '');
  if (!refText) {
   refs.summary.textContent = previewState.isCollapsed
    ? totalText + ' · Detail panel exists.'
    : totalText + ' Preview Select.';
   return;
  }
  refs.summary.textContent = previewState.isCollapsed
   ? totalText + ' ' + refText + ' Select · Details Hidden'
   : totalText + ' ' + refText + ' Preview';
 }

 function syncPreviewToggleButtons() {
  document.querySelectorAll('[data-case-preview-toggle="1"]').forEach(function (btn) {
   var collapsed = !!previewState.isCollapsed;
   var label = btn.getAttribute(collapsed ? 'data-collapsed-label' : 'data-expanded-label')
    || (collapsed ? 'Open details' : 'Collapse details');
   var labelEl = btn.querySelector('.case-preview-toggle__label');
   var iconEl = btn.querySelector('[data-case-preview-toggle-icon="1"]');
   btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
   btn.setAttribute('aria-label', label);
   btn.setAttribute('title', label);
   if (labelEl) labelEl.textContent = label;
   if (iconEl) iconEl.className = 'bi ' + (collapsed ? 'bi-chevron-bar-left' : 'bi-chevron-bar-right');
  });
 }

 function setPreviewCollapsed(collapsed, options) {
  var shouldCollapse = !!collapsed;
  var shell = getShell();
  var split = getSplit();
  var pane = document.getElementById('casePreviewPane');
  if (!shell || !split || !pane) {
   previewState.isCollapsed = shouldCollapse;
   syncPreviewToggleButtons();
   return;
  }

  if (shouldCollapse && !previewState.isCollapsed) {
   split.dataset.expandedGridColumns = split.style.gridTemplateColumns || '';
  }
  if (!shouldCollapse && previewState.isCollapsed) {
   split.style.gridTemplateColumns = split.dataset.expandedGridColumns || '';
  }

  previewState.isCollapsed = shouldCollapse;
  shell.classList.toggle('is-preview-collapsed', shouldCollapse);
  pane.setAttribute('aria-hidden', shouldCollapse ? 'true' : 'false');
  syncPreviewToggleButtons();
  updateSelectionSummary(previewState.lastSummaryData);

  if (!(options && options.persist === false)) {
   savePreviewCollapsedState(shouldCollapse);
  }
 }

 function showPreviewLoading(message) {
  var refs = getPreviewRefs();
  if (refs.empty) refs.empty.classList.add('d-none');
  if (refs.content) refs.content.classList.remove('d-none');
  if (refs.refreshBtn) refs.refreshBtn.classList.remove('d-none');
  updateSelectionPosition(previewState.selectedCaseId);
  if (refs.nextStep) refs.nextStep.setAttribute('data-tone', 'default');
  setText(refs.nextStepTitle, 'detail context .');
  setText(refs.nextStepBody, 'selected Matter  Actions exists.');
  if (refs.nextStepLink) {
   refs.nextStepLink.textContent = 'Loading';
   setLinkState(refs.nextStepLink, '');
  }
  if (refs.embedded) refs.embedded.innerHTML = '<div class="case-preview-embedded__status">' + escapeHtml(message || 'Loading.') + '</div>';
 }

 function showPreviewError(message) {
  var refs = getPreviewRefs();
  if (refs.empty) refs.empty.classList.add('d-none');
  if (refs.content) refs.content.classList.remove('d-none');
  if (refs.embedded) refs.embedded.innerHTML = '<div class="case-preview-embedded__status">' + escapeHtml(message || 'could not load.') + '</div>';
 }

 function clearPreview() {
  var refs = getPreviewRefs();
  if (refs.empty) refs.empty.classList.remove('d-none');
  if (refs.content) refs.content.classList.add('d-none');
  if (refs.refreshBtn) refs.refreshBtn.classList.add('d-none');
  previewState.selectedCaseId = '';
  previewState.lastSummaryData = null;
  updateSelectionPosition('');
  updateSelectionSummary(null);
 }

 function setActiveRow(caseId) {
  getRows().forEach(function (row) {
   var isActive = String(row.getAttribute('data-case-id') || '') === String(caseId || '');
   row.classList.toggle('is-active', isActive);
   row.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  updateSelectionPosition(caseId);
 }

 function getAdjacentRows(caseId) {
  var rows = getRows();
  for (var i = 0; i < rows.length; i += 1) {
   if (String(rows[i].getAttribute('data-case-id') || '') === String(caseId || '')) {
    return {
     prev: rows[i - 1] || null,
     next: rows[i + 1] || null
    };
   }
  }
  return { prev: null, next: null };
 }

 function prefetchSummary(caseId) {
  var key = String(caseId || '').trim();
  if (!key) return Promise.resolve(null);
  if (previewState.summaryCache.has(key)) return previewState.summaryCache.get(key);
  var promise = fetchJson(getSummaryUrl(key))
   .then(function (data) {
    previewState.summaryCache.set(key, Promise.resolve(data));
    return data;
   })
   .catch(function (err) {
    previewState.summaryCache.delete(key);
    throw err;
   });
  previewState.summaryCache.set(key, promise);
  return promise;
 }

 function getSummary(caseId, force) {
  var key = String(caseId || '').trim();
  if (!key) return Promise.reject(new Error('missing case id'));
  if (force) previewState.summaryCache.delete(key);
  return prefetchSummary(key);
 }

 function setTabState(tabName) {
  previewState.activeTab = tabName || 'deadlines';
  document.querySelectorAll('.case-preview-tab').forEach(function (btn) {
   var isActive = btn.getAttribute('data-preview-tab') === previewState.activeTab;
   btn.classList.toggle('is-active', isActive);
   btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  document.querySelectorAll('[data-preview-tab-target]').forEach(function (btn) {
   var isActive = btn.getAttribute('data-preview-tab-target') === previewState.activeTab;
   btn.classList.toggle('is-active', isActive);
   btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
   var card = btn.closest('[data-inline-tab-card]');
   if (card) card.classList.toggle('is-active', isActive);
  });
 }

 function getDdayLabel(nextDeadline) {
  if (!nextDeadline || typeof nextDeadline.d_day !== 'number') return '';
  if (nextDeadline.d_day === 0) return 'D-Day';
  if (nextDeadline.d_day> 0) return 'D-' + nextDeadline.d_day;
  return 'D+' + Math.abs(nextDeadline.d_day);
 }

 function getRecommendedAction(data) {
  var counts = data && data.counts ? data.counts : {};
  var links = data && data.links ? data.links : {};
  var nextDeadline = data && data.next_deadline ? data.next_deadline : null;
  var invoice = data && data.invoice ? data.invoice : {};
  var ddayText = getDdayLabel(nextDeadline);

  if (nextDeadline && nextDeadline.date && typeof nextDeadline.d_day === 'number' && nextDeadline.d_day <= 7) {
   return {
    tone: nextDeadline.d_day <= 0 ? 'alert' : 'info',
    title: ' Deadline Confirm',
    body: (nextDeadline.label || '  Deadline') + ' · ' + formatDateOnly(nextDeadline.date) + (ddayText ? ' · ' + ddayText : ''),
    url: nextDeadline.url || links.deadlines,
    cta: 'Deadline Open'
   };
  }

  if (Number(counts.active_workflows || 0)> 0) {
   return {
    tone: 'info',
    title: 'Open tasks ',
    body: 'In progress Task ' + Number(counts.active_workflows || 0).toLocaleString('en-US') + 'open.',
    url: links.workflow,
    cta: 'View tasks'
   };
  }

  if (Number(invoice.outstanding || 0)> 0 && links.finance) {
   return {
    tone: 'finance',
    title: 'Outstanding balance and billing status',
    body: formatCurrency(invoice.outstanding || 0, invoice.currency || 'USD') + ' outstanding · overdue ' + Number(invoice.overdue_count || 0).toLocaleString('en-US') + ' item(s)',
    url: links.finance,
    cta: 'View billing'
   };
  }

  if (Number(counts.files || 0)> 0 || Number(counts.memos || 0)> 0) {
   return {
    tone: 'default',
    title: 'Recent records ',
    body: 'Notes ' + Number(counts.memos || 0).toLocaleString('en-US') + ' · Files ' + Number(counts.files || 0).toLocaleString('en-US') + ' linked.',
    url: links.history || links.files || links.case,
    cta: ' View'
   };
  }

  return {
   tone: 'default',
   title: 'full matter context column Confirm',
   body: ' to All Details to Go exists.',
   url: links.case,
   cta: 'View full matter'
  };
 }

 function renderRecommendedAction(data) {
  var refs = getPreviewRefs();
  var action = getRecommendedAction(data || {});
  if (refs.nextStep) refs.nextStep.setAttribute('data-tone', action.tone || 'default');
  setText(refs.nextStepTitle, action.title || '-');
  setText(refs.nextStepBody, action.body || '-');
  if (refs.nextStepLink) {
   refs.nextStepLink.textContent = action.cta || ' Go';
   setLinkState(refs.nextStepLink, action.url || '');
  }
 }

 function renderInlineDrill(data) {
  var refs = getPreviewRefs();
  var counts = data && data.counts ? data.counts : {};
  var links = data && data.links ? data.links : {};
  var nextDeadline = data && data.next_deadline ? data.next_deadline : null;
  var lastActivityAt = data && data.last_activity_at ? data.last_activity_at : '';
  var deadlineDday = getDdayLabel(nextDeadline);
  var inlineItems = [
   {
    button: refs.inlineDeadlines,
    badge: refs.inlineDeadlinesBadge,
    meta: refs.inlineDeadlinesMeta,
    detail: refs.linkDeadlinesInline,
    detailUrl: links.deadlines,
    sectionUrl: links.section_deadlines,
    badgeText: formatCountLabel(counts.open_deadlines || data.open_deadline_count || 0, 'items'),
    metaText: nextDeadline && nextDeadline.date
     ? ((nextDeadline.label || 'Next deadline') + ' · ' + formatDateOnly(nextDeadline.date) + (deadlineDday ? ' · ' + deadlineDday : ''))
     : ' Deadline if none All Matter to .'
   },
   {
    button: refs.inlineHistory,
    badge: refs.inlineHistoryBadge,
    meta: refs.inlineHistoryMeta,
    detail: refs.linkHistory,
    detailUrl: links.history,
    sectionUrl: links.section_history,
    badgeText: lastActivityAt ? 'Recent Change' : ' ',
    metaText: lastActivityAt
     ? ('Recent ' + formatDateTime(lastActivityAt))
     : '  none.'
   },
   {
    button: refs.inlineMemo,
    badge: refs.inlineMemoBadge,
    meta: refs.inlineMemoMeta,
    detail: refs.linkMemo,
    detailUrl: links.memo,
    sectionUrl: links.section_memo,
    badgeText: formatCountLabel(counts.memos || 0, 'items'),
    metaText: Number(counts.memos || 0)> 0
     ? ('Linked notes ' + formatCountLabel(counts.memos || 0, 'items'))
     : 'No registered notes.'
   },
   {
    button: refs.inlineFiles,
    badge: refs.inlineFilesBadge,
    meta: refs.inlineFilesMeta,
    detail: refs.linkFiles,
    detailUrl: links.files,
    sectionUrl: links.section_files,
    badgeText: formatCountLabel(counts.files || 0, 'items'),
    metaText: Number(counts.files || 0)> 0
     ? (formatCountLabel(counts.files || 0, 'items') + ' available as read-only files.')
     : 'No registered files.'
   }
  ];

  inlineItems.forEach(function (item) {
   setText(item.badge, item.badgeText);
   setText(item.meta, item.metaText);
   setLinkState(item.detail, item.detailUrl);
   setButtonState(item.button, !item.sectionUrl);
   var card = item.button ? item.button.closest('[data-inline-tab-card]') : null;
   if (card) card.classList.toggle('is-disabled', !item.sectionUrl);
  });
 }

 function renderDetailDrill(data) {
  var refs = getPreviewRefs();
  var counts = data && data.counts ? data.counts : {};
  var people = data && data.people ? data.people : {};
  var links = data && data.links ? data.links : {};
  var invoice = data && data.invoice ? data.invoice : {};
  var workflowCard = refs.linkWorkflow ? refs.linkWorkflow.closest('.case-preview-detail-card') : null;
  var financeCard = refs.linkFinance ? refs.linkFinance.closest('.case-preview-detail-card') : null;
  var familyCard = refs.linkFamily ? refs.linkFamily.closest('.case-preview-detail-card') : null;
  var crmCard = refs.linkClient ? refs.linkClient.closest('.case-preview-detail-card') : null;

  setText(refs.detailWorkflowBadge, formatCountLabel(counts.active_workflows || 0, 'items'));
  setText(
   refs.detailWorkflowMeta,
   Number(counts.workflows || 0)> 0
    ? ('All ' + formatCountLabel(counts.workflows || 0, 'items') + ' · In progress ' + formatCountLabel(counts.active_workflows || 0, 'items'))
    : 'Open tasks none.'
  );
  setLinkState(refs.linkWorkflow, links.workflow);
  if (workflowCard) workflowCard.classList.toggle('is-disabled', !links.workflow);

  setText(refs.detailFinanceBadge, formatCurrency(invoice.outstanding || 0, invoice.currency || 'USD'));
  setText(
   refs.detailFinanceMeta,
   links.finance
    ? ('overdue ' + formatCountLabel(invoice.overdue_count || 0, 'items') + ' · Go to billing details')
    : 'No invoice access.'
  );
  setLinkState(refs.linkFinance, links.finance);
  if (financeCard) financeCard.classList.toggle('is-disabled', !links.finance);

  setText(refs.detailFamilyBadge, links.family ? 'Go ' : '');
  setText(
   refs.detailFamilyMeta,
   links.family
    ? 'Go to filing, family, and matter sections.'
    : 'No linked filing record.'
  );
  setLinkState(refs.linkFamily, links.family);
  if (familyCard) familyCard.classList.toggle('is-disabled', !links.family);

  setText(
   refs.detailCrmBadge,
   links.client ? 'linked' : (links.client_search ? 'Search ' : ' ')
  );
  setText(
   refs.detailCrmMeta,
   people.client_name
    ? ('Open CRM client: ' + people.client_name)
    : 'Search CRM clients for a match.'
  );
  setLinkState(refs.linkClient, links.client);
  setLinkState(refs.linkClientSearch, links.client_search);
  if (crmCard) crmCard.classList.toggle('is-disabled', !links.client && !links.client_search);
 }

 function renderSummary(data) {
  var refs = getPreviewRefs();
  var counts = data && data.counts ? data.counts : {};
  var people = data && data.people ? data.people : {};
  var application = data && data.application ? data.application : {};
  var links = data && data.links ? data.links : {};
  var nextDeadline = data && data.next_deadline ? data.next_deadline : null;
  var invoice = data && data.invoice ? data.invoice : {};
  previewState.lastSummaryData = data || null;

  if (refs.empty) refs.empty.classList.add('d-none');
  if (refs.content) refs.content.classList.remove('d-none');
  if (refs.refreshBtn) refs.refreshBtn.classList.remove('d-none');

  setText(refs.title, data && data.title ? data.title : '-');
  setText(refs.ourRef, data && data.our_ref ? data.our_ref : '-');
  if (refs.yourRefWrap) refs.yourRefWrap.classList.toggle('d-none', !(data && data.your_ref));
  setText(refs.yourRef, data && data.your_ref ? data.your_ref : '');
  setText(refs.division, data && data.division ? data.division : '-');
  setText(refs.type, data && data.type ? data.type : '-');
  setText(refs.status, data && data.status ? data.status : '-');
  if (refs.focus) refs.focus.setAttribute('data-tone', data && data.focus && data.focus.tone ? data.focus.tone : 'default');
  setText(refs.focusValue, data && data.focus && data.focus.label ? data.focus.label : '-');
  updateSelectionPosition(previewState.selectedCaseId);

  setLinkState(refs.openLink, links.case);
  setLinkState(refs.deadlineLink, links.deadlines);
  setLinkState(refs.linkDeadlinesInline, links.deadlines);
  setLinkState(refs.openDeadlineCard, links.deadlines);
  setLinkState(refs.workflowCard, links.workflow);
  setLinkState(refs.financeCard, links.finance);
  setLinkState(refs.linkHistory, links.history);
  setLinkState(refs.linkMemo, links.memo);
  setLinkState(refs.linkFiles, links.files);
  setLinkState(refs.linkWorkflow, links.workflow);
  setLinkState(refs.linkFinance, links.finance);
  setLinkState(refs.linkFamily, links.family);
  setLinkState(refs.linkClient, links.client);
  setLinkState(refs.linkClientSearch, links.client_search);

  if (nextDeadline && nextDeadline.url) setLinkState(refs.nextDeadlineCard, nextDeadline.url);
  else setLinkState(refs.nextDeadlineCard, links.deadlines);

  if (nextDeadline && nextDeadline.date) {
   var ddayText = '';
   if (typeof nextDeadline.d_day === 'number') {
    if (nextDeadline.d_day === 0) ddayText = 'D-Day';
    else if (nextDeadline.d_day> 0) ddayText = 'D-' + nextDeadline.d_day;
    else ddayText = 'D+' + Math.abs(nextDeadline.d_day);
   }
   refs.nextDeadlineValue.textContent = formatDateOnly(nextDeadline.date) + (ddayText ? ' ' + ddayText : '');
   refs.nextDeadlineMeta.textContent = nextDeadline.label || '  Deadline';
  } else {
   refs.nextDeadlineValue.textContent = '';
   refs.nextDeadlineMeta.textContent = 'No deadline.';
  }

  refs.openDeadlineValue.textContent = Number(counts.open_deadlines || data.open_deadline_count || 0).toLocaleString('en-US');
  refs.openDeadlineMeta.textContent = 'Go to matter deadlines';

  refs.workflowValue.textContent = Number(counts.workflows || 0).toLocaleString('en-US');
  refs.workflowMeta.textContent = 'Open tasks ' + Number(counts.active_workflows || 0).toLocaleString('en-US');

  refs.financeValue.textContent = formatCurrency(invoice.outstanding || 0, invoice.currency || 'USD');
  refs.financeMeta.textContent = links.finance
   ? ('overdue ' + Number(invoice.overdue_count || 0).toLocaleString('en-US') + ' item(s)')
   : 'No invoice access.';

  renderPartyValue(refs.client, people.client_name, links.client, links.client_search);
  renderPartyValue(refs.applicant, people.applicant_name, links.applicant, links.applicant_search);
  setText(refs.appNo, application.number ? application.number : '-');
  setText(refs.appDate, application.date ? formatDateOnly(application.date) : '-');
  setText(refs.attorney, people.attorney ? people.attorney : '-');
  setText(refs.ops, [people.handler, people.manager].filter(Boolean).join(' / ') || '-');
  setText(refs.recordCounts, 'Notes ' + Number(counts.memos || 0).toLocaleString('en-US') + ' / Files ' + Number(counts.files || 0).toLocaleString('en-US'));
  setText(refs.lastActivity, data.last_activity_at ? formatDateTime(data.last_activity_at) : '-');
  renderRecommendedAction(data);
  renderInlineDrill(data);
  renderDetailDrill(data);
 }

 function getSectionUrl(summary, tabName) {
  var links = summary && summary.links ? summary.links : {};
  if (tabName === 'history') return links.section_history || '';
  if (tabName === 'memo') return links.section_memo || '';
  if (tabName === 'files') return links.section_files || '';
  return links.section_deadlines || '';
 }

 function loadPreviewTab(tabName, options) {
  var activeCaseId = previewState.selectedCaseId;
  if (!activeCaseId) return;
  setTabState(tabName);

  getSummary(activeCaseId, false).then(function (summary) {
   var sectionUrl = getSectionUrl(summary, previewState.activeTab);
   var cacheKey = activeCaseId + ':' + previewState.activeTab;
   var refs = getPreviewRefs();
   if (!sectionUrl) {
    if (refs.embedded) refs.embedded.innerHTML = '<div class="case-preview-embedded__status">Display Details none.</div>';
    return;
   }
   if (!(options && options.force) && previewState.sectionCache.has(cacheKey)) {
    if (refs.embedded) {
     refs.embedded.innerHTML = previewState.sectionCache.get(cacheKey);
     resetScrollTop(refs.embedded);
    }
    return;
   }

   if (refs.embedded) {
    refs.embedded.innerHTML = '<div class="case-preview-embedded__status">' + escapeHtml('Details Loading.') + '</div>';
    resetScrollTop(refs.embedded);
   }
   var token = ++previewState.sectionToken;
   fetchHtml(sectionUrl).then(function (html) {
    if (token !== previewState.sectionToken || previewState.selectedCaseId !== activeCaseId || previewState.activeTab !== tabName) return;
    previewState.sectionCache.set(cacheKey, html);
    if (refs.embedded) {
     refs.embedded.innerHTML = html;
     resetScrollTop(refs.embedded);
    }
   }).catch(function () {
    if (token !== previewState.sectionToken || previewState.selectedCaseId !== activeCaseId || previewState.activeTab !== tabName) return;
    if (refs.embedded) refs.embedded.innerHTML = '<div class="case-preview-embedded__status">Details could not load.</div>';
   });
  }).catch(function () {
   showPreviewError('Details could not load.');
  });
 }

 function prefetchNeighbors(caseId) {
  var adjacent = getAdjacentRows(caseId);
  if (adjacent.next) prefetchSummary(adjacent.next.getAttribute('data-case-id'));
  if (adjacent.prev) prefetchSummary(adjacent.prev.getAttribute('data-case-id'));
 }

 function selectCase(caseId, options) {
  var key = String(caseId || '').trim();
  if (!key) return;
  previewState.selectedCaseId = key;
  setActiveRow(key);
  updateUrlSelection(key);
  resetScrollTop(getPreviewRefs().content);
  showPreviewLoading('Matter Loading.');

  var token = ++previewState.summaryToken;
  getSummary(key, !!(options && options.force)).then(function (data) {
   if (token !== previewState.summaryToken || previewState.selectedCaseId !== key) return;
   renderSummary(data);
   updateSelectionSummary(data);
   loadPreviewTab(previewState.activeTab || 'deadlines', { force: !!(options && options.forceTab) });
   prefetchNeighbors(key);
  }).catch(function () {
   if (token !== previewState.summaryToken || previewState.selectedCaseId !== key) return;
   showPreviewError('Matter could not load.');
  });
 }

 function moveSelection(direction) {
  var rows = getRows();
  if (!rows.length) return;
  var currentIndex = rows.findIndex(function (row) {
   return String(row.getAttribute('data-case-id') || '') === String(previewState.selectedCaseId || '');
  });
  if (currentIndex < 0) currentIndex = 0;
  var nextIndex = Math.max(0, Math.min(rows.length - 1, currentIndex + direction));
  var nextRow = rows[nextIndex];
  if (!nextRow) return;
  nextRow.focus();
  selectCase(nextRow.getAttribute('data-case-id'));
 }

 function initPreviewEvents() {
  document.addEventListener('click', function (event) {
   var resetTrigger = event.target.closest('[data-action="reset-detailed-search"]');
   if (resetTrigger) {
    event.preventDefault();
    resetDetailedSearch();
    return;
   }

   var previewToggle = event.target.closest('[data-case-preview-toggle="1"]');
   if (previewToggle) {
    event.preventDefault();
    setPreviewCollapsed(!previewState.isCollapsed);
    return;
   }

   var previewNav = event.target.closest('[data-case-preview-nav="1"]');
   if (previewNav && previewState.selectedCaseId) {
    saveCaseListPosition(previewState.selectedCaseId);
   }

   if (event.target.closest('#casePreviewPrevBtn')) {
    event.preventDefault();
    moveSelection(-1);
    return;
   }

   if (event.target.closest('#casePreviewNextBtn')) {
    event.preventDefault();
    moveSelection(1);
    return;
   }

   var inlineTabBtn = event.target.closest('[data-preview-tab-target]');
   if (inlineTabBtn) {
    event.preventDefault();
    var inlineTabName = inlineTabBtn.getAttribute('data-preview-tab-target') || 'deadlines';
    loadPreviewTab(inlineTabName, { force: false });
    return;
   }

   var tabBtn = event.target.closest('.case-preview-tab');
   if (tabBtn) {
    event.preventDefault();
    var tabName = tabBtn.getAttribute('data-preview-tab') || 'deadlines';
    loadPreviewTab(tabName, { force: false });
    return;
   }

   var mobileCard = event.target.closest('.case-list-mobile-card[data-case-id]');
   if (mobileCard) {
    if (event.target.closest(INTERACTIVE_SELECTOR)) return;
    var mobileUrl = mobileCard.getAttribute('data-case-url') || '';
    var mobileCaseId = mobileCard.getAttribute('data-case-id') || '';
    if (!mobileUrl) return;
    event.preventDefault();
    saveCaseListPosition(mobileCaseId);
    navigateWithDrilldown(mobileUrl);
    return;
   }

   var row = event.target.closest('#caseListTableBody tr[data-case-id]');
   if (!row) return;
   if (event.target.closest(INTERACTIVE_SELECTOR)) return;
   event.preventDefault();
   selectCase(row.getAttribute('data-case-id'));
  });

  document.addEventListener('dblclick', function (event) {
   var row = event.target.closest('#caseListTableBody tr[data-case-id]');
   if (!row || event.target.closest(INTERACTIVE_SELECTOR)) return;
   var url = row.getAttribute('data-case-url') || '';
   var caseId = row.getAttribute('data-case-id') || '';
   if (!url) return;
   saveCaseListPosition(caseId);
   navigateWithDrilldown(url);
  });

  document.addEventListener('mouseenter', function (event) {
   var row = event.target.closest('#caseListTableBody tr[data-case-id]');
   if (!row) return;
   prefetchSummary(row.getAttribute('data-case-id'));
  }, true);

  document.addEventListener('focusin', function (event) {
   var row = event.target.closest('#caseListTableBody tr[data-case-id]');
   if (!row) return;
   prefetchSummary(row.getAttribute('data-case-id'));
  });

  document.addEventListener('keydown', function (event) {
   if (!event.altKey && !event.ctrlKey && !event.metaKey && !isTypingElement(event.target) && previewState.selectedCaseId) {
    var shortcutTab = getShortcutTab(event.key);
    if (shortcutTab) {
     event.preventDefault();
     loadPreviewTab(shortcutTab, { force: false });
     return;
    }
   }

   var mobileCard = event.target.closest('.case-list-mobile-card[data-case-id]');
   if (mobileCard && (event.key === 'Enter' || event.key === ' ')) {
    var mobileUrl = mobileCard.getAttribute('data-case-url') || '';
    var mobileCaseId = mobileCard.getAttribute('data-case-id') || '';
    if (mobileUrl) {
     event.preventDefault();
     saveCaseListPosition(mobileCaseId);
     navigateWithDrilldown(mobileUrl);
    }
    return;
   }

   var row = event.target.closest('#caseListTableBody tr[data-case-id]');
   if (!row) return;
   if (event.target.closest(INTERACTIVE_SELECTOR) && event.target !== row) return;
   if (event.key === 'ArrowDown') {
    event.preventDefault();
    moveSelection(1);
    return;
   }
   if (event.key === 'ArrowUp') {
    event.preventDefault();
    moveSelection(-1);
    return;
   }
   if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    selectCase(row.getAttribute('data-case-id'));
   }
  });

  var refreshBtn = document.getElementById('casePreviewRefreshBtn');
  if (refreshBtn) {
   refreshBtn.addEventListener('click', function () {
    if (!previewState.selectedCaseId) return;
    selectCase(previewState.selectedCaseId, { force: true, forceTab: true });
   });
  }
 }

 function initPreviewSelection() {
  var rows = getRows();
  if (!rows.length) {
   clearPreview();
   return;
  }

  var selectedCaseId = getSelectedCaseIdFromUrl();
  if (!selectedCaseId && previewState.restoredHighlightId) {
   selectedCaseId = previewState.restoredHighlightId;
  }
  if (!selectedCaseId && isMobileListMode()) {
   clearPreview();
   return;
  }
  if (!selectedCaseId && !previewState.isCollapsed && rows[0]) {
   selectedCaseId = rows[0].getAttribute('data-case-id') || '';
  }

  if (!selectedCaseId) {
   clearPreview();
   return;
  }
  selectCase(selectedCaseId);
 }

 function initSplitPane() {
  try {
   if (window.SplitPane && typeof window.SplitPane.init === 'function') {
    window.SplitPane.init(getSplit());
   }
  } catch (e) {}
 }

 function initPreviewCollapseState() {
  setPreviewCollapsed(isMobileListMode() || loadPreviewCollapsedState(), { persist: false });
 }

 function init() {
  initSplitPane();
  initPreviewCollapseState();
  initPreviewEvents();
  initPreviewSelection();
 }

 if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', function () {
   restoreCaseListPositionAndHighlight();
   init();
  }, { once: true });
 } else {
  restoreCaseListPositionAndHighlight();
  init();
 }

 window.addEventListener('pageshow', function (event) {
  restoreCaseListPositionAndHighlight();
  if (event && event.persisted && previewState.restoredHighlightId) {
   selectCase(previewState.restoredHighlightId);
  }
 });
 window.saveCaseListPosition = saveCaseListPosition;
})();
