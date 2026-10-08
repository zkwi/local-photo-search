# Contributing

English | [简体中文](#简体中文)

Thanks for your interest! This is a small personal project; issues and pull requests are welcome.

## Development

Set up the project as described in [Build from source](README.md#build-from-source) and [Development](README.md#development), then check your changes:

```powershell
uv run pytest           # backend tests
uvx ruff check          # lint
cd app; npm test        # UI strings and date parsing
```

Code comments are mostly in Chinese; English comments are just as welcome.

## Adding an interface language

1. Copy `app/src/locales/en.js` to `app/src/locales/<code>.js` (for example `ja.js`), change `LOCALES.en` to `LOCALES["<code>"]`, set `_name` to the language's own name, and translate the values (including `examples`, the sample searches).
   Keep every `{placeholder}`. Strings that differ between singular and plural are written as `{ one, other }` — use the [plural categories](https://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html) of your language.
2. Add `<script src="locales/<code>.js"></script>` to `app/src/index.html`, before `i18n.js`.
3. Run `npm test` in `app/`: it checks that every key and placeholder of the English file is present.
4. Dates typed into the search box (“last year”, “去年5月”) are parsed in `app/src/time.js`, which understands Chinese and English. Users of other languages can still use the date menu.

## Privacy

Never commit personal photos, `index/`, `config.json` or logs. Use freely licensed demo images for screenshots, and check that no local paths or user names are visible.

## Releases (maintainers)

1. Bump the version in `app/src-tauri/tauri.conf.json`, `app/src-tauri/Cargo.toml`, `app/package.json` and `pyproject.toml`, then run `uv lock` so `uv.lock` records it too (`uv run pytest` checks all five match). Turn `[Unreleased]` in `CHANGELOG.md` into the new version — the release is refused without that section.
2. Commit, then push a tag such as `v0.2.0`. GitHub Actions builds `LocalPhotoSearch-<version>-windows-x64.zip` on a clean Windows machine and publishes the release with the changelog section as notes.
3. To try the package locally first, run `pwsh scripts/build_release.ps1` (PowerShell 7); the zip lands in `dist/`.

---

## 简体中文

欢迎提 issue 和 pull request。这是一个人维护的小项目，改动尽量小而清楚。

**开发**：按 [从源码编译](README.zh-CN.md#从源码编译) 和 [开发](README.zh-CN.md#开发) 准备好环境，提交前跑一遍 `uv run pytest`、`uvx ruff check` 和 `app` 目录下的 `npm test`。

**新增界面语言**：

1. 把 `app/src/locales/en.js` 复制为 `app/src/locales/<语言代码>.js`，把 `LOCALES.en` 改成 `LOCALES["<语言代码>"]`，`_name` 写成该语言自己的名字，再翻译各条文案（包括示例搜索词 `examples`）。保留所有 `{参数}`；区分单复数的写成 `{ one, other }`。
2. 在 `app/src/index.html` 里 `i18n.js` 之前加一行 `<script src="locales/<语言代码>.js"></script>`。
3. 在 `app` 目录运行 `npm test`，它会检查键名和参数是否与英文一致。
4. 搜索词里的时间说法由 `app/src/time.js` 解析，目前认中文和英文；其他语言的用户仍可用时间菜单筛选。

**隐私**：不要提交个人照片、`index/`、`config.json` 和日志；截图请用可自由使用的演示图片，并确认画面里没有本机路径或用户名。

**发版（维护者）**：改 `tauri.conf.json`、`Cargo.toml`、`package.json`、`pyproject.toml` 四处版本号，再运行 `uv lock` 让 `uv.lock` 也记上（`uv run pytest` 会检查五处是否一致）；把 `CHANGELOG.md` 的 `[Unreleased]` 改成新版本，没有这一节发布会被拒绝。提交后推送 `v0.2.0` 这样的标签，GitHub Actions 会在干净的 Windows 机器上打包并发布。想先在本机试包，用 PowerShell 7 运行 `pwsh scripts/build_release.ps1`，结果在 `dist/`。
