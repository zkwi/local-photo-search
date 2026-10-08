"""后端接口里不依赖真模型的行为。"""
import asyncio
import io
import json
from collections import OrderedDict

import httpx
import numpy as np
import pytest
import torch
from fastapi import HTTPException
from PIL import Image


def call(method, path, **kw):
    """不经网络直接调用接口，返回 (状态码, JSON)。"""
    from backend import server

    async def go():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            res = await client.request(method, path, **kw)
            return res.status_code, res.json()

    return asyncio.run(go())


class StubModel:
    """代替真模型：任何图片都编码成同一个向量。"""
    device = torch.device("cpu")

    def encode(self, items, **kw):
        return torch.tensor([[1.0, 0.0]] * len(items))


@pytest.fixture
def ready(monkeypatch):
    """模型已就绪、库里有两张照片的后端。"""
    from backend import server

    meta = {i: {"path": f"/p/{i}.jpg", "name": f"{i}.jpg", "bytes": 1, "width": 1, "height": 1, "taken_at": None,
                "kind": "photo"} for i in (1, 2)}
    snap = server.Snapshot(ids=[1, 2], meta=meta, pos={1: 0, 2: 1}, group_of={1: 1, 2: 2}, members={1: [1], 2: [2]})
    snap.mat = torch.tensor([[0.6, 0.8], [1.0, 0.0]])
    snap.is_shot = torch.tensor([False, False])
    monkeypatch.setattr(server.S, "phase", "ready")
    monkeypatch.setattr(server.S, "snap", snap)
    monkeypatch.setattr(server.S, "model", StubModel())
    monkeypatch.setattr(server.S, "queries", OrderedDict())
    return server


def png():
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (90, 140, 200)).save(buf, "PNG")
    return buf.getvalue()


def test_search_by_image(ready, tmp_path, monkeypatch):
    status, q = call("POST", "/api/image-query", content=png(), headers={"Content-Type": "image/png"})
    assert status == 200 and q["preview"].startswith("data:image/jpeg;base64,")
    assert (q["width"], q["height"]) == (32, 24)
    status, page = call("GET", f"/api/image-search/{q['qid']}")
    assert [r["id"] for r in page["results"]] == [2, 1] and page["results"][0]["score"] == 1.0  # 最像的在前

    (tmp_path / "q.png").write_bytes(png())
    status, again = call("POST", "/api/image-query", json={"path": str(tmp_path / "q.png")})  # 拖进窗口的文件按路径读
    assert status == 200 and again["qid"] == q["qid"]  # 同一张图编号相同
    missing = str(tmp_path / "nope.png")
    assert call("POST", "/api/image-query", json={"path": missing}) == (404, {"detail": {"code": "file_not_found",
                                                                                         "path": missing}})
    bad = call("POST", "/api/image-query", content=b"not an image", headers={"Content-Type": "image/png"})
    assert bad == (400, {"detail": {"code": "image_unreadable"}})
    # 表单、纯文本跨域发送时不用预检，任何网页都能直接发：一律不收
    assert call("POST", "/api/image-query", content=png(), headers={"Content-Type": "text/plain"})[0] == 415
    assert call("GET", "/api/image-search/0123456789abcdef")[1]["detail"]["code"] == "query_expired"

    monkeypatch.setattr(ready, "QUERY_MAX_MB", 0)
    big = call("POST", "/api/image-query", content=png(), headers={"Content-Type": "image/png"})
    assert big == (413, {"detail": {"code": "image_too_large", "mb": 0}})
    monkeypatch.setattr(ready.S, "phase", "loading")  # 模型还没就绪
    assert call("POST", "/api/image-query", json={"path": missing})[1]["detail"]["code"] == "model_loading"


def test_image_search_marks_the_same_photo(ready):
    """以图搜图的结果里，dHash 也对得上的标“同一张”；只是相似（dHash 差得多）的不标。"""
    from backend.indexer import THUMB_SIDE, dhash, open_image

    noise = (np.random.default_rng(3).random((24, 32, 3)) * 255).astype("uint8")  # 有明暗变化，dHash 才有区分度
    buf = io.BytesIO()
    Image.fromarray(noise).save(buf, "PNG")
    thumb = open_image(io.BytesIO(buf.getvalue()))[0]
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
    h = dhash(thumb) & (2**64 - 1)
    ready.S.snap.hashes = np.array([h ^ 0xFFFF, h], dtype=np.uint64)  # 第 1 张差 16 位，第 2 张一样
    ready.S.snap.hashed = np.array([True, True])
    q = call("POST", "/api/image-query", content=buf.getvalue(), headers={"Content-Type": "image/png"})[1]
    page = call("GET", f"/api/image-search/{q['qid']}")[1]
    assert [(r["id"], r.get("same", False)) for r in page["results"]] == [(2, True), (1, False)]


def test_folders_tells_dropped_folders_from_files(tmp_path):
    """拖进窗口的东西里只有文件夹会被问要不要加进图库。"""
    (tmp_path / "photos").mkdir()
    (tmp_path / "a.pdf").write_bytes(b"x")
    paths = [str(tmp_path / "photos"), str(tmp_path / "a.pdf"), "relative\\dir", str(tmp_path / "missing")]
    assert call("POST", "/api/folders", json={"paths": paths}) == (200, {"folders": [str(tmp_path / "photos")]})


def test_missing_ignores_offline_folders(ready, tmp_path, monkeypatch):
    """整理重复照片时删掉的要标出来；整个文件夹访问不到（NAS 断开）时不能当成删除。"""
    online, offline = tmp_path / "photos", tmp_path / "nas"
    online.mkdir()
    (online / "kept.jpg").write_bytes(b"x")
    meta = {1: {"path": str(online / "kept.jpg")}, 2: {"path": str(online / "deleted.jpg")},
            3: {"path": str(offline / "far.jpg")}}
    monkeypatch.setattr(ready.S, "snap", ready.Snapshot(ids=[1, 2, 3], meta=meta))
    monkeypatch.setitem(ready.CFG, "photo_dirs", [str(online), str(offline)])
    assert call("POST", "/api/missing", json={"ids": [1, 2, 3, 99]}) == (200, {"missing": [2]})


def test_duplicates_endpoint(ready, monkeypatch):
    """按组返回，每组第一张带组标题；一组不拆到两页。"""
    from test_duplicates import LIBRARY, snapshot

    snap = snapshot(LIBRARY)
    snap.time_of = {r[0]: None for r in LIBRARY}
    monkeypatch.setattr(ready.S, "snap", snap)
    status, page = call("GET", "/api/duplicates", params={"limit": 1})
    assert status == 200 and page["has_more"] and (page["groups"], page["total"]) == (3, 6)
    assert [(r["id"], r["dup"]["role"]) for r in page["results"]] == [(1, "keep"), (2, "identical")]
    assert page["results"][0]["dup"]["head"] == {"cat": "identical", "cats": ["identical"], "n": 2,
                                                 "extra": 5_000_000, "taken": "2025-05-01 10:00:00"}
    assert "head" not in page["results"][1]["dup"]
    _, page = call("GET", "/api/duplicates", params={"cat": "similar"})
    assert [r["id"] for r in page["results"]] == [8, 7] and page["counts"]["similar"] == 1


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
    from backend import server

    async def get(path, **kw):
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return (await client.get(path, **kw)).status_code

    assert asyncio.run(get("/api/status")) == 200
    assert asyncio.run(get("/api/status", headers={"Host": "photos.attacker.example"})) == 400
    assert asyncio.run(get("/docs")) == 404
