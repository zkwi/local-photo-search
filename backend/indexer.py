"""增量建索引：扫描照片目录 → 生成缩略图 → EmbeddingGemma 2 编码 → 写入 SQLite。

只读取照片目录；缩略图和索引都写在 index 目录。新增、修改、删除的照片按路径+大小+修改时间识别，
中途中断后再次运行会接着处理未完成的部分。无法访问的目录（如 NAS 断开）保留原有索引，不当作删除。

用法：python -m backend.indexer
"""
import itertools
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .common import IMAGE_TOKENS, load_config, load_model, open_db, register_heif

log = logging.getLogger("indexer")
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
if register_heif():
    EXTS |= {".heic", ".heif"}
EMBED_SIDE = 1024  # 先缩到这个尺寸再送模型，模型内部还会按 token 预算缩放
THUMB_SIDE = 480
CHUNK = 64  # 一次预取解码的张数
# 编码批大小 8：实测与 32 一样快（RTX 4080S 每张约 45ms），torch 显存峰值却从 4.3GB 降到 1.8GB，小显存显卡也能建索引
ENCODE_BATCH = 8


def walk_images(root):
    """用 os.scandir 遍历：Windows 上目录项自带大小和修改时间，在 NAS 上比逐个 stat 快得多。"""
    stack = [root]
    while stack:
        with os.scandir(stack.pop()) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif os.path.splitext(e.name)[1].lower() in EXTS and e.is_file():
                    st = e.stat()
                    yield e.path, st.st_size, st.st_mtime_ns


def scan(photo_dirs):
    """返回 (照片列表, 无法访问的目录)；读到一半出错的目录也算无法访问。"""
    files, offline = [], []
    for d in photo_dirs:
        try:
            files.extend(walk_images(d))
        except OSError as e:
            log.warning("照片目录无法访问 %s：%s", d, e)
            offline.append(d)
    return files, offline


def is_under(path, folder):
    return os.path.normcase(path).startswith(os.path.normcase(folder).rstrip("\\/") + os.sep)


# 各语言系统截图的默认文件名（小写比较）：安卓、Windows、英文 macOS 多为 Screenshot / Screen Shot，
# 部分国产机型和中文 macOS 用“截屏”，其余是常见系统语言的写法
SCREENSHOT_NAMES = (
    "screenshot", "screen shot", "截屏", "截图", "螢幕截圖", "螢幕快照", "スクリーンショット", "스크린샷",
    "bildschirmfoto", "capture d’écran", "capture d'écran", "captura de pantalla", "schermata", "снимок экрана",
)


def photo_kind(path):
    """按文件名区分截图和照片。"""
    name = Path(path).name.lower()
    return "screenshot" if any(s in name for s in SCREENSHOT_NAMES) else "photo"


def exif_time(exif):
    raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal，没有就用 DateTime
    try:
        return datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def open_image(fp):
    """解码照片（路径或文件对象），按 EXIF 方向转正并缩到模型输入尺寸。返回 (图, 原始宽, 原始高, 拍摄时间或 None)。"""
    with Image.open(fp) as im:
        exif = im.getexif()
        w, h = im.size
        if exif.get(274) in (5, 6, 7, 8):  # EXIF 方向为旋转 90° 时宽高互换
            w, h = h, w
        taken = exif_time(exif)
        im.draft("RGB", (EMBED_SIDE, EMBED_SIDE))  # JPEG 按 1/2~1/8 直接解码，大图快很多
        im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((EMBED_SIDE, EMBED_SIDE))
    return im, w, h, taken


def dhash(im):
    """64 位差值哈希（9×8 灰度图里相邻格子比明暗），按有符号 64 位整数返回，正好存进 SQLite。
    同一张照片的压缩、缩小、调色副本相差 0~4 位，连拍的相邻几张、不同照片一般相差 20 位以上；裁剪过的认不出。"""
    g = np.asarray(im.convert("L").resize((9, 8), Image.LANCZOS), dtype=np.int16)
    return int.from_bytes(np.packbits(g[:, 1:] > g[:, :-1]).tobytes(), "big", signed=True)


def prepare(path, thumb_path, mtime_ns):
    """解码一张照片并写缩略图，返回 (模型输入图, 宽, 高, 拍摄时间, 缩略图的 dHash)。"""
    im, w, h, taken = open_image(path)
    thumb = im.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
    thumb.save(thumb_path, "JPEG", quality=82)
    if taken is None:
        taken = datetime.fromtimestamp(mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")
    return im, w, h, taken, dhash(thumb)


def _prefetch(todo, pool, thumbs):
    """分批解码：GPU 编码当前批时线程池已在解码下一批，内存里最多两批图。"""
    it = iter(todo)

    def submit():
        return [(i, pool.submit(prepare, p, thumbs / f"{i}.jpg", m)) for i, p, m in itertools.islice(it, CHUNK)]

    nxt = submit()
    while nxt:
        cur, nxt = nxt, submit()
        out = []
        for i, fut in cur:
            try:
                out.append((i, fut.result()))
            except Exception as e:  # 坏图只记录，不中断整批
                log.warning("跳过 id=%s：%s", i, e)
                out.append((i, e))
        yield out


def thumbs_dir(cfg):
    path = Path(cfg["index_dir"]) / "thumbs"
    path.mkdir(exist_ok=True)
    return path


def sync_files(con, cfg):
    """把磁盘上的增删改同步到 photos 表，需要重新编码的行把 embedding 置空。

    返回 {"removed": 删除数, "offline": 无法访问的目录, "pending": 待编码数}。
    """
    sig = f"{cfg['model']}|image_tokens={IMAGE_TOKENS}"
    row = con.execute("SELECT value FROM meta WHERE key='signature'").fetchone()
    if row and row[0] != sig:  # 换了模型或参数，旧向量不能混用
        con.execute("UPDATE photos SET embedding=NULL, error=NULL")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('signature', ?)", (sig,))

    files, offline = scan(cfg["photo_dirs"])
    known = {p: (i, s, m) for i, p, s, m in con.execute("SELECT id, path, size, mtime_ns FROM photos")}
    seen = set()
    for path, size, mtime in files:
        if path in seen:  # 文件夹互相嵌套时同一张会被扫到两次
            continue
        seen.add(path)
        old = known.get(path)
        if old is None:
            con.execute("INSERT INTO photos(path, size, mtime_ns) VALUES (?,?,?)", (path, size, mtime))
        elif old[1:] != (size, mtime):
            con.execute("UPDATE photos SET size=?, mtime_ns=?, embedding=NULL, error=NULL, group_id=NULL, dhash=NULL "
                        "WHERE id=?", (size, mtime, old[0]))
    # 无法访问的目录里的照片不算删除，等目录恢复后照常使用
    gone = [i for p, (i, _, _) in known.items() if p not in seen and not any(is_under(p, d) for d in offline)]
    con.executemany("DELETE FROM photos WHERE id=?", [(i,) for i in gone])
    con.commit()
    thumbs = thumbs_dir(cfg)
    for i in gone:
        (thumbs / f"{i}.jpg").unlink(missing_ok=True)
    pending = con.execute("SELECT COUNT(*) FROM photos WHERE embedding IS NULL AND error IS NULL").fetchone()[0]
    return {"removed": len(gone), "offline": offline, "pending": pending}


def embed_pending(con, model, cfg, progress=None, lock=None):
    """编码所有还没有向量的照片。lock 用于和搜索请求共用模型时串行化。"""
    thumbs = thumbs_dir(cfg)
    todo = con.execute("SELECT id, path, mtime_ns FROM photos WHERE embedding IS NULL AND error IS NULL").fetchall()
    total, done, failed, t0 = len(todo), 0, 0, time.perf_counter()
    if progress:
        progress(0, total)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for batch in _prefetch(todo, pool, thumbs):
            bad = [(repr(r)[:300], i) for i, r in batch if isinstance(r, Exception)]
            if bad:
                con.executemany("UPDATE photos SET error=? WHERE id=?", bad)
                con.commit()
                done, failed = done + len(bad), failed + len(bad)
            ok = [(i, r) for i, r in batch if not isinstance(r, Exception)]
            # 解码按 64 张一批预取，编码则每 8 张写一次库、报一次进度：没有显卡时 64 张要两分钟，
            # 整批编完才报的话进度条半天不动，搜索也要等这么久才轮到模型
            for k in range(0, len(ok), ENCODE_BATCH):
                part = ok[k:k + ENCODE_BATCH]
                with lock or nullcontext():
                    emb = model.encode([{"image": r[0]} for _, r in part], batch_size=ENCODE_BATCH,
                                       normalize_embeddings=True, show_progress_bar=False,
                                       processing_kwargs={"image": {"max_soft_tokens": IMAGE_TOKENS}})
                con.executemany(
                    "UPDATE photos SET width=?, height=?, taken_at=?, dhash=?, embedding=? WHERE id=?",
                    [(r[1], r[2], r[3], r[4], e.astype(np.float16).tobytes(), i)
                     for (i, r), e in zip(part, emb, strict=True)])
                con.commit()
                done += len(part)
                if progress:
                    progress(done, total)
            if bad and not ok and progress:  # 整批都是坏图时也报一下
                progress(done, total)
    return {"embedded": total - failed, "failed": failed, "seconds": round(time.perf_counter() - t0, 1)}


def hash_pending(con, cfg, progress=None):
    """给 0.1.0 建的旧索引补算 dHash：只读缩略图、不读原图，上万张也只要十几秒。返回补算的张数。"""
    thumbs = thumbs_dir(cfg)
    todo = [r[0] for r in con.execute("SELECT id FROM photos WHERE embedding IS NOT NULL AND dhash IS NULL")]

    def one(i):
        try:
            with Image.open(thumbs / f"{i}.jpg") as im:
                return i, dhash(im)
        except OSError:  # 缩略图丢了：这张照片只是认不出副本，下次启动再试
            return i, None

    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for start in range(0, len(todo), 500):
            chunk = todo[start:start + 500]
            con.executemany("UPDATE photos SET dhash=? WHERE id=?",
                            [(h, i) for i, h in pool.map(one, chunk) if h is not None])
            con.commit()
            done += len(chunk)
            if progress:
                progress(done, len(todo))
    return len(todo)


def needs_regroup(con):
    return con.execute("SELECT COUNT(*) FROM photos WHERE embedding IS NOT NULL AND group_id IS NULL").fetchone()[0] > 0


def timestamp(taken_at):
    try:
        return datetime.strptime(taken_at, "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return None


def update_index(model, cfg, progress=None):
    import torch

    from .grouping import regroup

    con = open_db(cfg["index_dir"])
    stats = sync_files(con, cfg)
    stats["hashed"] = hash_pending(con, cfg)
    stats.update(embed_pending(con, model, cfg, progress))
    if needs_regroup(con):
        ids, mat, meta, _, _ = load_index(cfg["index_dir"])
        vecs = torch.from_numpy(mat.astype(np.float32)).to(model.device)
        stats["groups"] = regroup(con, ids, vecs, [timestamp(meta[i]["taken_at"]) for i in ids])
    stats["indexed"] = con.execute("SELECT COUNT(*) FROM photos WHERE embedding IS NOT NULL").fetchone()[0]
    con.close()
    log.info("索引完成：%s", stats)
    return stats


def load_index(index_dir):
    """读出全部向量，返回 (id 列表, 向量矩阵, 每张照片的元信息, 每张所属组的 id, 每张的 dHash（没算过为 None）)。"""
    con = open_db(index_dir)
    rows = con.execute("SELECT id, path, size, width, height, taken_at, embedding, group_id, dhash FROM photos "
                       "WHERE embedding IS NOT NULL ORDER BY id").fetchall()
    con.close()
    ids = [r[0] for r in rows]
    mat = np.frombuffer(b"".join(r[6] for r in rows), dtype=np.float16).reshape(len(rows), -1) if rows else None
    meta = {r[0]: {"path": r[1], "name": Path(r[1]).name, "bytes": r[2], "width": r[3], "height": r[4],
                   "taken_at": r[5], "kind": photo_kind(r[1])} for r in rows}
    groups = [r[7] or r[0] for r in rows]  # 还没分组的照片自成一组
    return ids, mat, meta, groups, [r[8] for r in rows]


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    t0 = time.perf_counter()
    model = load_model(cfg["model"])
    log.info("模型加载 %.1fs，设备 %s", time.perf_counter() - t0, model.device)
    last = [0.0]

    def progress(done, total):
        if time.perf_counter() - last[0] > 5 or done == total:
            last[0] = time.perf_counter()
            log.info("进度 %d/%d", done, total)

    print(update_index(model, cfg, progress))


if __name__ == "__main__":
    main()
