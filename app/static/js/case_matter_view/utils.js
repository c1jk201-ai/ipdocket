export function escapeHtml(text) {
 if (text === null || typeof text === "undefined") return "";
 return String(text)
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/\"/g, "&quot;")
  .replace(/'/g, "&#039;");
}

export function escapeAttr(text) {
 return escapeHtml(text);
}

export function safeUrl(raw) {
 const s = (raw || "").toString().trim();
 if (!s) return "";
 const low = s.toLowerCase();
 if (low.startsWith("javascript:") || low.startsWith("data:") || low.startsWith("vbscript:")) return "";
 if (s.startsWith("/") && !s.startsWith("//")) return s;
 try {
  const u = new URL(s, window.location.origin);
  if (u.origin === window.location.origin) {
   return u.pathname + u.search + u.hash;
  }
 } catch (e) {}
 return "";
}

export function parseIsoDate(value) {
 if (!value) return null;
 if (value instanceof Date) return value;
 let s = String(value).trim();
 if (!s) return null;
 if (/^\d+$/.test(s)) {
  const ms = Number(s);
  if (!Number.isNaN(ms)) return new Date(ms);
 }
 if (s.includes(" ")) {
  s = s.replace(" ", "T");
 }
 s = s.replace(/\.(\d{3})\d+/, ".$1");
 const hasTz = /[zZ]|[+-]\d{2}:\d{2}$/.test(s);
 if (!hasTz) {
  s = `${s}Z`;
 }
 const d = new Date(s);
 return Number.isNaN(d.getTime()) ? null : d;
}

export function formatDateTime(iso) {
 if (!iso) return "-";
 const d = parseIsoDate(iso);
 if (!d) return iso;
 const pad = (n) => String(n).padStart(2, "0");
 return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function formatRelativeTime(iso) {
 if (!iso) return "";
 const d = parseIsoDate(iso);
 if (!d) return "";
 const diff = Date.now() - d.getTime();
 if (diff < 0) return "";
 const seconds = Math.floor(diff / 1000);
 if (seconds < 60) return `${seconds} `;
 const minutes = Math.floor(seconds / 60);
 if (minutes < 60) return `${minutes} `;
 const hours = Math.floor(minutes / 60);
 if (hours < 24) return `${hours} `;
 const days = Math.floor(hours / 24);
 if (days < 7) return `${days}days `;
 const weeks = Math.floor(days / 7);
 if (weeks < 5) return `${weeks} `;
 const months = Math.floor(days / 30);
 if (months < 12) return `${months}items `;
 const years = Math.floor(days / 365);
 return `${years} `;
}

export function formatMoney(amount, currency) {
 const cur = String(currency || "USD").toUpperCase();
 const n = Number(amount || 0);
 if (!Number.isFinite(n)) {
  return `0 ${cur}`;
 }
 const scaleMap = { USD: 0, JPY: 0, USD: 2, EUR: 2, CNY: 2 };
 const scale = Object.prototype.hasOwnProperty.call(scaleMap, cur) ? scaleMap[cur] : 2;
 const major = n / (10 ** scale);
 return `${major.toLocaleString(undefined, {
  minimumFractionDigits: scale,
  maximumFractionDigits: scale,
 })} ${cur}`.trim();
}

export function formatDDay(dday) {
 if (dday === null || typeof dday === "undefined") return "";
 const n = Number(dday);
 if (Number.isNaN(n)) return "";
 return n>= 0 ? `D-${n}` : `D+${Math.abs(n)}`;
}

export function isPreviewableFilename(name) {
 if (!name) return false;
 return /\.(pdf|png|jpe?g|gif)$/i.test(name);
}

export function truncateText(text, limit = 160) {
 const value = String(text ?? "");
 if (!value) return "";
 if (value.length <= limit) return value;
 return `${value.slice(0, limit)}...(+${value.length - limit})`;
}

export function stringifyAuditValue(val) {
 if (val === null || typeof val === "undefined") return "";
 if (typeof val === "string" || typeof val === "number" || typeof val === "boolean") {
  return String(val);
 }
 try {
  return JSON.stringify(val);
 } catch (e) {
  return String(val);
 }
}

export function formatAuditValue(val, opts = {}) {
 const limit = opts.limit || 180;
 if (val === null || typeof val === "undefined" || val === "") return "-";
 if (typeof val === "string") return truncateText(val, limit);
 if (typeof val === "number" || typeof val === "boolean") return String(val);
 if (Array.isArray(val)) {
  const joined = val.map((v) => stringifyAuditValue(v)).filter(Boolean).join(", ");
  return joined ? truncateText(joined, limit) : "-";
 }
 if (typeof val === "object") {
  const preview = val.preview || val.doc_name || val.filename || val.label;
  if (preview) return truncateText(String(preview), limit);
  return truncateText(stringifyAuditValue(val), limit);
 }
 return truncateText(String(val), limit);
}
