"""后端和桌面外壳只报错误代码，提示语在界面的语言文件里：检查每个代码都有对应文案，免得界面显示成键名。"""
import re

import pytest
from conftest import ROOT
from fastapi import HTTPException

LOCALES = sorted((ROOT / "app" / "src" / "locales").glob("*.js"))


def locale_keys(path):
    return set(re.findall(r'^\s*"([\w.]+)":', path.read_text(encoding="utf-8"), re.M))


def test_every_error_code_has_text():
    server = (ROOT / "backend" / "server.py").read_text(encoding="utf-8")
    common = (ROOT / "backend" / "common.py").read_text(encoding="utf-8")
    shell = (ROOT / "app" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    needed = {f"err.{c}" for c in re.findall(r'HTTPException\(\d+, \{"code": "(\w+)"', server)}
    needed |= {f"err.{c}" for c in re.findall(r'error_info\(e, "(\w+)"\)', server) if c != "scan_failed"}
    needed |= {f"err.{c}" for c in re.findall(r'^\s+code = "(\w+)"', common, re.M)}  # AppError 各子类
    needed |= {"warn.dirs_offline", "warn.scan_failed"}
    needed |= {f"stage.{c}" for c in ("reading_index", "loading_model", "downloading_model")}
    needed |= {f"task.{c}" for c in ("scan", "embed", "group")}
    needed |= {f"backend.{c}" for c in re.findall(r'BackendError::new\("(\w+)"', shell)}
    assert len(needed) > 15  # 正则失效时不要悄悄通过
    assert len(LOCALES) >= 2
    for path in LOCALES:
        missing = needed - locale_keys(path)
        assert not missing, f"{path.name} 缺少 {sorted(missing)}"


def test_api_errors_are_codes(monkeypatch):
    from backend import server
    from backend.common import ModelDownloadError

    monkeypatch.setattr(server.S, "phase", "ready")
    monkeypatch.setattr(server.S, "snap", server.Snapshot(ids=[], meta={}))
    with pytest.raises(HTTPException) as e:
        server.get_snap(need_vectors=True)
    assert e.value.detail == {"code": "no_photos"}
    info = server.error_info(ValueError("x"), "startup_failed")
    assert info == {"code": "startup_failed", "detail": "ValueError: x"}
    info = server.error_info(ModelDownloadError("OSError: offline"), "startup_failed")
    assert info["code"] == "model_download_failed"  # 下载失败有专门的提示（建议换镜像）
