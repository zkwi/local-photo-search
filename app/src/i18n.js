/* 界面多语言：语言按“设置里选的 → 系统语言 → 英文”的顺序确定，切换语言时刷新页面。
   文案在 locales/*.js；普通脚本（不是模块），在 locales 之后、time.js 和 main.js 之前加载。
   不碰页面结构，app/tests 里的测试也直接加载它。 */

const LANG = pickLang();
const PLURAL = new Intl.PluralRules(LANG);
const COUNT_PARAMS = new Set(["n", "done", "total"]); // 这些参数是张数等计数，按当前语言加千分位

function pickLang() {
  const locales = globalThis.LOCALES || {};
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem("prefs"))?.lang;
  } catch {
    // 读不到偏好（例如测试环境没有 localStorage）就按系统语言
  }
  if (saved && locales[saved]) return saved;
  const nav = globalThis.navigator;
  for (const tag of nav?.languages?.length ? nav.languages : [nav?.language]) {
    if (!tag) continue;
    if (locales[tag]) return tag;
    // 只有语种对得上（如 zh-TW 对 zh-CN）也用它，总比退回英文好
    const same = Object.keys(locales).find((k) => k.split("-")[0] === tag.split("-")[0]);
    if (same) return same;
  }
  return "en";
}

/* 取原始文案（字符串、复数对象或数组）；当前语言缺这一条时用英文 */
function msg(key) {
  return globalThis.LOCALES?.[LANG]?.[key] ?? globalThis.LOCALES?.en?.[key];
}

/* 翻译并填参数：t("sel.count", {n: 3})。有 n 时按它选单复数 */
function t(key, params = {}) {
  let s = msg(key);
  if (s == null) return key;
  if (typeof s === "object") s = s[PLURAL.select(params.n ?? 0)] ?? s.other;
  return s.replace(/\{(\w+)\}/g, (all, name) => {
    if (!(name in params)) return all;
    const v = params[name];
    return typeof v === "number" && COUNT_PARAMS.has(name) ? v.toLocaleString(LANG) : String(v);
  });
}

const MONTH_FMT = new Intl.DateTimeFormat(LANG, { year: "numeric", month: "long" });
const DAY_FMT = new Intl.DateTimeFormat(LANG, { year: "numeric", month: "long", day: "numeric" });
const DATETIME_FMT = new Intl.DateTimeFormat(LANG, {
  year: "numeric", month: "long", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const fmtMonth = (y, m) => MONTH_FMT.format(new Date(y, m - 1, 1)); // m 从 1 开始
const fmtDay = (date) => DAY_FMT.format(date);
const fmtDateTime = (date) => DATETIME_FMT.format(date);

/* 把 index.html 里带 data-i18n* 属性的元素换成当前语言 */
function applyI18n(root = document) {
  document.documentElement.lang = LANG;
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  // 快捷键说明里有 <kbd>；文案是本项目自带的静态文本，不含外部输入
  for (const el of root.querySelectorAll("[data-i18n-html]")) el.innerHTML = t(el.dataset.i18nHtml);
  for (const attr of ["title", "placeholder", "aria-label"]) {
    for (const el of root.querySelectorAll(`[data-i18n-${attr}]`)) el.setAttribute(attr, t(el.getAttribute(`data-i18n-${attr}`)));
  }
}
