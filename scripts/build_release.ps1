#Requires -Version 7
<#
.SYNOPSIS
  打 Windows 发布包：dist/LocalPhotoSearch-<版本>-windows-x64.zip，本地和 GitHub Actions 共用（用 pwsh 7 运行）。

.DESCRIPTION
  包里是编译好的桌面程序、uv、后端源码和锁文件。用户解压后双击程序即可：首次打开时程序用自带的 uv
  按 uv.lock 建 Python 环境，再下载模型。编译时把用户目录和项目目录映射掉，exe 里不留开发者本机的路径。

.PARAMETER TauriConfig
  额外的 Tauri 配置（例如测试用的另一个应用标识），会合并进 tauri.conf.json。
#>
param([string]$TauriConfig)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = (Get-Content "$root/app/src-tauri/tauri.conf.json" -Raw | ConvertFrom-Json).version
if ($env:GITHUB_REF_NAME -like "v*" -and $env:GITHUB_REF_NAME -ne "v$version") {
  throw "标签 $env:GITHUB_REF_NAME 与 tauri.conf.json 里的版本 $version 不一致"
}
$name = "LocalPhotoSearch-$version-windows-x64"
$dist = "$root/dist"
# zip 里的顶层文件夹不带版本号：升级时把新版解压到同一位置覆盖即可，索引、设置和 Python 环境都保留
$stage = "$dist/LocalPhotoSearch"
$target = "$root/app/src-tauri/target/release-dist"

# 1. 编译。--remap-path-prefix 把 cargo 缓存、rustup 和源码路径都换掉；后写的规则优先，项目目录放最后
$sep = [char]0x1f
$env:CARGO_ENCODED_RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE=~$sep--remap-path-prefix=$root=."
Push-Location "$root/app"
try {
  npm ci
  if ($LASTEXITCODE) { throw "npm ci 失败" }
  $extra = if ($TauriConfig) { @("--config", $TauriConfig) } else { @() }
  npx tauri build @extra -- --locked --target-dir $target
  if ($LASTEXITCODE) { throw "tauri build 失败" }
} finally {
  Pop-Location
  $env:CARGO_ENCODED_RUSTFLAGS = $null
}
$exe = Get-ChildItem "$target/release" -Filter *.exe | Select-Object -First 1

# 2. 隐私检查：exe 里不能出现本机的用户目录或项目目录。UTF-8 和 UTF-16（奇偶两种对齐）都查，斜杠方向也都查
$bytes = [IO.File]::ReadAllBytes($exe.FullName)
$text = [Text.Encoding]::UTF8.GetString($bytes) + [Text.Encoding]::Unicode.GetString($bytes) +
  [Text.Encoding]::Unicode.GetString($bytes, 1, $bytes.Length - 1)
foreach ($path in @($env:USERPROFILE, $root)) {
  foreach ($form in @($path, $path.Replace("\", "/"))) {
    if ($text.IndexOf($form, [StringComparison]::OrdinalIgnoreCase) -ge 0) { throw "exe 里含有本机路径：$form" }
  }
}

# 3. 组装发布目录：只放运行需要的文件，不带 index/、config.json 等个人数据
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory "$stage/backend" | Out-Null
Copy-Item $exe.FullName "$stage/Local Photo Search.exe"
$uv = (Get-Command uv).Source
Copy-Item $uv "$stage/uv.exe"
Copy-Item "$root/backend/*.py" "$stage/backend/"
foreach ($f in "pyproject.toml", "uv.lock", "LICENSE", "README.md", "README.zh-CN.md", "config.example.json") {
  Copy-Item "$root/$f" "$stage/$f"
}
# 国内网络首次启动用：只设 Python 和模型的下载镜像。统一成 CRLF，cmd.exe 对 LF 换行的批处理有已知问题
$cmd = (Get-Content "$root/scripts/start-with-china-mirrors.cmd" -Raw) -replace "`r?`n", "`r`n"
[IO.File]::WriteAllText("$stage/Start with China mirrors.cmd", $cmd, [Text.Encoding]::ASCII)

# 4. 第三方许可：exe 里编进了这些 Rust 包，uv.exe 原样附带。许可证全文取自各包源码，相同的文本只写一遍
$meta = cargo metadata --format-version 1 --locked --manifest-path "$root/app/src-tauri/Cargo.toml" `
  --filter-platform x86_64-pc-windows-msvc | ConvertFrom-Json -Depth 64
if ($LASTEXITCODE) { throw "cargo metadata 失败" }
$crates = $meta.packages | Where-Object { $_.source } | Sort-Object name, version
$texts = [ordered]@{}
foreach ($c in $crates) {
  $dir = Split-Path $c.manifest_path
  foreach ($f in Get-ChildItem $dir -File | Where-Object { $_.Name -match '^(LICEN[CS]E|COPYING|NOTICE|UNLICENSE)' }) {
    $body = (Get-Content $f.FullName -Raw).Trim()
    if (-not $texts.Contains($body)) { $texts[$body] = [Collections.Generic.List[string]]::new() }
    $texts[$body].Add("$($c.name) $($c.version)")
  }
}
$uvVersion = ((& $uv --version) -split ' ')[1]
try {
  $uvLicense = (Invoke-WebRequest "https://raw.githubusercontent.com/astral-sh/uv/$uvVersion/LICENSE-MIT" -UseBasicParsing).Content.Trim()
} catch {
  if ($env:CI) { throw "取不到 uv 的许可证：$_" }
  $uvLicense = "MIT License — https://github.com/astral-sh/uv/blob/$uvVersion/LICENSE-MIT"
  Write-Warning "取不到 uv 的许可证全文，先写链接"
}
$out = [Text.StringBuilder]::new()
[void]$out.AppendLine("Local Photo Search $version — third-party notices")
[void]$out.AppendLine("")
[void]$out.AppendLine("Python packages and the EmbeddingGemma 2 model are not included in this package. They are downloaded on")
[void]$out.AppendLine("first run from PyPI, download.pytorch.org and Hugging Face, each under its own license.")
[void]$out.AppendLine("")
[void]$out.AppendLine("== uv $uvVersion (uv.exe), Astral Software Inc., MIT OR Apache-2.0, https://github.com/astral-sh/uv ==")
[void]$out.AppendLine($uvLicense)
[void]$out.AppendLine("")
[void]$out.AppendLine("== Rust crates compiled into Local Photo Search.exe ==")
foreach ($c in $crates) { [void]$out.AppendLine("$($c.name) $($c.version)  $($c.license)  $($c.repository)") }
foreach ($body in $texts.Keys) {
  [void]$out.AppendLine("")
  [void]$out.AppendLine("== License text used by: $($texts[$body] -join ', ') ==")
  [void]$out.AppendLine($body)
}
[IO.File]::WriteAllText("$stage/THIRD_PARTY_NOTICES.txt", $out.ToString(), [Text.UTF8Encoding]::new($false))

# 5. 打包、校验和、发布说明（取 CHANGELOG 里这个版本的一节，没有就取 Unreleased）
$zip = "$dist/$name.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $stage -DestinationPath $zip
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
"$hash  $name.zip" | Set-Content "$dist/SHA256SUMS.txt" -Encoding utf8NoBOM
$changelog = Get-Content "$root/CHANGELOG.md" -Raw
$section = [regex]::Match($changelog, "(?ms)^## \[$([regex]::Escape($version))\][^\n]*\n(.*?)(?=^## |\z)")
if (-not $section.Success) {
  # 正式发布必须有这个版本自己的一节，免得把“未发布”的说明发出去
  if ($env:GITHUB_REF_NAME -like "v*") { throw "CHANGELOG.md 里没有 ## [$version] 这一节" }
  $section = [regex]::Match($changelog, "(?ms)^## \[Unreleased\][^\n]*\n(.*?)(?=^## |\z)")
}
$notes = @"
**Download** ``$name.zip``, extract it to a normal folder (not Program Files), and double-click **Local Photo Search.exe**.
The first launch downloads Python, PyTorch and the EmbeddingGemma 2 model (about 4.5 GB in total), once.

**下载** ``$name.zip``，解压到普通文件夹（不要放在 Program Files），双击 **Local Photo Search.exe**。
首次打开会自动下载 Python、PyTorch 和 EmbeddingGemma 2 模型（共约 4.5 GB），只需一次；国内网络首次启动请改为双击 **Start with China mirrors.cmd**。

**Upgrading / 升级**: close the app and extract the new zip over the old folder; your index, settings and Python environment are kept.
关掉程序，把新版 zip 解压到原来的位置覆盖即可，索引、设置和 Python 环境都会保留。

$($section.Groups[1].Value.Trim())

SHA-256: ``$hash``
"@
[IO.File]::WriteAllText("$dist/RELEASE_NOTES.md", $notes, [Text.UTF8Encoding]::new($false))
Write-Host "发布包：$zip（$([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB）"
