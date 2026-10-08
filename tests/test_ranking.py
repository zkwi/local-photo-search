"""排序相关的纯计算：连拍分组、截图减分、每组只取一张的翻页、时间与范围筛选。不需要模型。"""
import torch

from backend.calibration import SCREENSHOT_PENALTY, penalize_screenshots
from backend.grouping import find_groups


def unit(*rows):
    t = torch.tensor(rows, dtype=torch.float32)
    return t / t.norm(dim=1, keepdim=True)


def test_groups_bursts_and_copies():
    near = [1.0, 0.1, 0.0]  # 和 [1, 0, 0] 的相似度约 0.995
    mat = unit([1, 0, 0], near, [0, 1, 0], near, [0, 0, 1])
    t0 = 1_700_000_000.0
    times = [t0, t0 + 60, t0 + 120, t0 + 86400 * 30, None]
    roots = find_groups(mat, times)
    assert roots[0] == roots[1]  # 1 分钟内的连拍
    assert roots[3] == roots[0]  # 相似度 ≥ 0.98：不限时间也算同一张图的拷贝
    assert roots[2] != roots[0] and roots[4] != roots[0]


def test_burst_needs_time_window():
    mat = unit([1, 0, 0], [1, 0.3, 0])  # 相似度约 0.958：够连拍、不够拷贝
    t0 = 1_700_000_000.0
    assert find_groups(mat, [t0, t0 + 60])[1] == 0  # 拍摄时间接近：同组
    assert find_groups(mat, [t0, t0 + 3600])[1] == 1  # 隔了一小时：不同组
    assert find_groups(mat, [t0, None])[1] == 1  # 时间未知：不按连拍合并


def test_screenshot_penalty_only_when_not_leading():
    is_shot = torch.tensor([True] * 5 + [False] * 5)
    close = torch.tensor([0.30] * 5 + [0.31] * 5)  # 截图和照片差不多：画面类查询，给截图减分
    assert torch.allclose(penalize_screenshots(close, is_shot)[:5], close[:5] - SCREENSHOT_PENALTY)
    leading = torch.tensor([0.40] * 5 + [0.30] * 5)  # 截图明显领先：在找截图或文字，不减
    assert torch.equal(penalize_screenshots(leading, is_shot), leading)


def test_in_range():
    from backend.server import in_range

    assert in_range(None, None, None)  # 没有时间筛选：时间未知的也显示
    assert not in_range(None, 0, None)  # 有筛选：时间未知的不显示
    assert in_range(100, 100, 200) and not in_range(200, 100, 200)  # 左闭右开


def test_page_folds_groups_and_filters():
    from backend.server import Snapshot, page_by_score

    ids = [10, 11, 12, 13]
    meta = {i: {"path": f"/p/{i}.jpg", "name": f"{i}.jpg", "width": 1, "height": 1, "taken_at": None,
                "kind": "screenshot" if i == 13 else "photo"} for i in ids}
    snap = Snapshot(ids=ids, meta=meta, pos={i: n for n, i in enumerate(ids)},
                    group_of={10: 10, 11: 10, 12: 12, 13: 13}, members={10: [10, 11], 12: [12], 13: [13]})
    snap.is_shot = torch.tensor([False, False, False, True])
    snap.times = torch.tensor([100.0, 101.0, 200.0, float("nan")], dtype=torch.float64)
    scores = torch.tensor([0.9, 0.95, 0.5, 0.8])

    hits, more = page_by_score(snap, scores.clone(), 0, 10, "all")
    assert [h["id"] for h in hits] == [11, 13, 12]  # 10 和 11 同组，只留分高的 11
    assert hits[0]["count"] == 2 and not more

    hits, _ = page_by_score(snap, scores.clone(), 0, 10, "photo")
    assert [h["id"] for h in hits] == [11, 12]

    hits, _ = page_by_score(snap, scores.clone(), 0, 10, "all", start=150, end=300)
    assert [h["id"] for h in hits] == [12]  # 时间未知的截图在有时间筛选时不出现

    hits, more = page_by_score(snap, scores.clone(), 0, 1, "all")
    assert [h["id"] for h in hits] == [11] and more

    hits, _ = page_by_score(snap, scores.clone(), 0, 10, "all", exclude=10)
    assert [h["id"] for h in hits] == [13, 12]  # 找相似时，原图所在的整组都不出现
