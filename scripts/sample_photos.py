"""从一个大的照片目录随机抽样，复制一小份出来做测试（先在小样本上试效果，再索引全部照片）。

只读取源目录，不写入、不移动、不改名；复制用 copy2 保留修改时间，并输出抽样清单便于追溯。

用法：
    python scripts/sample_photos.py --src "D:\\Photos" --dst data/photos --n 2000
"""
import argparse
import csv
import random
import shutil
from pathlib import Path

EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261008)
    args = ap.parse_args()

    src, dst = Path(args.src).resolve(), Path(args.dst).resolve()
    if dst == src or src in dst.parents:
        raise SystemExit("目标目录不能在源目录里面")

    files = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in EXTS)
    picked = sorted(random.Random(args.seed).sample(files, min(args.n, len(files))))
    dst.mkdir(parents=True, exist_ok=True)

    rows = []
    for i, p in enumerate(picked, 1):
        rel = p.relative_to(src)
        # 平铺成 2025_10_IMG_xxx.jpg：避免不同子目录同名冲突，也保留来源子目录
        name = "_".join(rel.parts)
        out = dst / name
        if not out.exists():
            shutil.copy2(p, out)
        rows.append((str(rel), name, p.stat().st_size))
        if i % 200 == 0:
            print(f"已复制 {i}/{len(picked)}", flush=True)

    with open(dst.parent / "sample_manifest.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["source_relpath", "dest_name", "bytes"])
        w.writerows(rows)
    print(f"候选 {len(files)} 张，复制 {len(picked)} 张到 {dst}，共 {sum(r[2] for r in rows) / 1024**3:.2f} GB")


if __name__ == "__main__":
    main()
