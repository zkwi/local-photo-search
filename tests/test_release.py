"""发版前的一致性检查：版本号写在 4 个文件里（uv.lock 也记着一份），发布脚本按 tauri.conf.json 的版本给安装包命名。"""
import json
import tomllib

from conftest import ROOT


def test_versions_match():
    def read(path):
        return (ROOT / path).read_text(encoding="utf-8")

    lock = tomllib.loads(read("uv.lock"))["package"]
    versions = {
        "pyproject.toml": tomllib.loads(read("pyproject.toml"))["project"]["version"],
        "Cargo.toml": tomllib.loads(read("app/src-tauri/Cargo.toml"))["package"]["version"],
        "tauri.conf.json": json.loads(read("app/src-tauri/tauri.conf.json"))["version"],
        "package.json": json.loads(read("app/package.json"))["version"],
        # 改了 pyproject 的版本却没 uv lock 的话，CI 的 uv sync --locked 会失败
        "uv.lock": next(p["version"] for p in lock if p["name"] == "local-photo-search"),
    }
    assert len(set(versions.values())) == 1, versions


def test_china_mirror_script_is_ascii():
    """cmd.exe 会把批处理里的中文切碎后当命令执行，所以只能用 ASCII。"""
    (ROOT / "scripts/start-with-china-mirrors.cmd").read_bytes().decode("ascii")
