// 界面文案与搜索词里时间说法的解析。运行：在 app 目录下 npm test（只用 Node 自带的测试工具，不需要安装依赖）
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const read = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), "utf8");
// 页面里的普通脚本（不是模块）：按 index.html 的顺序放进独立上下文执行，再取出要测的函数；navigator 决定界面语言
function load(lang) {
  const ctx = vm.createContext({ navigator: { languages: [lang] } });
  for (const f of ["locales/en.js", "locales/zh-CN.js", "i18n.js", "time.js"]) vm.runInContext(read(f), ctx, { filename: f });
  return vm.runInContext("({ parseTime, lastDays, t, LANG, LOCALES })", ctx);
}

const zh = load("zh-CN");
const en = load("en-US");
const NOW = new Date(2026, 9, 8, 15, 30); // 固定“现在”为 2026-10-08 下午
const ts = (y, m, d = 1) => new Date(y, m, d).getTime() / 1000;
const range = (start, end, label) => ({ start, end, label });
// 结果对象来自另一个上下文，转成本上下文的普通对象再比较
const plain = (x) => JSON.parse(JSON.stringify(x));
const parse = (api, text, now = NOW) => plain(api.parseTime(text, now));
const END = ts(2026, 9, 9); // “最近 N 天”截到今天结束

test("按系统语言选择界面语言", () => {
  assert.equal(zh.LANG, "zh-CN");
  assert.equal(en.LANG, "en");
  assert.equal(load("zh-TW").LANG, "zh-CN"); // 只有语种对得上也用它
  assert.equal(load("fr-FR").LANG, "en"); // 没有的语言退回英文
});

test("各语言文案的键名和参数与英文一致", () => {
  const placeholders = (v) => [...new Set(JSON.stringify(v).match(/\{\w+\}/g) || [])].sort();
  const { LOCALES } = zh;
  for (const [lang, table] of Object.entries(LOCALES)) {
    assert.deepEqual(Object.keys(table).sort(), Object.keys(LOCALES.en).sort(), lang);
    for (const key of Object.keys(LOCALES.en)) {
      if (key === "viewer.dimensions_value") continue; // 中文写“万像素”，英文写 MP，参数本来就不同
      assert.deepEqual(placeholders(table[key]), placeholders(LOCALES.en[key]), `${lang} ${key}`);
    }
  }
});

test("翻译：复数、千分位、缺词", () => {
  assert.equal(en.t("sel.paths_copied", { n: 1 }), "Copied 1 file path");
  assert.equal(en.t("sel.paths_copied", { n: 1234 }), "Copied 1,234 file paths");
  assert.equal(zh.t("sel.paths_copied", { n: 1234 }), "已复制 1,234 个文件路径");
  assert.equal(en.t("time.year", { y: 2025 }), "2025"); // 年份不加千分位
  assert.equal(en.t("no.such.key"), "no.such.key");
});

test("中文：年、月、日", () => {
  assert.deepEqual(parse(zh, "2023年5月20日 海边"), { range: range(ts(2023, 4, 20), ts(2023, 4, 21), "2023年5月20日"), rest: "海边" });
  assert.deepEqual(parse(zh, "2023年5月的猫"), { range: range(ts(2023, 4), ts(2023, 5), "2023年5月"), rest: "猫" });
  assert.deepEqual(parse(zh, "2024年 雪"), { range: range(ts(2024, 0), ts(2025, 0), "2024年"), rest: "雪" });
  assert.deepEqual(parse(zh, "2025 夏天"), { range: range(ts(2025, 0), ts(2026, 0), "2025年"), rest: "夏天" });
  assert.equal(parse(zh, "在2023年拍的海边照片").rest, "海边照片");
});

test("中文：今年、去年、上个月、只说几月", () => {
  assert.deepEqual(parse(zh, "去年5月 海边"), { range: range(ts(2025, 4), ts(2025, 5), "2025年5月"), rest: "海边" });
  assert.deepEqual(parse(zh, "前年拍的烟花"), { range: range(ts(2024, 0), ts(2025, 0), "2024年"), rest: "烟花" });
  assert.equal(parse(zh, "这个月").range.label, "2026年10月");
  assert.equal(parse(zh, "這個月").range.label, "2026年10月"); // 繁体
  assert.equal(parse(zh, "上个月 猫").range.label, "2026年9月");
  assert.equal(parse(zh, "上个月", new Date(2026, 0, 15)).range.label, "2025年12月");
  // 只说几月时取最近的那个：现在是 10 月，5 月是今年的，12 月是去年的
  assert.equal(parse(zh, "5月 海边").range.label, "2026年5月");
  assert.equal(parse(zh, "12月").range.label, "2025年12月");
  assert.equal(parse(zh, "5月20日").range.label, "2026年5月20日");
});

test("今天、昨天、前天在月初也能跨月", () => {
  assert.deepEqual(parse(zh, "今天").range, range(ts(2026, 9, 8), ts(2026, 9, 9), "今天"));
  const march1 = new Date(2026, 2, 1, 9);
  assert.deepEqual(parse(zh, "昨天", march1).range, range(ts(2026, 1, 28), ts(2026, 2, 1), "昨天"));
  assert.deepEqual(parse(zh, "前天", march1).range, range(ts(2026, 1, 27), ts(2026, 1, 28), "前天"));
  assert.deepEqual(parse(en, "yesterday", march1).range, range(ts(2026, 1, 28), ts(2026, 2, 1), "Yesterday"));
  assert.deepEqual(parse(en, "the day before yesterday", march1).range,
    range(ts(2026, 1, 27), ts(2026, 1, 28), "Day before yesterday"));
});

test("中文：最近 N 天、周、个月、年", () => {
  assert.deepEqual(parse(zh, "最近一个月 猫"), { range: range(END - 30 * 86400, END, "最近一个月"), rest: "猫" });
  assert.deepEqual(parse(zh, "近 3 天").range, range(END - 3 * 86400, END, "近3天"));
  assert.equal(parse(zh, "最近半年").range.start, END - 182.5 * 86400);
  assert.equal(parse(zh, "最近两週").range.start, END - 14 * 86400);
  assert.deepEqual(plain(zh.lastDays(7, "最近一周", NOW)), range(END - 7 * 86400, END, "最近一周"));
});

test("英文：年、月、日与 ISO 日期", () => {
  assert.deepEqual(parse(en, "beach May 2023"), { range: range(ts(2023, 4), ts(2023, 5), "May 2023"), rest: "beach" });
  assert.deepEqual(parse(en, "sunset on May 20, 2023"),
    { range: range(ts(2023, 4, 20), ts(2023, 4, 21), "May 20, 2023"), rest: "sunset" });
  assert.equal(parse(en, "20th of May 2023").range.label, "May 20, 2023");
  assert.equal(parse(en, "2023-05-20").range.label, "May 20, 2023");
  assert.equal(parse(en, "2023-05").range.label, "May 2023");
  assert.deepEqual(parse(en, "photos from 2023"), { range: range(ts(2023, 0), ts(2024, 0), "2023"), rest: "photos" });
});

test("英文：相对时间", () => {
  assert.deepEqual(parse(en, "cat last year"), { range: range(ts(2025, 0), ts(2026, 0), "2025"), rest: "cat" });
  assert.equal(parse(en, "last year's trip").rest, "trip");
  assert.equal(parse(en, "dog this month").range.label, "October 2026");
  assert.equal(parse(en, "last month").range.label, "September 2026");
  assert.equal(parse(en, "last month", new Date(2026, 0, 15)).range.label, "December 2025");
  assert.equal(parse(en, "May last year").range.label, "May 2025");
  assert.equal(parse(en, "last May").range.label, "May 2026");
  assert.equal(parse(en, "last October").range.label, "October 2025"); // 本月的“last”指去年
  assert.deepEqual(parse(en, "beach in May"), { range: range(ts(2026, 4), ts(2026, 5), "May 2026"), rest: "beach" });
  assert.equal(parse(en, "December").range.label, "December 2025");
  assert.equal(parse(en, "May 20").range.label, "May 20, 2026");
  assert.deepEqual(parse(en, "cat past 30 days"), { range: range(END - 30 * 86400, END, "Past 30 days"), rest: "cat" });
  assert.equal(parse(en, "last 3 months").range.start, END - 90 * 86400);
  assert.equal(parse(en, "last three weeks").range.label, "Last three weeks");
  assert.equal(parse(en, "last week").range.start, END - 7 * 86400);
});

test("标签跟随界面语言，解析与界面语言无关", () => {
  assert.equal(parse(zh, "beach May 2023").range.label, "2023年5月");
  assert.equal(parse(en, "海边 2023年5月").range.label, "May 2023");
  assert.equal(parse(en, "最近一個月").range.label, "最近一個月");
});

test("不是时间的说法不拆", () => {
  for (const text of ["海边", "附近天桥", "最近天气", "2000元的东西", "最近0天", "may contain cats",
    "march of the penguins", "Austin skyline", "the past", "todays special"]) {
    assert.equal(en.parseTime(text, NOW), null, text);
  }
});
