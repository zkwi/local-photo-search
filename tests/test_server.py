"""后端接口里不依赖模型的行为。"""
import json

import pytest
from fastapi import HTTPException


def test_add_folder_while_model_loads(library, monkeypatch):
    """首次下载模型要好几分钟，这期间加文件夹：先保存，启动流程结束后再扫描（不能直接扫，模型还没有）。"""
    from backend import common, server

    photos, cfg = library
    monkeypatch.setattr(server, "CFG", {**cfg, "photo_dirs": []})
    monkeypatch.setattr(server.S, "phase", "loading")
    monkeypatch.setattr(server.S, "rescan_again", False)
    out = server.update_library(server.LibraryUpdate(photo_dirs=[str(photos)]))
    assert out["rescan"] == "after_load"
    assert server.S.rescan_again
    assert json.loads(common.CONFIG_PATH.read_text(encoding="utf-8"))["photo_dirs"] == out["photo_dirs"]

    monkeypatch.setattr(server.S, "phase", "error")
    monkeypatch.setattr(server.S, "error", {"code": "startup_failed", "detail": "x"})
    with pytest.raises(HTTPException):
        server.update_library(server.LibraryUpdate(photo_dirs=[]))


def test_config_with_bom_or_broken_json(tmp_path, monkeypatch):
    """手改 config.json 的两种常见情况：记事本存成带 BOM 的 UTF-8 要能读；JSON 写坏了要说清是哪个文件。"""
    from backend import common

    cfg = tmp_path / "config.json"
    monkeypatch.setattr(common, "CONFIG_PATH", cfg)
    cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps({"photo_dirs": [], "index_dir": str(tmp_path / "idx")}).encode())
    assert common.load_config()["photo_dirs"] == []
    cfg.write_text('{"photo_dirs": ["D:\\Photos"]}', encoding="utf-8")  # 路径里的 \ 没写成 \\
    with pytest.raises(ValueError, match="config.json is not valid JSON"):
        common.load_config()


def test_only_local_host_headers():
    """防 DNS 重绑定：Host 不是本机地址的请求一律拒绝；接口文档页也不提供。"""
    import asyncio

    import httpx

    from backend import server

    async def get(path, **kw):
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return (await client.get(path, **kw)).status_code

    assert asyncio.run(get("/api/status")) == 200
    assert asyncio.run(get("/api/status", headers={"Host": "photos.attacker.example"})) == 400
    assert asyncio.run(get("/docs")) == 404
