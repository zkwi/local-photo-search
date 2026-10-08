from conftest import make_photo

from backend.common import open_db
from backend.indexer import is_under, photo_kind, prepare, sync_files


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
    im, w, h, taken = prepare(src, tmp_path / "t.jpg", 0)
    assert (w, h) == (40, 80)  # 方向 6 表示需要旋转 90°，宽高互换
    assert im.size == (40, 80)
    assert taken == "2024-05-01 10:20:30"
    assert (tmp_path / "t.jpg").exists()
