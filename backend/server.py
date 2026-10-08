"""照片搜索后端：FastAPI 服务，供 Tauri 界面调用，也可直接用浏览器打开调试。

启动：python -m backend.server [--port 0] [--parent-pid PID]
端口默认由系统分配（避开 Windows 动态保留端口段），监听后在 stdout 打印 "READY <地址>"。

启动顺序为了让界面尽快可用：先读 SQLite（约 1 秒，可浏览照片）→ 扫描新照片与加载模型并行
→ 模型就绪即可搜索 → 新照片在后台编码，每完成约 1000 张刷新一次。torch 等重依赖延后导入。
"""
import argparse
import base64
import hashlib
import io
import logging
import os
import socket
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from .common import IMAGE_TOKENS, ROOT, AppError, load_config, load_model, model_cached, open_db, save_photo_dirs
from .indexer import embed_pending, hash_pending, is_under, load_index, needs_regroup, open_image, sync_files, timestamp

CFG = load_config()
log = logging.getLogger("server")
PAGE_MAX = 500
REFRESH_EVERY = 1000  # 建索引时每新增这么多张刷新一次快照，边建边可搜

# 不提供 /docs 等接口文档页：用不上，打开时还会从外部 CDN 加载脚本
app = FastAPI(title="Local Photo Search", docs_url=None, redoc_url=None, openapi_url=None)
# 只放行 Tauri 窗口的来源，避免普通网页跨域读取本机照片信息；写操作只收 JSON 或图片字节，跨域时必须先过预检
app.add_middleware(CORSMiddleware, allow_origins=["http://tauri.localhost", "https://tauri.localhost", "tauri://localhost"],
                   allow_methods=["GET", "POST", "PUT"], allow_headers=["*"])
# 只认发给本机地址的请求：防 DNS 重绑定（恶意网页把自己的域名解析到 127.0.0.1，再以“同源”身份读照片）
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@dataclass
class Snapshot:
    """某一时刻的索引；整体替换，请求开头取一次后只用这一份，不会读到更新一半的数据。"""
    ids: list
    meta: dict
    pos: dict = field(default_factory=dict)  # id → 行号
    group_of: dict = field(default_factory=dict)  # id → 组 id（连拍/重复照片）
    members: dict = field(default_factory=dict)  # 组 id → 组内照片 id（按拍摄时间）
    folded: dict = field(default_factory=dict)  # 范围 → 按拍摄时间倒序、每组一张的 id 列表
    counts: dict = field(default_factory=dict)  # 范围 → 照片张数（不合并）
    time_of: dict = field(default_factory=dict)  # id → 拍摄时间戳（未知为 None）
    years: list = field(default_factory=list)  # [[年份, 张数], ...]，新的在前
    hashes: object = None  # 每行的 dHash（numpy uint64），没算过的为 0、hashed 为 False
    hashed: object = None
    mat: object = None  # 以下为 torch 张量，模型加载后才有
    bias: object = None  # 每张图对通用文本的基线分，文本搜索时扣掉
    is_shot: object = None
    times: object = None  # 拍摄时间戳，未知为 NaN
    dups: object = None  # 重复照片组，第一次打开“重复照片”时才算，见 duplicates.py


class State:
    # 交给界面的状态都用代码（stage、error.code、warning.code），界面按当前语言显示对应文字
    phase = "starting"  # starting → loading → ready / error
    stage = "reading_index"  # 启动阶段：reading_index / loading_model / downloading_model
    download = None  # 首次下载模型时已下载的字节数，界面据此显示进度
    error = None  # 启动失败时为 {"code", "detail"}
    task = None  # 后台整理索引时为 {"stage": scan/hash/embed/group, "done": n, "total": m}
    warning = None  # 例如照片目录无法访问：{"code": "dirs_offline", "dirs": [...]}
    last_scan = None
    model = None
    bg = None  # 通用描述的文本向量（去偏用）
    snap: Snapshot | None = None
    lock = threading.Lock()  # 模型编码串行执行
    scan_lock = threading.Lock()  # 同一时间只跑一次扫描/编码
    dup_lock = threading.Lock()  # 同一份快照的重复照片组只算一次
    rescan_again = False  # 扫描进行中又改了文件夹：结束后再扫一次
    queries = OrderedDict()  # 以图搜图：查询编号 → 图片向量，只留最近几个，翻页时用
    query_lock = threading.Lock()


S = State()


def error_info(e, code):
    """异常 → 交给界面的 {code, detail}：code 决定显示哪句提示，detail 是原始错误，方便排查。"""
    return {"code": e.code if isinstance(e, AppError) else code, "detail": f"{type(e).__name__}: {e}"}


def watch_download(model_id):
    """首次下载模型（约 1.5 GB）时每秒统计一次缓存目录里已有的字节数。下载由 huggingface_hub 完成，它不报进度给我们。"""
    from huggingface_hub import constants

    folder = Path(constants.HF_HUB_CACHE) / f"models--{model_id.replace('/', '--')}"
    while S.phase == "loading" and S.stage == "downloading_model":
        total = 0
        try:
            for f in folder.rglob("*"):
                if f.is_file() and not f.is_symlink():  # 能建符号链接的系统上 snapshots 里是链接，别重复计
                    total += f.stat().st_size
        except OSError:
            pass  # 下载中的文件随时会被改名或移走，下一秒再算
        S.download = total
        time.sleep(1)


def build_snapshot(with_vectors):
    ids, mat, meta, groups, hashes = load_index(CFG["index_dir"])
    taken = lambda i: meta[i]["taken_at"] or ""  # noqa: E731
    group_of = dict(zip(ids, groups, strict=True))
    members = {}
    for pid in sorted(ids, key=taken):
        members.setdefault(group_of[pid], []).append(pid)
    recent = sorted(ids, key=taken, reverse=True)
    folded, counts = {}, {}
    for kind in ("all", "photo", "screenshot"):
        allowed = [i for i in recent if kind == "all" or meta[i]["kind"] == kind]
        seen, folded[kind] = set(), []
        for i in allowed:  # 每组只留最新的一张
            if group_of[i] not in seen:
                seen.add(group_of[i])
                folded[kind].append(i)
        counts[kind] = len(allowed)
    time_of = {i: timestamp(meta[i]["taken_at"]) for i in ids}
    years = {}
    for i in ids:
        if meta[i]["taken_at"]:
            years[meta[i]["taken_at"][:4]] = years.get(meta[i]["taken_at"][:4], 0) + 1
    snap = Snapshot(ids=ids, meta=meta, pos={pid: i for i, pid in enumerate(ids)}, group_of=group_of,
                    members=members, folded=folded, counts=counts, time_of=time_of,
                    years=[[int(y), n] for y, n in sorted(years.items(), reverse=True)],
                    hashes=np.array([h or 0 for h in hashes], dtype=np.int64).view(np.uint64),
                    hashed=np.array([h is not None for h in hashes], dtype=bool))
    if with_vectors and mat is not None:
        import torch

        from .calibration import text_bias

        dev = S.model.device
        snap.mat = torch.from_numpy(mat.copy()).to(dev, torch.float16 if dev.type == "cuda" else torch.float32)
        snap.bias = text_bias(S.bg, snap.mat)
        snap.is_shot = torch.tensor([meta[i]["kind"] == "screenshot" for i in ids], device=dev)
        snap.times = torch.tensor([float("nan") if time_of[i] is None else time_of[i] for i in ids],
                                  dtype=torch.float64, device=dev)
    return snap


def in_range(t, start, end):
    """拍摄时间是否落在 [start, end)；没有时间筛选时都算，时间未知的照片在有筛选时不算。"""
    if start is None and end is None:
        return True
    return t is not None and (start is None or t >= start) and (end is None or t < end)


def scan(con):
    """扫描照片文件夹，把增删改同步到索引库；删掉的照片立刻从浏览里去掉。"""
    S.task = {"stage": "scan"}
    sync = sync_files(con, CFG)
    log.info("扫描完成：%s", sync)
    S.warning = {"code": "dirs_offline", "dirs": sync["offline"]} if sync["offline"] else None
    if sync["removed"]:
        S.snap = build_snapshot(with_vectors=S.snap is not None and S.snap.mat is not None)
    return sync


def hash_missing(con):
    """给 0.1.0 建的旧索引补算 dHash（“重复照片”靠它认出同一张照片的副本）。只读缩略图，不需要模型。"""
    def progress(done, total):
        S.task = {"stage": "hash", "done": done, "total": total}

    n = hash_pending(con, CFG, progress)
    if n:
        log.info("补算 dHash：%d 张", n)
        if S.snap is not None and S.snap.mat is not None:
            S.snap = build_snapshot(with_vectors=True)


def index_new(con, sync):
    """编码新照片（每完成约 1000 张刷新一次，边建边可搜），再重新分组。需要模型已加载。"""
    if sync["pending"]:
        last = [0]

        def progress(done, total):
            S.task = {"stage": "embed", "done": done, "total": total}
            if done - last[0] >= REFRESH_EVERY:
                last[0] = done
                S.snap = build_snapshot(with_vectors=True)

        stats = embed_pending(con, S.model, CFG, progress, lock=S.lock)
        S.snap = build_snapshot(with_vectors=True)
        log.info("新照片编码完成：%s", stats)
    if needs_regroup(con):  # 有新照片或旧索引还没分过组
        from .grouping import regroup

        S.task = {"stage": "group"}
        snap = S.snap
        n = regroup(con, snap.ids, snap.mat, [timestamp(snap.meta[i]["taken_at"]) for i in snap.ids])
        S.snap = build_snapshot(with_vectors=True)
        log.info("连拍/重复照片分组完成：%d 组", n)
    S.last_scan = time.strftime("%Y-%m-%d %H:%M:%S")


def boot():
    S.scan_lock.acquire()
    try:
        S.snap = build_snapshot(with_vectors=False)
        S.phase = "loading"
        S.stage = "loading_model" if model_cached(CFG["model"]) else "downloading_model"
        if S.stage == "downloading_model":
            threading.Thread(target=watch_download, args=(CFG["model"],), daemon=True).start()
        loaded = {}

        def load():
            try:
                t0 = time.perf_counter()
                loaded["model"] = load_model(CFG["model"])
                log.info("模型加载 %.1fs，设备 %s", time.perf_counter() - t0, loaded["model"].device)
            except Exception as e:
                loaded["error"] = e

        loader = threading.Thread(target=load, daemon=True)
        loader.start()
        con = open_db(CFG["index_dir"])
        sync = scan(con)  # 扫描磁盘、补算 dHash 与加载模型同时进行
        hash_missing(con)
        loader.join()
        if "error" in loaded:
            raise loaded["error"]
        from .calibration import background_embeddings

        S.model = loaded["model"]
        S.bg = background_embeddings(S.model)  # 顺带完成 GPU 预热
        S.snap = build_snapshot(with_vectors=True)
        S.phase, S.stage = "ready", None
        log.info("就绪：%d 张照片", len(S.snap.ids))
        index_new(con, sync)
        con.close()
    except Exception as e:
        log.exception("启动失败")
        S.phase, S.error = "error", error_info(e, "startup_failed")
    finally:
        scan_finished()


def scan_finished():
    S.task = None
    S.scan_lock.release()
    if S.rescan_again and S.phase == "ready":
        S.rescan_again = False
        rescan()


def rescan():
    """后台重新扫描一次。已有扫描在跑时记下来，等它结束后再扫；返回 "started" 或 "queued"。"""
    if not S.scan_lock.acquire(blocking=False):
        S.rescan_again = True
        return "queued"

    def run():
        try:
            con = open_db(CFG["index_dir"])
            sync = scan(con)
            hash_missing(con)
            index_new(con, sync)
            con.close()
        except Exception as e:
            log.exception("扫描失败")
            S.warning = error_info(e, "scan_failed")
        finally:
            scan_finished()

    threading.Thread(target=run, daemon=True).start()
    return "started"


def get_snap(need_vectors):
    snap = S.snap
    if S.phase == "error":
        raise HTTPException(500, S.error)
    if snap is None:
        raise HTTPException(503, {"code": "starting"})
    if need_vectors:
        if not snap.ids:
            raise HTTPException(404, {"code": "no_photos"})
        if snap.mat is None:
            raise HTTPException(503, {"code": "model_loading"})
    return snap


def item(snap, pid, score=None):
    out = {"id": pid, **snap.meta[pid], "count": len(snap.members[snap.group_of[pid]])}
    if score is not None:
        out["score"] = round(score, 4)
    return out


def page_by_score(snap, scores, offset, limit, kind, exclude=None, start=None, end=None):
    """按分数排序后每组只取得分最高的一张，返回 (这一页, 后面是否还有)。exclude 所在的整组不出现。"""
    import torch

    if kind != "all":
        scores = scores.masked_fill(snap.is_shot if kind == "photo" else ~snap.is_shot, float("-inf"))
    if start is not None or end is not None:
        ok = ~torch.isnan(snap.times)  # 有时间筛选时，拍摄时间未知的照片不算
        if start is not None:
            ok &= snap.times >= start
        if end is not None:
            ok &= snap.times < end
        scores = scores.masked_fill(~ok, float("-inf"))
    if exclude is not None:
        scores[[snap.pos[m] for m in snap.members[snap.group_of[exclude]]]] = float("-inf")
    valid = int(torch.isfinite(scores).sum())
    need = offset + limit + 1  # 多取一组，用来判断后面还有没有
    k = min(valid, need * 3 + 64)
    while True:
        picked, seen = [], set()
        if k > 0:
            vals, idx = torch.topk(scores, k)
            for v, i in zip(vals.tolist(), idx.tolist(), strict=True):
                pid = snap.ids[i]
                if snap.group_of[pid] not in seen:
                    seen.add(snap.group_of[pid])
                    picked.append((pid, v))
                    if len(picked) == need:
                        break
        if len(picked) == need or k >= valid:
            break
        k = min(valid, k * 4)  # 同组照片太多、凑不满一页时多取一些
    return [item(snap, pid, v) for pid, v in picked[offset:offset + limit]], len(picked) > offset + limit


@lru_cache(maxsize=64)
def query_vector(q):
    """翻页时同一查询不必重复编码。"""
    with S.lock:
        return S.model.encode(q, prompt_name="SearchQuery", normalize_embeddings=True,
                              convert_to_tensor=True, show_progress_bar=False)


KIND = Query("all", pattern="^(all|photo|screenshot)$")
OFFSET = Query(0, ge=0)
LIMIT = Query(120, ge=1, le=PAGE_MAX)


@app.get("/api/status")
def status():
    snap = S.snap
    return {"phase": S.phase, "stage": S.stage, "download": S.download, "error": S.error, "task": S.task,
            "warning": S.warning,
            "browsable": snap is not None, "searchable": S.phase == "ready" and snap is not None,
            "count": len(snap.ids) if snap else 0, "screenshots": snap.counts["screenshot"] if snap else 0,
            "years": snap.years if snap else [], "device": str(S.model.device) if S.model else None}


@app.get("/api/recent")
def recent(offset: int = OFFSET, limit: int = LIMIT, kind: str = KIND, start: float | None = None,
           end: float | None = None):
    snap = get_snap(need_vectors=False)
    ids = snap.folded[kind]
    photos = snap.counts[kind]
    if start is not None or end is not None:
        ids = [i for i in ids if in_range(snap.time_of[i], start, end)]
        photos = sum(1 for i in snap.ids if (kind == "all" or snap.meta[i]["kind"] == kind)
                     and in_range(snap.time_of[i], start, end))
    return {"results": [item(snap, i) for i in ids[offset:offset + limit]], "has_more": offset + limit < len(ids),
            "total": len(ids), "photos": photos}


@app.get("/api/group/{pid}")
def group(pid: int):
    """一组连拍/重复照片的全部成员，按拍摄时间。"""
    snap = get_snap(need_vectors=False)
    if pid not in snap.group_of:
        raise HTTPException(404, {"code": "photo_not_found"})
    return {"results": [item(snap, i) for i in snap.members[snap.group_of[pid]]]}


@app.get("/api/search")
def search(q: str = Query(..., min_length=1, max_length=200), offset: int = OFFSET, limit: int = LIMIT,
           kind: str = KIND, start: float | None = None, end: float | None = None):
    snap = get_snap(need_vectors=True)
    t0 = time.perf_counter()
    from .calibration import penalize_screenshots

    vec = query_vector(q.strip())
    scores = penalize_screenshots((snap.mat @ vec.to(snap.mat.device, snap.mat.dtype)).float() - snap.bias,
                                  snap.is_shot)
    hits, more = page_by_score(snap, scores, offset, limit, kind, start=start, end=end)
    return {"results": hits, "has_more": more, "took_ms": round((time.perf_counter() - t0) * 1000)}


@app.get("/api/similar/{pid}")
def similar(pid: int, offset: int = OFFSET, limit: int = LIMIT, kind: str = KIND, start: float | None = None,
            end: float | None = None):
    snap = get_snap(need_vectors=True)
    if pid not in snap.pos:
        raise HTTPException(404, {"code": "photo_not_found"})
    t0 = time.perf_counter()
    scores = (snap.mat @ snap.mat[snap.pos[pid]]).float()
    hits, more = page_by_score(snap, scores, offset, limit, kind, exclude=pid, start=start, end=end)
    return {"results": hits, "has_more": more, "took_ms": round((time.perf_counter() - t0) * 1000)}


QUERY_MAX_MB = 50  # 查询图片的大小上限：手机照片一般不到 20 MB
QUERY_CACHE = 32  # 记住最近这么多张查询图片的向量


def too_large():
    return HTTPException(413, {"code": "image_too_large", "mb": QUERY_MAX_MB})


async def read_body(request):
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > QUERY_MAX_MB * 2**20:
        raise too_large()
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > QUERY_MAX_MB * 2**20:
            raise too_large()
    return bytes(data)


def read_query_file(raw):
    """拖进窗口或在对话框里选的图片按路径读：界面里的脚本读不了任意本机文件。"""
    path = Path(raw)
    try:
        if not raw or not path.is_absolute() or not path.is_file():
            raise FileNotFoundError(raw)
        if path.stat().st_size > QUERY_MAX_MB * 2**20:
            raise too_large()
        return path.read_bytes()
    except OSError as e:
        raise HTTPException(404, {"code": "file_not_found", "path": raw}) from e


def encode_query(data):
    """解码查询图片并编码，返回 (查询编号, {向量, 预览小图, 宽, 高})。字节相同的图编号相同，不重复编码。"""
    qid = hashlib.sha1(data).hexdigest()[:16]
    with S.query_lock:
        if qid in S.queries:
            S.queries.move_to_end(qid)
            return qid, S.queries[qid]
    try:
        im, w, h, _ = open_image(io.BytesIO(data))
    except Exception as e:  # 不是图片、文件损坏、像素多得离谱（Pillow 的解压炸弹保护）等
        raise HTTPException(400, {"code": "image_unreadable"}) from e
    with S.lock:
        vec = S.model.encode([{"image": im}], normalize_embeddings=True, convert_to_tensor=True,
                             show_progress_bar=False, processing_kwargs={"image": {"max_soft_tokens": IMAGE_TOKENS}})[0]
    small = im.copy()
    small.thumbnail((160, 160))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=80)
    entry = {"vec": vec, "width": w, "height": h,
             "preview": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}
    with S.query_lock:
        S.queries[qid] = entry
        while len(S.queries) > QUERY_CACHE:
            S.queries.popitem(last=False)
    return qid, entry


@app.post("/api/image-query")
async def image_query(request: Request):
    """以图搜图第一步：收下图片并编码，返回查询编号（翻页时用）和预览小图。
    图片可以是请求体里的原始字节（Content-Type 为 image/* 或 application/octet-stream：粘贴的、浏览器里选的），
    也可以是 JSON {"path": 本机路径}（拖进窗口、在对话框里选的）。不收表单和纯文本：
    那几种类型跨域发送时不用预检，任何网页都能直接发过来。"""
    if S.phase == "error":
        raise HTTPException(500, S.error)
    if S.phase != "ready":  # 模型对象有了但还在预热时也不接，免得和启动流程同时用模型
        raise HTTPException(503, {"code": "model_loading"})
    t0 = time.perf_counter()
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "application/json":
        try:
            raw = (await request.json()).get("path")
        except (ValueError, AttributeError) as e:
            raise HTTPException(400, {"code": "invalid_request"}) from e
        data = await run_in_threadpool(read_query_file, str(raw or "").strip())
    elif ctype.startswith("image/") or ctype == "application/octet-stream":
        data = await read_body(request)
    else:
        raise HTTPException(415, {"code": "invalid_request"})
    qid, q = await run_in_threadpool(encode_query, data)
    return {"qid": qid, "preview": q["preview"], "width": q["width"], "height": q["height"],
            "took_ms": round((time.perf_counter() - t0) * 1000)}


@app.get("/api/image-search/{qid}")
def image_search(qid: str, offset: int = OFFSET, limit: int = LIMIT, kind: str = KIND, start: float | None = None,
                 end: float | None = None):
    """以图搜图第二步：按和查询图片的相似度排序。库里有这张图（或它的副本）时会排在最前面。"""
    snap = get_snap(need_vectors=True)
    with S.query_lock:
        q = S.queries.get(qid)
    if q is None:  # 后台服务重启过，或之后又搜过很多张图：界面会重新上传
        raise HTTPException(404, {"code": "query_expired"})
    t0 = time.perf_counter()
    scores = (snap.mat @ q["vec"].to(snap.mat.device, snap.mat.dtype)).float()
    hits, more = page_by_score(snap, scores, offset, limit, kind, start=start, end=end)
    return {"results": hits, "has_more": more, "took_ms": round((time.perf_counter() - t0) * 1000)}


DUP_CAT = Query("all", pattern="^(all|identical|near|similar)$")


@app.get("/api/duplicates")
def duplicates(offset: int = OFFSET, limit: int = LIMIT, kind: str = KIND, cat: str = DUP_CAT,
               start: float | None = None, end: float | None = None):
    """重复照片，按组列出（一组不拆到两页），每张标上建议保留 / 完全相同 / 几乎相同 / 相似。只读，不删任何照片。"""
    from .duplicates import CATEGORIES, find_duplicates, page_duplicates

    snap = get_snap(need_vectors=True)
    t0 = time.perf_counter()
    with S.dup_lock:
        if snap.dups is None:
            snap.dups = find_duplicates(snap)
            log.info("重复照片：%d 组，用时 %.1fs", len(snap.dups), time.perf_counter() - t0)

    def match(pid):
        return (kind == "all" or snap.meta[pid]["kind"] == kind) and in_range(snap.time_of[pid], start, end)

    page = page_duplicates(snap.dups, match, cat, offset, limit)
    results = []
    for g in page["groups"]:
        head = {"cat": g["cat"], "cats": [c for c in CATEGORIES if c in g["cats"]], "n": len(g["ids"]),
                "extra": g["extra"], "taken": g["taken"]}
        for k, pid in enumerate(g["ids"]):
            r = item(snap, pid)
            r["dup"] = {"g": g["ids"][0], "role": g["roles"][pid], **({"head": head} if k == 0 else {})}
            results.append(r)
    return {"results": results, "has_more": page["has_more"], "total": page["photos"], "groups": page["count"],
            "extra": page["extra"], "counts": page["counts"], "took_ms": round((time.perf_counter() - t0) * 1000)}


@app.get("/api/library")
def library():
    """图库设置面板：照片文件夹、各自张数与是否可访问、索引统计。"""
    snap = S.snap
    paths = [m["path"] for m in snap.meta.values()] if snap else []
    dirs = [{"path": d, "photos": sum(is_under(p, d) for p in paths), "online": os.path.isdir(d)}
            for d in CFG["photo_dirs"]]
    index_dir = Path(CFG["index_dir"])
    try:
        with os.scandir(index_dir / "thumbs") as it:
            thumbs_bytes = sum(e.stat().st_size for e in it if e.is_file())
    except OSError:
        thumbs_bytes = 0
    return {"dirs": dirs, "index_dir": str(index_dir), "model": CFG["model"],
            "device": str(S.model.device) if S.model else None,
            "count": len(paths), "screenshots": snap.counts["screenshot"] if snap else 0,
            "groups": sum(len(g) > 1 for g in snap.members.values()) if snap else 0,
            "index_bytes": (index_dir / "index.db").stat().st_size + thumbs_bytes,
            "last_scan": S.last_scan, "busy": S.scan_lock.locked()}


class LibraryUpdate(BaseModel):
    photo_dirs: list[str]


@app.put("/api/library")
def update_library(body: LibraryUpdate):
    """修改照片文件夹并重新扫描。新加的文件夹必须存在；已有的即使暂时无法访问也保留。
    首次下载模型要好几分钟，这期间也可以先加文件夹：启动流程结束后自动扫描。"""
    if S.phase == "error":
        raise HTTPException(500, S.error)
    index_dir = Path(CFG["index_dir"])
    dirs = []
    for raw in body.photo_dirs:
        raw = raw.strip()
        if raw in CFG["photo_dirs"]:
            path = raw
        else:
            p = Path(raw)
            if not p.is_absolute() or not p.is_dir():
                raise HTTPException(400, {"code": "folder_not_found", "path": raw})
            p = p.resolve()
            if p == index_dir or index_dir.is_relative_to(p) or p.is_relative_to(index_dir):
                raise HTTPException(400, {"code": "folder_contains_index"})
            path = str(p)
        if path not in dirs:
            dirs.append(path)
    save_photo_dirs(dirs)
    CFG["photo_dirs"] = dirs
    log.info("照片文件夹改为：%s", dirs)
    if S.phase != "ready":
        # 启动流程（含模型加载）持有扫描锁，结束时会看到这个标记并重新扫描；这里不能直接扫，模型还没有
        S.rescan_again = True
        return {"photo_dirs": dirs, "rescan": "after_load"}
    return {"photo_dirs": dirs, "rescan": rescan()}


class IdList(BaseModel):
    ids: list[int] = Field(max_length=5000)


@app.post("/api/missing")
def missing(body: IdList):
    """这些照片里哪些已经被删掉了（用户整理重复照片时在资源管理器里删的），界面据此标出“已删除”。
    所在的照片文件夹整个访问不到（NAS 断开等）时不算删除。"""
    snap = get_snap(need_vectors=False)
    online = [d for d in CFG["photo_dirs"] if os.path.isdir(d)]
    gone = []
    for pid in body.ids:
        path = snap.meta[pid]["path"] if pid in snap.meta else None
        if path and any(is_under(path, d) for d in online) and not os.path.exists(path):
            gone.append(pid)
    return {"missing": gone}


class ExportRequest(BaseModel):
    ids: list[int]
    dest: str
    include_groups: bool = False


@app.post("/api/export")
def export(body: ExportRequest):
    """把选中的照片复制到指定文件夹（只读原图）。同名同大小的视为已导出过，跳过；同名不同内容的改名保存。"""
    import shutil

    snap = get_snap(need_vectors=False)
    dest = Path(body.dest.strip())
    if not dest.is_absolute() or not dest.is_dir():
        raise HTTPException(400, {"code": "export_dir_not_found", "path": body.dest})
    dest = dest.resolve()
    for d in CFG["photo_dirs"] + [CFG["index_dir"]]:
        if dest == Path(d) or dest.is_relative_to(Path(d)):
            raise HTTPException(400, {"code": "export_dir_in_library"})
    picked, seen = [], set()
    for pid in body.ids:
        if pid not in snap.meta:
            continue
        for m in snap.members[snap.group_of[pid]] if body.include_groups else [pid]:
            if m not in seen:
                seen.add(m)
                picked.append(m)
    copied, skipped, failed = 0, 0, []
    for pid in picked:
        src = Path(snap.meta[pid]["path"])
        try:
            target = dest / src.name
            if target.exists() and target.stat().st_size == src.stat().st_size:
                skipped += 1
                continue
            n = 1
            while target.exists():  # 同名但内容不同：加序号另存
                target = dest / f"{src.stem}_{n}{src.suffix}"
                n += 1
            shutil.copy2(src, target)
            copied += 1
        except OSError as e:
            log.warning("导出失败 %s：%s", src, e)
            failed.append(src.name)
    log.info("导出 %d 张到 %s（跳过 %d，失败 %d）", copied, dest, skipped, len(failed))
    return {"copied": copied, "skipped": skipped, "failed": failed, "dest": str(dest)}


@app.post("/api/rescan")
def start_rescan():
    if S.phase != "ready":
        raise HTTPException(503, {"code": "model_loading"})
    return {"rescan": rescan()}


@app.get("/thumb/{pid}")
def thumb(pid: int):
    path = Path(CFG["index_dir"]) / "thumbs" / f"{pid}.jpg"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/photo/{pid}")
def photo(pid: int, max_side: int = Query(2048, ge=256, le=8192), fmt: str = Query("jpeg", pattern="^(jpeg|png)$")):
    """预览或复制用：按方向转正并缩小后返回；原图只读不改。"""
    info = S.snap.meta.get(pid) if S.snap else None
    if not info:
        raise HTTPException(404, {"code": "photo_not_found"})
    try:
        with Image.open(info["path"]) as im:
            im.draft("RGB", (max_side, max_side))
            im = ImageOps.exif_transpose(im).convert("RGB")
    except OSError as e:
        raise HTTPException(404, {"code": "original_unavailable"}) from e
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    if fmt == "png":  # 剪贴板只接受 PNG
        im.save(buf, "PNG", compress_level=1)
    else:
        im.save(buf, "JPEG", quality=88)
    return Response(buf.getvalue(), media_type=f"image/{fmt}", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/info/{pid}")
def info(pid: int):
    """预览面板用的补充信息：文件大小、相机型号。按需读取，不存进索引。"""
    meta = S.snap.meta.get(pid) if S.snap else None
    if not meta:
        raise HTTPException(404, {"code": "photo_not_found"})
    out = {"bytes": None, "camera": None}
    try:
        out["bytes"] = os.path.getsize(meta["path"])
        with Image.open(meta["path"]) as im:
            exif = im.getexif()
        make, model = str(exif.get(271) or "").strip("\x00 "), str(exif.get(272) or "").strip("\x00 ")
        out["camera"] = model if make.lower() in model.lower() else f"{make} {model}".strip()
    except OSError:
        pass  # 原图不可访问时只返回空值，界面照常显示其他信息
    return out


# 界面静态文件：Tauri 用编进 exe 的那份，这里挂出来是为了能直接用浏览器调试；发布包里没有 app/src，就不挂
UI_DIR = ROOT / "app" / "src"
if UI_DIR.is_dir():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")


def exit_with_parent(pid):
    """界面进程退出（包括崩溃）后后端跟着退出，避免残留在后台占显存。

    不用“读 stdin 到 EOF”的办法：Windows 上有线程阻塞读管道 stdin 时，导入 transformers 会卡死。
    """
    if os.name != "nt":  # 其他系统：每 2 秒看一次界面进程还在不在
        def poll():
            while True:
                time.sleep(2)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    log.info("界面进程已退出，后端随之退出")
                    os._exit(0)
                except PermissionError:
                    pass  # 进程还在，只是没有权限发信号

        threading.Thread(target=poll, daemon=True).start()
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        log.warning("打不开界面进程 %s，后端不会随界面退出", pid)
        return

    def wait():
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        log.info("界面进程已退出，后端随之退出")
        os._exit(0)

    threading.Thread(target=wait, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--parent-pid", type=int, help="界面进程 PID；它退出后后端随之退出")
    args = ap.parse_args()

    handlers = [logging.FileHandler(Path(CFG["index_dir"]) / "backend.log", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=handlers)

    if args.parent_pid:
        exit_with_parent(args.parent_pid)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", args.port))
    sock.listen(64)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    threading.Thread(target=boot, daemon=True).start()
    log.info("监听 %s", url)
    # 缩略图目录交给界面外壳，用 asset 协议直接读文件，不经过本进程
    thumbs = Path(CFG["index_dir"]) / "thumbs"
    thumbs.mkdir(exist_ok=True)
    print(f"THUMBS {thumbs}", flush=True)
    print(f"READY {url}", flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
