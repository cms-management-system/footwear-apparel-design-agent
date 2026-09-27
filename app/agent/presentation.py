"""Shared presentation defaults for comparable design candidates."""

from app.category import is_shoe


def presentation_for(spec):
    return {
        "version": 1,
        "priority": "用户明确的展示要求优先于默认值；有修改底图时保留底图视角、人物、背景和构图。整批保持一致。",
        "view": "单只鞋的完整外侧面，鞋头朝左，相机与鞋平齐" if is_shoe(spec["intent"]) else "完整服装的正面平视图",
        "subject": "独立单品，不上脚、不上身，不出现人物、身体部位、手、衣架或人台",
        "background": "纯白背景，柔和均匀棚拍光，轻微接触阴影",
        "framing": "正方形画面，单品居中完整入镜，最长边占画面约80%，统一留白，不裁切、不拼图",
        "variation": "各款只变化允许的设计结构与细节，不能用更换视角、模特、背景或缩放充当设计差异",
    }


def exploration_axes(spec):
    if is_shoe(spec["intent"]):
        return [
            "鞋面轮廓与包覆比例",
            "鞋带连接与侧面开口",
            "鞋面分割与金属扣位置",
            "鞋面裁片与缝线",
            "允许变化的鞋底边缘细节",
            "允许的材质外观与配色",
        ]
    return ["轮廓与比例", "领口与肩袖连接", "腰部与分割线", "褶裥与裁片", "口袋与开合细节", "允许的面料外观与配色"]
