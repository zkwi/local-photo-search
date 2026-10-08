"""文本搜图的去偏：截图、文档这类文字多的图片，对任何文本查询的相似度都偏高，会挤占搜索结果。

做法：用一组和具体查询无关的通用描述，算出每张图的平均相似度作为“基线分”，排序时减掉。
在 2000 张手机照片上实测：画面类查询前 20 名里的截图从 26% 降到 8%，搜“聊天截图”等仍以截图为主。
"""

BACKGROUND_QUERIES = [
    "一个人站在路边", "城市街道", "公园里的树", "室内的房间", "桌子上的东西", "天空和云", "一辆汽车", "小孩", "老人",
    "饮料", "建筑物", "河流", "草地", "动物", "鸟", "夜晚的灯光", "超市", "餐厅", "办公室", "学校", "医院", "商场",
    "地铁", "火车站", "机场", "飞机", "船", "自行车", "道路", "桥", "广场", "寺庙", "博物馆", "海报", "标志牌", "手机",
    "电脑", "书", "衣服", "鞋子", "包", "玩具", "水果", "蔬菜", "厨房", "卧室", "客厅", "阳台", "窗户", "门", "楼梯",
    "停车场", "跑步", "游泳", "篮球", "足球", "唱歌", "跳舞", "画画", "写字", "看书", "睡觉", "吃饭", "走路", "开车",
    "骑车", "旅游", "日出", "晴天", "阴天", "森林", "沙漠", "田野", "农村", "古镇", "夜市", "烟花", "灯笼", "节日",
    "婚礼", "毕业", "宠物", "鱼", "马", "牛", "a photo of something", "an image", "a picture of people", "a room",
    "outdoor scene", "close-up of an object", "a street", "a building", "food on a table", "a person smiling",
]


def background_embeddings(model):
    """通用描述的文本向量；索引更新时复用，不必每次重新编码。"""
    return model.encode(BACKGROUND_QUERIES, prompt_name="SearchQuery", normalize_embeddings=True,
                        convert_to_tensor=True, show_progress_bar=False)


def text_bias(bg, mat):
    """每张图对通用文本的平均相似度（长度 N 的 float32 张量，与 mat 同设备）。"""
    return (bg.to(mat.device, mat.dtype) @ mat.T).float().mean(0)


# 画面类查询里，截图还会靠字面“蹭”上来（搜“海边”时，文字里带“海”字的截图也会排进前面）。
# 只在截图并不比照片更贴近时给截图减分；截图明显更贴近（找聊天记录、人名等）时不减。
# 2000 张样本实测：画面类查询前 20 名截图 11.7% → 3.9%；
# 找文字/截图类的查询 87% → 83.5%（固定减分会降到 80%，人名查询受损）。
SCREENSHOT_PENALTY = 0.02
TEXT_QUERY_GAP = 0.01


def penalize_screenshots(scores, is_shot):
    import torch

    n_shot = int(is_shot.sum())
    if n_shot < 5 or is_shot.numel() - n_shot < 5:
        return scores
    shot_top = torch.topk(scores.masked_fill(~is_shot, float("-inf")), 5).values.mean()
    photo_top = torch.topk(scores.masked_fill(is_shot, float("-inf")), 5).values.mean()
    if shot_top - photo_top >= TEXT_QUERY_GAP:
        return scores
    return scores - SCREENSHOT_PENALTY * is_shot.float()
