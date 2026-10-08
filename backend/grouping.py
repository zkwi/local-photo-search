"""连拍与重复照片分组：结果里每组只显示一张，预览时可看整组。

规则：相似度 ≥ 0.93 且拍摄时间相差 10 分钟内（连拍、同一瞬间换个角度），或相似度 ≥ 0.98（同一张图的多份拷贝，
如原图和微信导出）。阈值来自 2000 张样本的人工比对：0.93 以上基本是同一瞬间，0.88~0.92 多是同一场合的不同构图；
不限时间时 0.93 会串成上百张的大组，所以必须加时间窗。
"""
import torch

BURST_SIM = 0.93
BURST_WINDOW = 600  # 秒
DUP_SIM = 0.98
STEP = 1024  # 分块计算相似度，控制显存占用


def find_groups(mat, times):
    """mat 为已归一化的 (N, D) 张量，times 为拍摄时间戳（未知为 None）。返回每张所属组的根下标。"""
    n = mat.shape[0]
    t = torch.tensor([float("nan") if x is None else x for x in times], device=mat.device, dtype=torch.float64)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for start in range(0, n, STEP):
        sims = (mat[start:start + STEP] @ mat.T).float()
        near = (t[start:start + STEP, None] - t[None, :]).abs() <= BURST_WINDOW  # 时间未知时为 NaN，不算相近
        hit = (sims >= DUP_SIM) | ((sims >= BURST_SIM) & near)
        rows, cols = torch.nonzero(hit, as_tuple=True)
        for a, b in zip((rows + start).tolist(), cols.tolist(), strict=True):
            if a < b:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    return [find(i) for i in range(n)]


def regroup(con, ids, mat, times):
    """重新分组并写回 photos.group_id（值为组内第一张的 id）；返回多于一张的组数。"""
    roots = find_groups(mat, times)
    con.executemany("UPDATE photos SET group_id=? WHERE id=?", [(ids[r], ids[i]) for i, r in enumerate(roots)])
    con.commit()
    sizes = {}
    for r in roots:
        sizes[r] = sizes.get(r, 0) + 1
    return sum(1 for s in sizes.values() if s > 1)
