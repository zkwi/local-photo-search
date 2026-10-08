"""重复照片整理：哪些算完全相同、哪些算几乎相同、建议保留哪张、按组翻页。不需要模型。"""
import math

import numpy as np
import torch

from backend.duplicates import find_duplicates, page_duplicates

H = 0x0F0F_3C3C_5A5A_6969  # 随便一个 dHash


def vec(axis, sim=1.0):
    """单位向量：和第 axis 维的基向量相似度为 sim。各组用不同的维度（隔一维），组与组之间互不相似。"""
    v = [0.0] * 10
    v[axis], v[axis + 1] = sim, math.sqrt(1 - sim**2)
    return v


def snapshot(rows):
    """rows: (id, 向量, dHash, 字节数, (宽, 高), 拍摄时间, 是否截图, 所属连拍组)"""
    from backend.server import Snapshot

    ids = [r[0] for r in rows]
    members = {}
    for r in rows:
        members.setdefault(r[7], []).append(r[0])
    snap = Snapshot(
        ids=ids, pos={pid: n for n, pid in enumerate(ids)}, group_of={r[0]: r[7] for r in rows}, members=members,
        meta={r[0]: {"path": f"/p/{r[0]}.jpg", "name": f"{r[0]}.jpg", "bytes": r[3], "width": r[4][0],
                     "height": r[4][1], "taken_at": r[5], "kind": "screenshot" if r[6] else "photo"} for r in rows},
        hashes=np.array([r[2] or 0 for r in rows], dtype=np.int64).view(np.uint64),
        hashed=np.array([r[2] is not None for r in rows]))
    snap.mat = torch.tensor([r[1] for r in rows], dtype=torch.float32)
    snap.is_shot = torch.tensor([r[6] for r in rows])
    return snap


BIG, SMALL = (4000, 3000), (1080, 810)
LIBRARY = [
    # 备份时多拷了一份：同一组，大小、尺寸、画面都一样
    (1, vec(0), H, 5_000_000, BIG, "2025-05-01 10:00:00", False, 1),
    (2, vec(0), H, 5_000_000, BIG, "2025-05-01 10:00:00", False, 1),
    # 原图和聊天软件压缩过的副本：相似度只有 0.9，不在同一组，dHash 只差 1 位
    (3, vec(2), H ^ 1, 4_000_000, BIG, "2025-04-01 09:00:00", False, 3),
    (4, vec(2, 0.9), H, 300_000, SMALL, "2025-04-20 20:00:00", False, 4),
    # 同一个 App 界面隔了几个月的两张截图：dHash 一样也不算副本
    (5, vec(4), H, 900_000, (1260, 2800), "2025-01-01 08:00:00", True, 5),
    (6, vec(4, 0.95), H, 900_000, (1260, 2800), "2025-06-01 08:00:00", True, 6),
    # 连拍：同一组，画面有变化（dHash 差 20 位）
    (7, vec(6), 0x00FF_00FF_00FF_00FF, 3_000_000, BIG, "2025-03-01 12:00:02", False, 7),
    (8, vec(6, 0.95), 0x00FF_00FF_00FF_00FF ^ 0xFFFFF, 3_100_000, BIG, "2025-03-01 12:00:01", False, 7),
    # 两张纯黑的误拍：dHash 都是 0，相似度也高，但不是同一张
    (9, vec(8), 0, 100_000, BIG, "2025-02-01 00:00:00", False, 9),
    (10, vec(8, 0.9), 0, 100_000, BIG, "2025-02-02 00:00:00", False, 10),
]


def test_find_duplicates_classifies_groups():
    groups = find_duplicates(snapshot(LIBRARY))
    assert [g["cat"] for g in groups] == ["identical", "near", "similar"]  # 完全相同的排最前

    same, copies, burst = groups
    assert same["roles"] == {1: "keep", 2: "identical"} and same["extra"] == 5_000_000

    assert copies["ids"] == [3, 4]  # 建议保留的（分辨率高）排第一
    assert copies["roles"] == {3: "keep", 4: "near"} and copies["extra"] == 300_000
    assert copies["taken"] == "2025-04-01 09:00:00"

    assert burst["ids"] == [8, 7]  # 只有连拍时按拍摄先后，不给建议
    assert set(burst["roles"].values()) == {"similar"} and burst["extra"] == 0


def test_page_keeps_groups_whole_and_counts_categories():
    groups = find_duplicates(snapshot(LIBRARY))
    every = lambda pid: True  # noqa: E731
    page = page_duplicates(groups, every, "all", 0, 1)
    assert [g["cat"] for g in page["groups"]] == ["identical"] and page["has_more"]  # 不够一组也给整组
    assert page["counts"] == {"all": 3, "identical": 1, "near": 1, "similar": 1}
    assert page["photos"] == 6 and page["extra"] == 5_300_000

    page = page_duplicates(groups, every, "all", 2, 120)  # 第二页从第三张（第二组）开始
    assert [g["cat"] for g in page["groups"]] == ["near", "similar"] and not page["has_more"]

    page = page_duplicates(groups, every, "near", 0, 120)
    assert [g["cat"] for g in page["groups"]] == ["near"] and page["count"] == 1

    page = page_duplicates(groups, lambda pid: pid == 4, "all", 0, 120)  # 组里有一张符合筛选就显示整组
    assert [g["ids"] for g in page["groups"]] == [[3, 4]]
