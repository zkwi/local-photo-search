"""配置、模型加载与 SQLite 索引的公共部分。"""
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 不给 Hugging Face 发使用统计（要在导入 huggingface_hub 之前设置）；用户自己设了就以用户为准
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# 默认用项目根目录的 config.json；测试时可用环境变量指向另一份配置（例如索引库副本），不碰正在用的索引
CONFIG_PATH = Path(os.environ.get("PHOTO_SEARCH_CONFIG") or ROOT / "config.json")
IMAGE_TOKENS = 280  # 视觉 token 预算：照片用默认 280 足够，调到 1120 编码会慢 5 倍以上

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS photos (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    width     INTEGER,
    height    INTEGER,
    taken_at  TEXT,
    error     TEXT,
    embedding BLOB,           -- float16 × 768，已归一化
    group_id  INTEGER         -- 连拍/重复照片分组，值为组内第一张的 id
);
"""


MODEL_ID = "google/embeddinggemma-2"


class AppError(RuntimeError):
    """启动时可预见的问题：code 对应界面文案 err.<code>（按界面语言显示），异常消息是原始信息，写进日志和详情。"""
    code = "startup_failed"


class ModelDownloadError(AppError):
    """模型下载失败：界面提示改用国内镜像启动或设置 HF_ENDPOINT。"""
    code = "model_download_failed"


class VcRuntimeMissingError(AppError):
    """缺 PyTorch 需要的 Microsoft Visual C++ 运行库（全新安装的 Windows 上可能没有）：界面给出官方下载链接。"""
    code = "vc_runtime_missing"


def register_heif():
    """让 Pillow 能读 iPhone 默认的 HEIC/HEIF 照片；没装 pillow-heif 时跳过。"""
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return False
    register_heif_opener()
    return True


def load_config():
    """读取 config.json；相对路径按项目根目录解析。默认不预设照片文件夹，首次打开在界面里添加。"""
    cfg = {"photo_dirs": [], "index_dir": "index", "model": MODEL_ID}
    if CONFIG_PATH.exists():
        try:
            # utf-8-sig：用记事本另存的 UTF-8 文件开头带 BOM
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except ValueError as e:  # 手改时漏了逗号、路径里的 \ 没写成 \\ 等；这句会出现在界面的错误详情里
            raise ValueError(f"{CONFIG_PATH.name} is not valid JSON: {e}") from e
    cfg["photo_dirs"] = [str((ROOT / d).resolve()) for d in cfg["photo_dirs"]]
    cfg["index_dir"] = str((ROOT / cfg["index_dir"]).resolve())
    Path(cfg["index_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def save_photo_dirs(dirs):
    """把照片文件夹列表写回 config.json，保留其他配置项。"""
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")) if CONFIG_PATH.exists() else {}
    cfg["photo_dirs"] = dirs
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def open_db(index_dir):
    con = sqlite3.connect(Path(index_dir) / "index.db", check_same_thread=False)
    con.executescript(SCHEMA)
    # 旧索引库补列：group_id 是后加的
    if "group_id" not in {r[1] for r in con.execute("PRAGMA table_info(photos)")}:
        con.execute("ALTER TABLE photos ADD COLUMN group_id INTEGER")
        con.commit()
    return con


def import_torch():
    """导入 torch。全新安装的 Windows 可能缺 Microsoft Visual C++ 运行库，torch 的 DLL 会加载失败（WinError 126），
    这时换成界面能看懂的错误；其他原因原样抛出。"""
    try:
        import torch
    except OSError as e:
        if os.name == "nt" and not _dll_loads("msvcp140.dll"):
            raise VcRuntimeMissingError(f"{type(e).__name__}: {e}") from e
        raise
    return torch


def _dll_loads(name):
    import ctypes

    try:
        ctypes.WinDLL(name)
        return True
    except OSError:
        return False


def pick_device(torch):
    """有能用的 NVIDIA 显卡就用 GPU，否则用 CPU。
    锁定的 PyTorch（CUDA 13 版）只带 Turing（GTX 16 / RTX 20）及更新显卡的内核：
    更老的显卡 is_available() 也可能为 True，但一算就报 “no kernel image is available”，所以先查算力再试算一次。"""
    if not torch.cuda.is_available():
        return "cpu"
    try:
        capability = torch.cuda.get_device_capability()
        if capability < (7, 5):
            raise RuntimeError(f"compute capability {capability} is older than 7.5")
        torch.zeros(1, device="cuda").add_(1).item()
        return "cuda"
    except Exception as e:
        logging.getLogger("model").warning("显卡用不了，改用 CPU：%s", e)
        return "cpu"


def load_model(model_id):
    """只加载文本和视觉编码器（省掉 300M 的音频编码器）；有能用的 GPU 用 bf16，否则 CPU fp32。"""
    t0 = time.perf_counter()
    torch = import_torch()
    from sentence_transformers import SentenceTransformer

    logging.getLogger("model").info("导入 torch 与 sentence-transformers %.1fs", time.perf_counter() - t0)
    device = pick_device(torch)
    kwargs = {
        "device": device,
        # 该模型不能用 float16，会静默产生 NaN
        "model_kwargs": {"torch_dtype": torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported()
                         else torch.float32},
        "config_kwargs": {"audio_config": None},
    }
    try:
        return SentenceTransformer(model_id, local_files_only=True, **kwargs)
    except OSError:
        pass
    try:
        return SentenceTransformer(model_id, **kwargs)  # 本机还没缓存时联网下载一次（约 1.5 GB）
    except Exception as e:
        first_line = (str(e).splitlines() or [""])[0]
        raise ModelDownloadError(f"{type(e).__name__}: {first_line}") from e


def model_cached(model_id):
    """模型权重是否已在本机缓存（只用于提示“首次运行要下载”，判断不了时当作已缓存）。"""
    try:
        from huggingface_hub import try_to_load_from_cache

        return isinstance(try_to_load_from_cache(model_id, "model.safetensors"), str)
    except Exception:
        return True
