# Security policy

English | [简体中文](#简体中文)

## Reporting a vulnerability

Please don't report security problems in public issues. Open the **Security** tab of this repository and click **Report a vulnerability** to send a private report.

## How the app protects your photos

- Everything runs on your computer. After the first launch, which downloads Python packages and the model, the app makes no network connections.
- The background service listens only on `127.0.0.1` with a random port, accepts only requests addressed to `127.0.0.1` or `localhost` (protection against DNS rebinding), and answers cross-origin requests only from the app window.
- The app window has a strict Content Security Policy: it can load only its own files, thumbnails, and data from the local background service.
- Photo folders are only read; exporting makes copies.

## 简体中文

请不要在公开 issue 里报告安全问题。在本仓库的 **Security** 标签页点 **Report a vulnerability** 私下提交。

程序的防护措施：全部在本机运行，首次启动下载完成后不再联网；后台服务只监听 `127.0.0.1` 的随机端口，只接受发给 `127.0.0.1` 或 `localhost` 的请求（防 DNS 重绑定），跨域请求只认程序窗口；程序窗口启用了严格的内容安全策略（CSP），只能加载自身文件、缩略图和本机后台服务的数据；照片文件夹只读，导出是复制。
