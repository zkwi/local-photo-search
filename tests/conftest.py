"""测试共用：每个测试用自己的临时照片目录和索引目录，不碰真实数据，也不需要下载模型。"""
import json
import os
import tempfile
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

# backend.server 一导入就会读配置：先指向临时目录，整场测试不读写项目里的 config.json 和 index/
_TMP = Path(tempfile.mkdtemp(prefix="photo-search-test-"))
(_TMP / "config.json").write_text(json.dumps({"index_dir": str(_TMP / "index")}), encoding="utf-8")
os.environ["PHOTO_SEARCH_CONFIG"] = str(_TMP / "config.json")


def pytest_unconfigure(config):
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture
def library(tmp_path, monkeypatch):
    """临时图库：返回 (照片目录, 配置字典)。"""
    from backend import common

    photos, index = tmp_path / "photos", tmp_path / "index"
    photos.mkdir()
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"photo_dirs": [str(photos)], "index_dir": str(index)}), encoding="utf-8")
    monkeypatch.setattr(common, "CONFIG_PATH", cfg_file)
    return photos, common.load_config()


def make_photo(path, color=(200, 80, 40), size=(64, 48), taken=None, orientation=None):
    """生成一张小 JPEG，可写入拍摄时间和 EXIF 方向。"""
    exif = Image.Exif()
    if taken:
        exif.get_ifd(0x8769)[36867] = taken
    if orientation:
        exif[274] = orientation
    Image.new("RGB", size, color).save(path, "JPEG", exif=exif.tobytes())
    os.utime(path, (1_700_000_000, 1_700_000_000))
    return path
