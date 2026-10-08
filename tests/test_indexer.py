import io

import numpy as np
from conftest import make_photo
from PIL import Image

from backend.common import open_db
from backend.indexer import dhash, embed_pending, hash_pending, is_under, photo_kind, prepare, sync_files


def smooth_image(seed, size=(800, 600)):
    """平滑的随机图（小网格放大），像照片一样有大块明暗变化。"""
    grid = (np.random.default_rng(seed).random((6, 8, 3)) * 255).astype("uint8")
    return Image.fromarray(grid).resize(size, Image.BICUBIC)


def bits_apart(a, b):
    return bin((a ^ b) & (2**64 - 1)).count("1")


def test_sync_adds_changes_and_removes(library):
    photos, cfg = library
    a = make_photo(photos / "a.jpg")
    (photos / "sub").mkdir()
    make_photo(photos / "sub" / "b.jpg")  # 子文件夹也要扫到
    con = open_db(cfg["index_dir"])
    assert sync_files(con, cfg) == {"removed": 0, "offline": [], "pending": 2}

    a.unlink()
    assert sync_files(con, cfg)["removed"] == 1
    assert con.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1


def test_offline_folder_keeps_index(library):
    """NAS 断开（目录无法访问）时，不能把里面的照片当作已删除。"""
    photos, cfg = library
    make_photo(photos / "a.jpg")
    con = open_db(cfg["index_dir"])
    sync_files(con, cfg)
    photos.rename(photos.with_name("photos_offline"))  # 模拟目录暂时访问不到
    result = sync_files(con, cfg)
    assert result["removed"] == 0
    assert result["offline"] == cfg["photo_dirs"]
    assert con.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1


def test_removed_folder_drops_its_photos(library):
    """从配置里移除的文件夹，它下面的照片应从索引中去掉。"""
    photos, cfg = library
    make_photo(photos / "a.jpg")
    con = open_db(cfg["index_dir"])
    sync_files(con, cfg)
    assert sync_files(con, {**cfg, "photo_dirs": []})["removed"] == 1


def test_photo_kind_and_is_under():
    assert photo_kind(r"D:\x\Screenshot_20250101_120000.jpg") == "screenshot"
    assert photo_kind("/x/截屏2025-01-01.png") == "screenshot"
    assert photo_kind("/x/Screen Shot 2019-05-01 at 10.00.00.png") == "screenshot"  # 旧版 macOS
    assert photo_kind("/x/スクリーンショット 2025-01-01 10.00.00.png") == "screenshot"
    assert photo_kind("/x/Bildschirmfoto 2025-01-01 um 10.00.00.png") == "screenshot"
    assert photo_kind(r"D:\x\IMG_20250101_120000.jpg") == "photo"
    assert is_under(r"D:\Photos\2025\a.jpg", r"D:\photos")
    assert not is_under(r"D:\Photos2\a.jpg", r"D:\Photos")


def test_prepare_reads_exif_and_rotation(tmp_path):
    src = make_photo(tmp_path / "r.jpg", size=(80, 40), taken="2024:05:01 10:20:30", orientation=6)
    im, w, h, taken, dh = prepare(src, tmp_path / "t.jpg", 0)
    assert (w, h) == (40, 80)  # 方向 6 表示需要旋转 90°，宽高互换
    assert im.size == (40, 80)
    assert taken == "2024-05-01 10:20:30"
    assert (tmp_path / "t.jpg").exists()
    assert -2**63 <= dh < 2**63  # 能直接存进 SQLite 的 INTEGER


def test_dhash_tells_copies_from_other_photos():
    """压缩、缩小过的副本 dHash 只差几位；另一张照片差得多。"""
    photo = smooth_image(0)
    buf = io.BytesIO()
    photo.resize((400, 300)).save(buf, "JPEG", quality=60)
    copy = Image.open(buf)
    assert bits_apart(dhash(photo), dhash(copy)) <= 4
    assert bits_apart(dhash(photo), dhash(smooth_image(1))) >= 16


class CountingModel:
    """代替真模型：记下每次编码几张，返回固定向量。"""
    def __init__(self):
        self.calls = []

    def encode(self, items, **kw):
        self.calls.append(len(items))
        return np.ones((len(items), 4), dtype=np.float32) / 2


def test_embed_reports_progress_every_few_photos(library):
    """每编完 8 张就写库、报进度（没有显卡时一批 64 张要两分钟）；坏图记下错误，不影响其他照片。"""
    photos, cfg = library
    for k in range(20):
        make_photo(photos / f"p{k:02}.jpg", color=(k * 10, 80, 40))
    (photos / "broken.jpg").write_bytes(b"not a jpeg")
    con = open_db(cfg["index_dir"])
    sync_files(con, cfg)
    model, seen = CountingModel(), []
    stats = embed_pending(con, model, cfg, progress=lambda done, total: seen.append((done, total)))
    assert model.calls == [8, 8, 4]
    assert seen[0] == (0, 21) and seen[-1] == (21, 21) and len(seen) >= 4
    assert stats["embedded"] == 20 and stats["failed"] == 1
    done = con.execute("SELECT COUNT(*) FROM photos WHERE embedding IS NOT NULL AND dhash IS NOT NULL").fetchone()[0]
    assert done == 20


def test_changed_file_and_old_index_get_new_dhash(library):
    """改过的照片 dHash 清空、重新算；0.1.0 建的旧索引从缩略图补算，缩略图丢了就跳过。"""
    photos, cfg = library
    a = make_photo(photos / "a.jpg")
    make_photo(photos / "b.jpg")
    con = open_db(cfg["index_dir"])
    sync_files(con, cfg)
    con.execute("UPDATE photos SET embedding=x'00', dhash=NULL")  # 模拟旧索引：已编码、没有 dHash
    aid = con.execute("SELECT id FROM photos WHERE path=?", (str(a),)).fetchone()[0]
    smooth_image(0).save(f"{cfg['index_dir']}/thumbs/{aid}.jpg")  # 只有 a 的缩略图还在
    assert hash_pending(con, cfg) == 2
    rows = dict(con.execute("SELECT path, dhash FROM photos").fetchall())
    assert rows[str(a)] is not None and rows[str(photos / "b.jpg")] is None

    make_photo(a, color=(10, 20, 30), size=(70, 50))  # 照片被改过
    sync_files(con, cfg)
    assert con.execute("SELECT dhash FROM photos WHERE path=?", (str(a),)).fetchone()[0] is None
