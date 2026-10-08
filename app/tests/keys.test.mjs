// 界面里写死的文案键（脚本里的 t("…") 和页面上的 data-i18n*="…"）都要在英文文案里有，否则界面会直接显示键名。
// 两种语言都漏掉的键，各语言键名一致的检查查不出来，靠这一项。运行：在 app 目录下 npm test
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const read = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), "utf8");

test("界面用到的文案键都存在", () => {
  const ctx = vm.createContext({});
  vm.runInContext(read("locales/en.js"), ctx);
  const en = vm.runInContext("LOCALES.en", ctx);
  const used = new Set([
    ...["main.js", "time.js"].flatMap((f) => [...read(f).matchAll(/\bt\("([\w.]+)"/g)].map((m) => m[1])),
    ...[...read("index.html").matchAll(/data-i18n(?:-[\w-]+)?="([\w.]+)"/g)].map((m) => m[1]),
  ]);
  assert.ok(used.size > 100, `只找到 ${used.size} 个键，正则可能失效了`);
  assert.deepEqual([...used].filter((k) => !(k in en)), []);
});
