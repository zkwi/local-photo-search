//! 本地照片搜索的桌面外壳：首次运行时用 uv 建好 Python 环境，再启动 Python 后端子进程，把它报告的地址交给界面。
//! 缩略图通过 asset 协议直接读本地文件，不经过 Python，避免和模型加载、搜索抢 GIL。
//! 出错时只报代码（code）和原始信息（detail），提示语由界面按当前语言显示。

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, RunEvent};
use tauri_plugin_window_state::StateFlags;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000; // 不弹黑色控制台

/// 后端子进程及其状态；info、error、setup 由后台线程写入，界面轮询读取
#[derive(Default)]
struct Backend {
    /// 正在运行的子进程：首次准备环境时是 uv，之后是 Python 后端
    child: Mutex<Option<Child>>,
    info: Mutex<Option<BackendInfo>>,
    error: Mutex<Option<BackendError>>,
    /// 首次准备环境期间的进度，准备完为 None
    setup: Mutex<Option<SetupProgress>>,
    /// 每次（重新）启动加一；旧进程退出时代数对不上，说明是主动重启，不当作故障
    generation: Mutex<u64>,
}

#[derive(Clone, Serialize)]
struct BackendInfo {
    url: String,
    /// 缩略图目录：界面用 asset 协议直接读这里的文件
    thumbs: Option<String>,
}

/// 后台服务没起来或退出的原因：code 对应界面文案 backend.<code>，detail 是路径或原始错误
#[derive(Clone, Serialize)]
struct BackendError {
    code: &'static str,
    detail: String,
}

impl BackendError {
    fn new(code: &'static str, detail: impl Into<String>) -> Self {
        Self { code, detail: detail.into() }
    }
}

/// 首次准备环境的进度：正在下载的包（如 "torch (2.4GiB)"）和 uv 输出的最后一行
#[derive(Clone, Default, Serialize)]
struct SetupProgress {
    downloading: Vec<String>,
    line: String,
}

impl SetupProgress {
    /// uv 不接终端时逐行报告：“Downloading torch (2.4GiB)” 记为正在下载，“Downloaded torch” 时去掉
    fn update(&mut self, line: &str) {
        if line.starts_with("warning") {
            return; // 例如装在 D 盘时 uv 提示没法和 C 盘的缓存建硬链接、改为复制：不影响结果，不当进度显示
        }
        if let Some(rest) = line.strip_prefix("Downloading ") {
            self.downloading.push(rest.to_string());
        } else if let Some(name) = line.strip_prefix("Downloaded ") {
            let name = name.split_whitespace().next().unwrap_or(name);
            self.downloading.retain(|d| d.split_whitespace().next() != Some(name));
        }
        self.line = line.to_string();
    }
}

/// 项目根目录：环境变量 PHOTO_SEARCH_ROOT 优先；否则从程序所在位置往上找含 backend/server.py 的目录
/// （整个项目搬走也能找到）。不退回编译时的源码位置：那样会把开发者本机的路径写进发布的程序
fn project_root() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("PHOTO_SEARCH_ROOT") {
        return Some(PathBuf::from(dir));
    }
    let exe = std::env::current_exe().ok()?;
    exe.ancestors().find(|d| d.join("backend").join("server.py").is_file()).map(Path::to_path_buf)
}

/// uv 建的虚拟环境里的 Python
fn venv_python(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    }
}

/// 环境准备完成的标记，内容是 uv.lock 的指纹。首次准备中途被打断（关窗、断网）时没有它，
/// 升级到依赖有变化的新版本时指纹对不上，这两种情况下次打开都会用 uv 补齐
fn setup_marker(root: &Path) -> PathBuf {
    root.join(".venv").join(".setup-done")
}

/// uv.lock 的指纹（64 位 FNV-1a），只用来判断依赖有没有变
fn lock_fingerprint(root: &Path) -> String {
    let bytes = std::fs::read(root.join("uv.lock")).unwrap_or_default();
    let hash = bytes.iter().fold(0xcbf2_9ce4_8422_2325_u64, |h, &b| (h ^ u64::from(b)).wrapping_mul(0x0100_0000_01b3));
    format!("{hash:016x}")
}

fn env_ready(root: &Path) -> bool {
    std::fs::read_to_string(setup_marker(root)).is_ok_and(|s| s.trim() == lock_fingerprint(root))
}

/// 找 uv：先找发布包里自带的，再找 PATH，最后找 uv 安装器的默认位置（刚装完 uv 时资源管理器的 PATH 可能还没更新）
fn find_uv(root: &Path) -> Option<PathBuf> {
    let name = if cfg!(windows) { "uv.exe" } else { "uv" };
    let mut dirs = vec![root.to_path_buf()];
    if let Some(path) = std::env::var_os("PATH") {
        dirs.extend(std::env::split_paths(&path));
    }
    if let Some(home) = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }) {
        let home = PathBuf::from(home);
        dirs.push(home.join(".local").join("bin"));
        dirs.push(home.join(".cargo").join("bin"));
    }
    dirs.into_iter().map(|d| d.join(name)).find(|p| p.is_file())
}

/// 首次运行：按 uv.lock 用 uv 建 Python 环境（下载 Python、PyTorch 等，约 3 GB），进度交给界面显示
fn setup_env(app: &AppHandle, root: &Path) -> Result<(), BackendError> {
    let state = app.state::<Backend>();
    let Some(uv) = find_uv(root) else {
        // 没有 uv，但环境已经在（开发者自己 uv sync 建的）：直接用
        return if venv_python(root).is_file() { Ok(()) } else { Err(BackendError::new("uv_missing", "")) };
    };
    // 先确认能写：解压到 Program Files 这类只读位置时，环境和索引都建不了
    let probe = root.join(".write-test");
    if std::fs::write(&probe, b"").is_err() {
        return Err(BackendError::new("root_not_writable", root.display().to_string()));
    }
    let _ = std::fs::remove_file(&probe);

    let mut cmd = Command::new(&uv);
    // --frozen 按锁定的版本装；--no-dev 不装测试工具；--inexact 不删开发者自己另装的包
    cmd.args(["sync", "--frozen", "--no-dev", "--inexact", "--color", "never"])
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    let mut child = cmd.spawn().map_err(|e| BackendError::new("setup_failed", format!("{}: {e}", uv.display())))?;
    let Some(stderr) = child.stderr.take() else {
        return Err(BackendError::new("setup_failed", "no stderr"));
    };
    *state.setup.lock().unwrap() = Some(SetupProgress::default());
    *state.child.lock().unwrap() = Some(child); // 放进状态里，关窗或重启时能结束它
    // 失败时报 uv 的第一条 “error:” 行（真正的原因），后面跟着的 hint 只是补充；没有就报最后一行
    let (mut first_error, mut last) = (None, String::new());
    for line in BufReader::new(stderr).lines().map_while(Result::ok) {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if first_error.is_none() && line.starts_with("error") {
            first_error = Some(line.to_string());
        }
        last = line.to_string();
        if let Some(progress) = state.setup.lock().unwrap().as_mut() {
            progress.update(line);
        }
    }
    let status = state.child.lock().unwrap().take().map(|mut c| c.wait());
    *state.setup.lock().unwrap() = None;
    match status {
        Some(Ok(s)) if s.success() => {
            let _ = std::fs::write(setup_marker(root), lock_fingerprint(root));
            Ok(())
        }
        _ => Err(BackendError::new("setup_failed", first_error.unwrap_or(last))),
    }
}

/// 准备好环境后启动后端，并一直读它的输出，直到它退出；返回值说明退出原因
fn run_backend(app: &AppHandle, generation: u64) -> BackendError {
    let state = app.state::<Backend>();
    let Some(root) = project_root() else {
        let exe = std::env::current_exe().map(|p| p.display().to_string()).unwrap_or_default();
        return BackendError::new("root_not_found", exe);
    };
    if !env_ready(&root) {
        if let Err(e) = setup_env(app, &root) {
            return e;
        }
    }
    let python = venv_python(&root);
    if !python.is_file() {
        return BackendError::new("venv_missing", python.display().to_string());
    }
    let mut cmd = Command::new(&python);
    // 把本进程 PID 交给后端：本进程退出（包括崩溃）后后端自行退出
    cmd.args(["-X", "utf8", "-m", "backend.server", "--parent-pid"])
        .arg(std::process::id().to_string())
        .current_dir(&root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(e) => return BackendError::new("spawn_failed", format!("{}: {e}", python.display())),
    };
    let (Some(stdout), Some(stderr)) = (child.stdout.take(), child.stderr.take()) else {
        return BackendError::new("spawn_failed", "no stdout");
    };
    *state.child.lock().unwrap() = Some(child);

    // 一直读后端的 stderr（日志也会写到这里，不读会把管道写满卡住后端），只留最后几行：
    // 启动早期出错（例如 config.json 写坏了）时日志还没配置好，这几行是唯一的线索
    let tail = Arc::new(Mutex::new(VecDeque::new()));
    let reader = {
        let tail = Arc::clone(&tail);
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                let mut tail = tail.lock().unwrap();
                if tail.len() == 20 {
                    tail.pop_front();
                }
                tail.push_back(line);
            }
        })
    };

    // 一直读到后端退出：THUMBS 行给出缩略图目录，READY 行给出地址，其余输出丢弃，避免管道写满卡住后端
    let mut thumbs = None;
    let mut ready = false;
    for line in BufReader::new(stdout).lines().map_while(Result::ok) {
        if let Some(dir) = line.strip_prefix("THUMBS ") {
            // 只放行后端声明的这一个目录（不递归），界面不能借 asset 协议读别的文件
            let dir = dir.trim().to_string();
            if app.asset_protocol_scope().allow_directory(&dir, false).is_ok() {
                thumbs = Some(dir);
            }
        } else if let Some(url) = line.strip_prefix("READY ") {
            ready = true;
            if *state.generation.lock().unwrap() == generation {
                *state.info.lock().unwrap() = Some(BackendInfo { url: url.trim().into(), thumbs: thumbs.clone() });
            }
        }
    }
    let _ = reader.join();
    if ready {
        return BackendError::new("backend_exited", "");
    }
    // 没打出 READY 就退出了：报 stderr 最后一条非空行（通常是异常那一行），没有就指向日志
    let last = tail.lock().unwrap().iter().rev().find(|l| !l.trim().is_empty()).cloned();
    BackendError::new("backend_failed", last.unwrap_or_else(|| root.join("index").join("backend.log").display().to_string()))
}

fn spawn_backend(app: &AppHandle) {
    let app = app.clone();
    let generation = *app.state::<Backend>().generation.lock().unwrap();
    std::thread::spawn(move || {
        let reason = run_backend(&app, generation);
        let state = app.state::<Backend>();
        if *state.generation.lock().unwrap() != generation {
            return; // 已被 restart_backend 替换
        }
        *state.info.lock().unwrap() = None;
        *state.error.lock().unwrap() = Some(reason.clone());
        let _ = app.emit("backend-exited", reason);
    });
}

/// 结束子进程。Windows 上 venv 的 python.exe 是转发器，真正的解释器是它的子进程，要结束整棵进程树
fn kill_backend(state: &Backend) {
    if let Some(mut child) = state.child.lock().unwrap().take() {
        #[cfg(windows)]
        {
            let mut kill = Command::new("taskkill");
            kill.args(["/T", "/F", "/PID", &child.id().to_string()]).creation_flags(CREATE_NO_WINDOW);
            let _ = kill.status();
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// 界面轮询调用：后端就绪前返回 None，启动失败或已退出时返回错误
#[tauri::command]
fn backend_url(state: tauri::State<'_, Backend>) -> Result<Option<BackendInfo>, BackendError> {
    if let Some(err) = state.error.lock().unwrap().clone() {
        return Err(err);
    }
    Ok(state.info.lock().unwrap().clone())
}

/// 界面轮询调用：首次准备环境期间返回进度，其余时候返回 None
#[tauri::command]
fn setup_progress(state: tauri::State<'_, Backend>) -> Option<SetupProgress> {
    state.setup.lock().unwrap().clone()
}

/// 后台服务出问题时由界面调用：结束旧进程，重新启动（环境没准备好时会重新准备）
#[tauri::command]
fn restart_backend(app: AppHandle) {
    let state = app.state::<Backend>();
    *state.generation.lock().unwrap() += 1;
    kill_backend(&state);
    *state.info.lock().unwrap() = None;
    *state.error.lock().unwrap() = None;
    *state.setup.lock().unwrap() = None;
    spawn_backend(&app);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // 只允许一个实例：再次打开时把已有窗口调到前台，避免两个后端同时写同一个索引库
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        // 记住窗口大小和位置，但不记全屏：全屏时关掉的话，下次一打开就是全屏，主界面上没有退出全屏的按钮
        .plugin(tauri_plugin_window_state::Builder::default().with_state_flags(StateFlags::all() & !StateFlags::FULLSCREEN).build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        // 全屏时关窗：先退出全屏，窗口状态插件记下的才是平常窗口的大小和位置，而不是整个屏幕
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if window.is_fullscreen().unwrap_or(false) {
                    let _ = window.set_fullscreen(false);
                }
            }
        })
        .manage(Backend::default())
        .setup(|app| {
            spawn_backend(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_url, setup_progress, restart_backend])
        .build(tauri::generate_context!())
        .expect("failed to start Tauri")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                let state = app.state::<Backend>();
                *state.generation.lock().unwrap() += 1; // 正常退出，不再上报“意外退出”
                kill_backend(&state);
            }
        });
}
