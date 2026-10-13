"""内部生图能力：只向 provider 绑定 B 发布，权重由部署配置指定。"""
from copy import deepcopy

from engines.image_generate import CAPABILITIES as PUBLIC_CAPABILITIES

ENGINE = "krea2-internal-fp8"
ENGINE_VERSION = "1-06c67806-4ecd0d82"
MODEL_REPOSITORY = "Quiho/Krea2_NSFW_Uncensored_Complete"
MODEL_REVISION = "4ecd0d82d30daec24691db30495d752f2d7d7f64"

CAPABILITIES = deepcopy(PUBLIC_CAPABILITIES)
CAPABILITIES[0].update(
    id="image.generate.internal", visibility="internal",
    description="内部 FP8 文生图，仅 provider 绑定 B 可用；media 为 UTF-8 text/plain 提示词。"
                "单张 PNG base64，最多 1048576 像素，大结果需提供 media.put（接收 JSON）。")
CAPABILITIES[0]["tiers"][0].update(engine=ENGINE, engine_version=ENGINE_VERSION)
