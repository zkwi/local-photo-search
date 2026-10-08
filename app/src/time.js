/* 时间筛选的解析与计算：不碰页面，main.js 和 app/tests/time.test.mjs 共用。
   普通脚本（不是模块），在 i18n.js 之后、main.js 之前加载；时间戳都是本地时间的秒数，与后端按本地时间解析拍摄时间一致。
   中文（简繁）和英文的时间说法都认，与界面语言无关；生成的筛选标签用界面语言。 */

const CN_NUM = { 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10, 半: 0.5 };
const EN_NUM = { a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10,
  eleven: 11, twelve: 12 };
const MONTHS = { january: 1, february: 2, march: 3, april: 4, may: 5, june: 6, july: 7, august: 8, september: 9,
  october: 10, november: 11, december: 12, jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9,
  oct: 10, nov: 11, dec: 12 };
// 英文月份名：长的写在前面，免得 "mar" 抢先匹配 "march" 的开头
const MON = "(january|february|march|april|may|june|july|august|september|october|november|december"
  + "|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)";
const DAYS_PER = { 天: 1, 日: 1, 周: 7, 週: 7, 星期: 7, 个月: 30, 個月: 30, 年: 365, day: 1, week: 7, month: 30, year: 365 };
const DAY = 86400;
// 月份从 0 开始，日期可以溢出（如 0 日、32 日），由 Date 自动进位
const ts = (y, m, d = 1) => new Date(y, m, d).getTime() / 1000;
const yearRange = (y) => (y >= 1990 && y <= 2100 ? { start: ts(y, 0), end: ts(y + 1, 0), label: t("time.year", { y }) } : null);
const monthRange = (y, m) => (m >= 1 && m <= 12 ? { start: ts(y, m - 1), end: ts(y, m), label: fmtMonth(y, m) } : null);
const dayOf = (y, m, d, label) => ({ start: ts(y, m, d), end: ts(y, m, d + 1), label: label ?? fmtDay(new Date(y, m, d)) });
// 用户写的具体日期先检查（“5月32日”不算）；昨天、前天这类相对日期直接用 dayOf，跨月交给 Date 进位
const dayRange = (y, m, d) => (m >= 0 && m <= 11 && d >= 1 && d <= 31 ? dayOf(y, m, d) : null);

function lastDays(days, label, now = new Date()) {
  const end = ts(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  return { start: end - days * DAY, end, label };
}

/* 把搜索词里的时间说法（2025年、去年5月、最近一个月、May 2023、past 30 days……）拆出来，返回 {range, rest}；没有则返回 null */
function parseTime(text, now = new Date()) {
  const y = now.getFullYear();
  const m = now.getMonth();
  const d = now.getDate();
  const ago = { 今年: 0, 去年: 1, 前年: 2 };
  const recentMonth = (mm) => (mm <= m + 1 ? y : y - 1); // 只说几月时取最近的那一个
  const mon = (name) => MONTHS[name.toLowerCase()];
  // “最近 N 天/past N weeks”：标签用用户写的原话，中文去掉空格，英文首字母大写
  const rolling = (n, unit, phrase) => {
    const label = /[一-鿿]/.test(phrase)
      ? phrase.replace(/\s+/g, "") : phrase.replace(/\s+/g, " ").replace(/^\w/, (c) => c.toUpperCase());
    return n > 0 ? lastDays(n * DAYS_PER[unit], label, now) : null;
  };
  const rules = [
    // 中文
    [/(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]/, (a) => dayRange(+a[1], a[2] - 1, +a[3])],
    [/(\d{4})\s*年\s*(\d{1,2})\s*月份?/, (a) => monthRange(+a[1], +a[2])],
    [/(今年|去年|前年)\s*(\d{1,2})\s*月份?/, (a) => monthRange(y - ago[a[1]], +a[2])],
    [/(\d{4})\s*年/, (a) => yearRange(+a[1])],
    [/今年|去年|前年/, (a) => yearRange(y - ago[a[0]])],
    [/[这這][个個]月|本月/, () => monthRange(y, m + 1)],
    [/上[个個]?月/, () => (m === 0 ? monthRange(y - 1, 12) : monthRange(y, m))],
    [/今天/, () => dayOf(y, m, d, t("time.today"))],
    [/昨天/, () => dayOf(y, m, d - 1, t("time.yesterday"))],
    [/前天/, () => dayOf(y, m, d - 2, t("time.day_before_yesterday"))],
    // 必须带数字：“附近天桥”“最近天气”里的“近天”不是时间
    [/(?:最近|近)\s*(\d+|[一二两三四五六七八九十半])\s*(天|日|周|週|星期|[个個]月|年)/,
      (a) => rolling(Number(a[1]) || CN_NUM[a[1]], a[2], a[0])],
    [/(?<![\d年])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号號]/, (a) => dayRange(recentMonth(+a[1]), a[1] - 1, +a[2])],
    [/(?<![\d年])(\d{1,2})\s*月份?(?![\d日号號])/, (a) => monthRange(recentMonth(+a[1]), +a[1])],
    // 英文（不分大小写）与 ISO 日期
    [/\b(\d{4})-(\d{1,2})-(\d{1,2})\b/, (a) => dayRange(+a[1], a[2] - 1, +a[3])],
    [new RegExp(`\\b${MON}\\s+(\\d{1,2})(?:st|nd|rd|th)?,?\\s+(\\d{4})\\b`, "i"), (a) => dayRange(+a[3], mon(a[1]) - 1, +a[2])],
    [new RegExp(`\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+(?:of\\s+)?${MON},?\\s+(\\d{4})\\b`, "i"),
      (a) => dayRange(+a[3], mon(a[2]) - 1, +a[1])],
    [/\b(\d{4})-(\d{1,2})\b/, (a) => monthRange(+a[1], +a[2])],
    [new RegExp(`\\b${MON},?\\s+(?:of\\s+)?(\\d{4})\\b`, "i"), (a) => monthRange(+a[2], mon(a[1]))],
    [new RegExp(`\\b${MON}\\s+(this|last)\\s+year\\b`, "i"), (a) => monthRange(/last/i.test(a[2]) ? y - 1 : y, mon(a[1]))],
    [new RegExp(`\\blast\\s+${MON}\\b`, "i"), (a) => monthRange(mon(a[1]) < m + 1 ? y : y - 1, mon(a[1]))],
    [/\bthis\s+year\b/i, () => yearRange(y)],
    [/\blast\s+year\b/i, () => yearRange(y - 1)],
    [/\bthis\s+month\b/i, () => monthRange(y, m + 1)],
    [/\blast\s+month\b/i, () => (m === 0 ? monthRange(y - 1, 12) : monthRange(y, m))],
    [/\btoday\b/i, () => dayOf(y, m, d, t("time.today"))],
    [/\b(?:the\s+)?day\s+before\s+yesterday\b/i, () => dayOf(y, m, d - 2, t("time.day_before_yesterday"))],
    [/\byesterday\b/i, () => dayOf(y, m, d - 1, t("time.yesterday"))],
    [/\b(?:last|past|previous)\s+(\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(day|week|month|year)s?\b/i,
      (a) => rolling(Number(a[1]) || EN_NUM[a[1].toLowerCase()], a[2].toLowerCase(), a[0])],
    [/\b(?:last|past|previous)\s+(day|week|month|year)\b/i, (a) => rolling(1, a[1].toLowerCase(), a[0])],
    [new RegExp(`\\b${MON}\\s+(\\d{1,2})(?:st|nd|rd|th)?\\b`, "i"), (a) => dayRange(recentMonth(mon(a[1])), mon(a[1]) - 1, +a[2])],
    [new RegExp(`\\b(\\d{1,2})(?:st|nd|rd|th)?\\s+(?:of\\s+)?${MON}\\b`, "i"),
      (a) => dayRange(recentMonth(mon(a[2])), mon(a[2]) - 1, +a[1])],
    // 单独的月份名容易和普通单词撞（may、march），只认 “in May” 或整个搜索词就是月份名
    [new RegExp(`\\b(?:in|during)\\s+${MON}\\b`, "i"), (a) => monthRange(recentMonth(mon(a[1])), mon(a[1]))],
    [new RegExp(`^\\s*${MON}\\s*$`, "i"), (a) => monthRange(recentMonth(mon(a[1])), mon(a[1]))],
    [/(?:^|\s)((?:19|20)\d{2})(?=\s|$)/, (a) => yearRange(+a[1])], // 单独的“2025”；“2000元”这类不算
  ];
  for (const [re, toRange] of rules) {
    const hit = text.match(re);
    const range = hit && toRange(hit);
    if (!range) continue;
    // 去掉时间说法，以及前面的“在 / in / from”、后面紧跟的“拍的 / 的 / 's”等
    const before = text.slice(0, hit.index).replace(/在\s*$/, "").replace(/\b(?:taken\s+)?(?:in|on|from|during|since)\s*$/i, "");
    const after = text.slice(hit.index + hit[0].length)
      .replace(/^\s*(?:拍的|的时候|时候|里的|里|中的|的)/, "").replace(/^(?:'s|’s)\b/, "");
    return { range, rest: `${before} ${after}`.replace(/\s+/g, " ").trim() };
  }
  return null;
}
