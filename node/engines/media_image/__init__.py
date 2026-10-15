"""Deterministic image tools. Importing the registry does not load Pillow."""

from engines.common.media_tools import choice, integer, number, options

ENGINE = "media-image"
ENGINE_VERSION = "1"


FORMAT = {
    "format": choice("png", "jpeg", "webp", "tiff", "bmp", default="png"),
    "quality": integer(1, 100),
    "background": {"type": "string", "maxLength": 32, "default": "white",
                   "description": "JPEG/BMP 的透明背景合成色；补边也用此色，可显式指定 transparent。"},
}


def media(name="image", required=True, many=False, accepts=None):
    return {"name": name, "kind": "media", "required": required,
            "accepts": accepts or ["image/*", "application/octet-stream"],
            "cardinality": {"min": 1 if required else 0, "max": 16 if many else 1}}


def capability(op, description, properties=None, required=(), inputs=None):
    fields = inputs if inputs is not None else [media()]
    if properties is not None:
        fields = [*fields, {"name": "options", "kind": "json", "required": bool(required),
                           "schema": options({**FORMAT, **properties}, required)}]
    outputs = [{"name": "metadata", "kind": "json", "required": True, "schema": {
        "type": "object", "properties": {
            "width": integer(1), "height": integer(1), "format": {"type": "string"},
            "mode": {"type": "string"}, "has_alpha": {"type": "boolean"},
            "bytes": integer(0, 9007199254740991), "timings_ms": {"type": "object"},
        }, "required": ["width", "height", "format", "mode", "has_alpha", "bytes"],
        "additionalProperties": False,
    }}]
    if op != "inspect":
        outputs.insert(0, {"name": "image", "kind": "media", "required": True,
            "produces": ["image/png", "image/jpeg", "image/webp", "image/tiff", "image/bmp"],
            "cardinality": {"min": 1, "max": 1}})
    return {"id": f"image.{op}", "purpose": f"image.{op}", "visibility": "public",
            "description": description + "；静态单帧图片，按内容解码；不支持、损坏或超限明确报错。" + (
                "输出规范化方向的 8 位 RGB/RGBA，移除原始元数据。" if op != "inspect" else ""),
            "tiers": [{"tier": "cpu", "engine": ENGINE, "engine_version": ENGINE_VERSION,
                       "cost": "low", "max_concurrency": 1}],
            "input": {"fields": fields}, "output": {"fields": outputs, "agent_context": "none"}}


CAPABILITIES = [
    capability("inspect", "读取图片实际格式、方向修正后的像素尺寸、模式和透明通道"),
    capability("convert", "图片格式转换；JPEG/BMP 用 background 合成透明背景", {}),
    capability("crop", "按方向修正后的像素坐标裁切，区域必须完全在图片内", {
        "x": integer(), "y": integer(), "width": integer(1), "height": integer(1),
    }, ("x", "y", "width", "height")),
    capability("resize", "缩放到指定宽高：contain 等比适配、stretch 拉伸、pad 等比补边", {
        "width": integer(1), "height": integer(1),
        "mode": choice("contain", "stretch", "pad", default="contain"),
    }, ("width", "height")),
    capability("rotate", "逆时针旋转指定角度，扩展画布保留全部内容", {
        "angle": number(-360, 360),
    }, ("angle",)),
    capability("flip", "水平或垂直翻转", {"axis": choice("horizontal", "vertical")}, ("axis",)),
    capability("pad", "补到指定画布尺寸，支持透明或纯色背景；不生成新内容", {
        "width": integer(1), "height": integer(1), "x": integer(), "y": integer(),
    }, ("width", "height")),
    capability("concat", "原尺寸横向、纵向或网格拼图；不自动缩放素材", {
        "layout": choice("horizontal", "vertical", "grid", default="horizontal"),
        "columns": integer(1, 16, default=2), "gap": integer(0, 1024, default=0),
    }, inputs=[media("images", many=True)]),
    capability("overlay", "叠加图片或文字水印；文字须提供字体媒体，位置按像素指定", {
        "x": integer(), "y": integer(), "opacity": number(0, 1, default=1),
        "width": integer(1), "height": integer(1),
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        "font_size": integer(1, 512, default=32),
        "color": {"type": "string", "maxLength": 32, "default": "white"},
    }, inputs=[media(), media("overlay", required=False), media("font", required=False,
               accepts=["font/ttf", "font/otf", "application/octet-stream"])]),
    capability("adjust", "数值调整亮度、对比度、饱和度或转灰度，不做自动增强", {
        "brightness": number(0, 4, default=1), "contrast": number(0, 4, default=1),
        "saturation": number(0, 4, default=1), "grayscale": {"type": "boolean", "default": False},
    }),
]
