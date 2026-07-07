(function () {
 function normalize(value) {
  return String(value || "")
   .toLowerCase()
   .replace(/\s+/g, " ")
   .trim();
 }

 function isTypingTarget(target) {
  if (!target) return false;
  const tag = String(target.tagName || "").toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
 }

 document.addEventListener("DOMContentLoaded", function () {
  const page = document.querySelector(".help-page");
  if (!page) return;

  const input = page.querySelector("#helpFilterInput");
  const emptyState = page.querySelector("#helpFilterEmpty");
  const cards = Array.from(page.querySelectorAll(".js-help-card"));
  const navGroups = Array.from(page.querySelectorAll(".js-help-nav-group"));
  const navLinks = Array.from(page.querySelectorAll(".js-help-link[href^='#']"));
  const content = page.querySelector(".js-help-content");
  const headings = content
   ? Array.from(content.querySelectorAll("h2[id], h3[id], h4[id]"))
   : [];

  if (content) {
   headings.forEach(function (heading) {
    if (heading.querySelector(".help-anchor")) return;
    const anchor = document.createElement("a");
    anchor.className = "help-anchor";
    anchor.href = "#" + heading.id;
    anchor.setAttribute("aria-label", heading.textContent.trim() + " link");
    anchor.innerHTML = '<i class="bi bi-link-45deg"></i>';
    heading.appendChild(anchor);
   });
  }

  function applyFilter() {
   const term = normalize(input && input.value);
   let visibleCount = 0;

   cards.forEach(function (card) {
    const match = !term || normalize(card.dataset.helpFilter).includes(term);
    card.hidden = !match;
    if (match) visibleCount += 1;
   });

   navGroups.forEach(function (group) {
    const titleMatch = !term || normalize(group.dataset.helpFilter).includes(term);
    const groupLinks = Array.from(group.querySelectorAll(".js-help-nav-link"));
    let anyVisible = false;

    groupLinks.forEach(function (link) {
     const match = !term || titleMatch || normalize(link.dataset.helpFilter).includes(term);
     link.hidden = !match;
     anyVisible = anyVisible || match;
    });

    const showGroup = !term || titleMatch || anyVisible;
    group.hidden = !showGroup;
    if (showGroup) visibleCount += 1;
   });

   if (emptyState) {
    emptyState.hidden = !term || visibleCount> 0;
   }
  }

  if (input) {
   input.addEventListener("input", applyFilter);
   input.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && input.value) {
     input.value = "";
     applyFilter();
     input.blur();
    }
   });
  }

  document.addEventListener("keydown", function (event) {
   if (event.key !== "/" || event.defaultPrevented || isTypingTarget(document.activeElement)) {
    return;
   }
   if (!input) return;
   event.preventDefault();
   input.focus();
   input.select();
  });

  const linksById = new Map();
  navLinks.forEach(function (link) {
   const href = link.getAttribute("href") || "";
   const id = href.startsWith("#") ? decodeURIComponent(href.slice(1)) : "";
   if (!id) return;
   const group = linksById.get(id) || [];
   group.push(link);
   linksById.set(id, group);
  });

  function setActiveHeading(id) {
   navLinks.forEach(function (link) {
    link.classList.remove("is-active");
   });
   (linksById.get(id) || []).forEach(function (link) {
    link.classList.add("is-active");
   });
  }

  function updateActiveHeading() {
   if (!headings.length) return;
   const threshold = window.scrollY + 180;
   let currentId = headings[0].id;
   headings.forEach(function (heading) {
    const top = window.scrollY + heading.getBoundingClientRect().top;
    if (top <= threshold) {
     currentId = heading.id;
    }
   });
   setActiveHeading(currentId);
  }

  let ticking = false;
  window.addEventListener(
   "scroll",
   function () {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(function () {
     updateActiveHeading();
     ticking = false;
    });
   },
   { passive: true }
  );

  applyFilter();
  updateActiveHeading();
 });
})();
