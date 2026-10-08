"""重复照片整理：只列出来供用户自己挑，不删除、不移动任何照片。

在连拍/重复分组（grouping.py）的基础上，再把“看起来几乎一样”的照片并进来：原图和聊天软件压缩、缩小、调色后的副本
画面相同，相似度却可能只有 0.83 左右，要靠 dHash 认。这一步只看照片不看截图：同一个 App 界面隔几个月截的图，
dHash 也只差几位（2000 张样本里这类误配有上百对，照片之间则没有一对误配）。

每组按像素数、文件大小挑出建议保留的一张，其余标为完全相同 / 几乎相同 / 相似（连拍里另一个瞬间，要用户自己挑）。
“几乎相同”除了另存的副本，也包括画面没有可见差别的连拍（样本里同一秒的连拍常常如此），对整理来说留一张就够了。
截图之间只认完全相同：同一个界面不同日子的截图（余额、聊天记录不同）看起来几乎一样，不能建议删掉。
"""
import numpy as np
import torch

from .grouping import STEP

SAME_SIM, SAME_HASH = 0.995, 2  # 完全相同：再加上文件大小、尺寸一样（多是备份或拷贝出来的）
NEAR_SIM, NEAR_HASH = 0.80, 4  # 几乎相同：微信压缩副本的相似度最低约 0.83，dHash 相差 0~1 位，调色后 0~4 位
CATEGORIES = ("identical", "near", "similar")  # 也是列表里的先后顺序


def hash_bits(hashes):
    """uint64 哈希 → (N, 64) 的 ±1 矩阵：两张图 dHash 相差的位数 = (64 − 点积) / 2，能和相似度一起分块用矩阵乘法算。"""
    bits = (hashes[:, None] >> np.arange(64, dtype=np.uint64)) & np.uint64(1)
    return bits.astype(np.float32) * 2 - 1


def near_pairs(snap):
    """看起来几乎一样的照片对（行号，可能已在同一组）。"""
    # 纯黑、纯白这类没有明暗变化的图 dHash 全是 0 或全是 1，彼此“相同”却不是同一张，不参与
    flat = (snap.hashes == 0) | (snap.hashes == np.uint64(2**64 - 1))
    usable = torch.from_numpy(snap.hashed & ~flat).to(snap.mat.device) & ~snap.is_shot
    rows = torch.nonzero(usable).flatten()
    if len(rows) < 2:
        return []
    mat = snap.mat[rows]
    bits = torch.from_numpy(hash_bits(snap.hashes)).to(mat.device, mat.dtype)[rows]
    pairs = []
    for start in range(0, len(rows), STEP):
        # 直接按原精度比较，不转 float32，大图库时显存省一半；±1 的点积是 −64~64 的整数，半精度也是精确的
        sims = mat[start:start + STEP] @ mat.T
        dots = bits[start:start + STEP] @ bits.T  # 相差 d 位时点积为 64 − 2d
        a, b = torch.nonzero((sims >= NEAR_SIM) & (dots >= 64 - 2 * NEAR_HASH), as_tuple=True)
        a = a + start
        keep = a < b
        pairs += zip(rows[a[keep]].tolist(), rows[b[keep]].tolist(), strict=True)
    return pairs


def mark_same(snap, hits, ref_hash):
    """以图搜图、找相似的结果里，看起来和参照图是同一张的（规则同“几乎相同”）标上 same。ref_hash 为 None 时不标。
    只看相似度分不清：同一个人在同一把椅子上隔天拍的照片相似度也有 0.96，压缩过的副本却可能只有 0.83。"""
    if ref_hash is None or not hits or snap.hashes is None:
        return hits
    ref = np.uint64(ref_hash)
    if ref in (0, 2**64 - 1):  # 纯黑、纯白这类图的 dHash 没有区分度
        return hits
    rows = [snap.pos[h["id"]] for h in hits]
    dist = np.bitwise_count(snap.hashes[rows] ^ ref)
    sims = np.array([h["score"] for h in hits])
    # 截图要几乎一模一样才算：同一个界面隔半个月截的两张（数字不同）相似度也有 0.985、dHash 只差 2 位
    shot = snap.is_shot[rows].cpu().numpy()
    alike = np.where(shot, (dist <= SAME_HASH) & (sims >= SAME_SIM), (dist <= NEAR_HASH) & (sims >= NEAR_SIM))
    same = snap.hashed[rows] & alike
    for h, s in zip(hits, same.tolist(), strict=True):
        if s:
            h["same"] = True
    return hits


def describe(snap, ids):
    """一组照片：挑出建议保留的一张，其余标上和它（或排在前面的其他张）的关系。"""
    meta = snap.meta
    pixels = lambda i: (meta[i]["width"] or 0) * (meta[i]["height"] or 0)  # noqa: E731
    # 先比分辨率，再比文件大小：同样尺寸的 JPEG，细节越多文件越大，连拍里文件最大的通常也最清晰
    order = sorted(ids, key=lambda i: (-pixels(i), -(meta[i].get("bytes") or 0), i))
    rows = [snap.pos[i] for i in order]
    sims = (snap.mat[rows] @ snap.mat[rows].T).float().cpu().numpy()
    h = snap.hashes[rows]
    dist = np.bitwise_count(h[:, None] ^ h[None, :])
    both = snap.hashed[rows][:, None] & snap.hashed[rows][None, :]
    shot = snap.is_shot[rows].cpu().numpy()
    size = np.array([meta[i].get("bytes") or -1 for i in order])
    w = np.array([meta[i]["width"] or 0 for i in order])
    hh = np.array([meta[i]["height"] or 0 for i in order])
    earlier = np.tril(np.ones((len(order), len(order)), dtype=bool), -1)  # 只和排在前面（更清晰、更大）的比
    same = (both & earlier & (dist <= SAME_HASH) & (sims >= SAME_SIM) & (size[:, None] == size[None, :])
            & (w[:, None] == w[None, :]) & (hh[:, None] == hh[None, :]))
    # 截图之间不算“几乎相同”：同一个界面隔几天截的图（余额、聊天记录不同）相似度和 dHash 都和真副本差不多，
    # 建议删掉其中一张可能丢信息。截图只认完全相同的文件，其余都归“相似”，由用户自己看
    near = both & earlier & (dist <= NEAR_HASH) & (sims >= NEAR_SIM) & ~(shot[:, None] | shot[None, :])
    roles = dict(zip(order, np.where(same.any(1), "identical", np.where(near.any(1), "near", "similar")).tolist(),
                     strict=True))
    if same.any() or near.any():
        roles[order[0]] = "keep"
        shown = order  # 有多余的：建议保留的排第一，其余按清晰度
    else:
        shown = sorted(ids, key=lambda i: (meta[i]["taken_at"] or "", i))  # 只有连拍：按拍摄先后，不给建议
    cats = set(roles.values()) - {"keep"}
    return {"ids": shown, "roles": roles, "cats": cats, "cat": next(c for c in CATEGORIES if c in cats),
            "extra": sum(meta[i].get("bytes") or 0 for i in ids if roles[i] in ("identical", "near")),
            "taken": meta[shown[0]]["taken_at"]}


def find_duplicates(snap):
    """所有重复照片组：完全相同的在前，其次是几乎相同，最后是连拍和相似；同类里按拍摄时间从新到旧。需要向量。"""
    parent = {g: g for g in snap.members}

    def find(g):
        while parent[g] != g:
            parent[g] = parent[parent[g]]
            g = parent[g]
        return g

    for a, b in near_pairs(snap):
        ra, rb = find(snap.group_of[snap.ids[a]]), find(snap.group_of[snap.ids[b]])
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    clusters = {}
    for g, members in snap.members.items():
        clusters.setdefault(find(g), []).extend(members)
    out = [describe(snap, ids) for ids in clusters.values() if len(ids) > 1]
    out.sort(key=lambda d: d["taken"] or "", reverse=True)
    out.sort(key=lambda d: CATEGORIES.index(d["cat"]))
    return out


def page_duplicates(groups, match, cat, offset, limit):
    """按组翻页（一组不拆到两页）：offset、limit 按照片张数算，和其他列表一样。
    match(id) 判断照片是否符合照片/截图、时间筛选，组里有一张符合就显示整组。"""
    shown = [g for g in groups if any(match(i) for i in g["ids"])]
    counts = {"all": len(shown), **{c: sum(c in g["cats"] for g in shown) for c in CATEGORIES}}
    if cat != "all":
        shown = [g for g in shown if cat in g["cats"]]
    page, n, pos, last = [], 0, 0, -1
    for k, g in enumerate(shown):
        if pos >= offset:
            if n >= limit:
                break
            page.append(g)
            n += len(g["ids"])
            last = k
        pos += len(g["ids"])
    return {"groups": page, "has_more": bool(page) and last + 1 < len(shown), "count": len(shown),
            "photos": sum(len(g["ids"]) for g in shown), "extra": sum(g["extra"] for g in shown), "counts": counts}
