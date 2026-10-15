from __future__ import annotations

import base64
import io
import math
import threading
import warnings
from contextlib import ExitStack
from pathlib import Path

from PIL import Image, ImageCms, ImageColor, ImageEnhance, ImageFont, ImageOps, UnidentifiedImageError

from engines.common.media_tools import MediaError, finite_options, limit

MAX_PIXELS = limit("MMP_IMAGE_MAX_PIXELS", 16_000_000)
MAX_TOTAL_PIXELS = limit("MMP_IMAGE_MAX_TOTAL_PIXELS", 24_000_000)
MAX_BYTES = limit("MMP_IMAGE_MAX_OUTPUT_BYTES", 64 * 1024 * 1024)
MAX_INPUT_BYTES = limit("MMP_IMAGE_MAX_INPUT_BYTES", 64 * 1024 * 1024)
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
INPUT_FORMATS = ("BMP", "DIB", "GIF", "JPEG", "JPEG2000", "PCX", "PNG", "PPM", "QOI",
                 "SGI", "TGA", "TIFF", "WEBP", "ICO", "AVIF")
MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp",
         "TIFF": "image/tiff", "BMP": "image/bmp"}


def dimensions(width: int, height: int):
    if width < 1 or height < 1:
        raise MediaError("invalid_argument: image dimensions must be positive")
    if width * height > MAX_PIXELS or max(width, height) > 32768:
        raise MediaError(f"resource_limit: image {width}x{height} exceeds {MAX_PIXELS} pixels or 32768 per side")


def color(value: str):
    if value == "transparent":
        return (0, 0, 0, 0)
    try:
        return ImageColor.getcolor(value, "RGBA")
    except ValueError as e:
        raise MediaError(f"invalid_argument: invalid color {value!r}") from e


def check_cancel(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("cancelled")


class BoundedBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_BYTES:
            raise MediaError(f"resource_limit: image output exceeds {MAX_BYTES} bytes")
        return super().write(data)


def process(job: dict, cancelled: threading.Event | None = None) -> dict:
    op = job["task_type"].removeprefix("image.")
    args = job["arguments"]
    opts = args.get("options") or {}
    finite_options(opts)
    with ExitStack() as stack:
        total_pixels = 0

        def load(item):
            nonlocal total_pixels
            check_cancel(cancelled)
            path = Path(item["path"])
            if path.stat().st_size > MAX_INPUT_BYTES:
                raise MediaError(f"resource_limit: image input exceeds {MAX_INPUT_BYTES} bytes")
            try:
                # Warning policy is scoped to this worker; the engine serializes image jobs.
                with warnings.catch_warnings(), ExitStack() as decode:
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    raw = decode.enter_context(Image.open(path, formats=INPUT_FORMATS))
                    dimensions(*raw.size)
                    total_pixels += raw.width * raw.height
                    if total_pixels > MAX_TOTAL_PIXELS:
                        raise MediaError(f"resource_limit: total input pixels exceed {MAX_TOTAL_PIXELS}")
                    try:
                        raw.seek(1)
                    except EOFError:
                        raw.seek(0)
                    else:
                        raise MediaError("unsupported_input: animated or multipage image; submit a single frame")
                    fmt = raw.format
                    raw.load()  # Truncation must not become a successful partial result.
                    im = decode.enter_context(ImageOps.exif_transpose(raw))
                    if im.mode.startswith("I;16"):
                        # Map the complete unsigned 16-bit range, rather than clipping values above 255.
                        integer_image = decode.enter_context(im.convert("I"))
                        mapped = decode.enter_context(integer_image.point(lambda value: (value + 128) / 257))
                        im = decode.enter_context(mapped.convert("L"))
                    elif im.mode in {"I", "F"}:
                        raise MediaError("unsupported_input: signed integer/float raster needs an explicit tone mapping")
                    if profile := im.info.get("icc_profile"):
                        source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
                        im = decode.enter_context(ImageCms.profileToProfile(
                            im, source, ImageCms.createProfile("sRGB"),
                            outputMode="RGBA" if "A" in im.getbands() else "RGB"))
                    normalized = stack.enter_context(im.convert("RGBA"))
                    normalized.info.clear()
                    normalized.info["source_mode"] = raw.mode
                    normalized.info["source_alpha"] = "A" in raw.getbands() or "transparency" in raw.info
                    return normalized, fmt
            except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
                raise MediaError(f"resource_limit: {e}") from e
            except UnidentifiedImageError as e:
                raise MediaError("unsupported_format: content is not a supported raster image") from e
            except (OSError, SyntaxError, ValueError, ImageCms.PyCMSError) as e:
                if isinstance(e, MediaError):
                    raise
                raise MediaError(f"invalid_media: cannot decode image: {e}") from e

        items = args.get("images") or [args["image"]]
        images = [load(item) for item in items]
        im, source_format = images[0]

        def own(value):
            return stack.enter_context(value)

        if op == "inspect":
            return {"metadata": {"kind": "json", "value": {
                "width": im.width, "height": im.height, "format": source_format.lower(),
                "mode": im.info["source_mode"], "has_alpha": im.info["source_alpha"],
                "bytes": Path(items[0]["path"]).stat().st_size,
            }}}
        if op == "crop":
            x, y, w, h = (opts[k] for k in ("x", "y", "width", "height"))
            if x + w > im.width or y + h > im.height:
                raise MediaError("invalid_argument: crop rectangle lies outside the image")
            im = own(im.crop((x, y, x + w, y + h)))
        elif op == "resize":
            w, h = opts["width"], opts["height"]
            dimensions(w, h)
            mode = opts.get("mode", "contain")
            if mode == "stretch":
                im = own(im.resize((w, h), Image.Resampling.LANCZOS))
            else:
                scale = min(w / im.width, h / im.height)
                im = own(im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                                   Image.Resampling.LANCZOS))
                if mode == "pad":
                    canvas = own(Image.new("RGBA", (w, h), color(opts.get("background", "white"))))
                    canvas.alpha_composite(im, ((w - im.width) // 2, (h - im.height) // 2))
                    im = canvas
        elif op == "rotate":
            angle = opts["angle"]
            if angle % 90 == 0:
                dimensions(*(im.size if angle % 180 == 0 else im.size[::-1]))
            else:
                radians = math.radians(angle)
                dimensions(math.ceil(abs(im.width * math.cos(radians)) + abs(im.height * math.sin(radians))) + 2,
                           math.ceil(abs(im.width * math.sin(radians)) + abs(im.height * math.cos(radians))) + 2)
            im = own(im.rotate(angle, Image.Resampling.BICUBIC, expand=True,
                               fillcolor=color(opts.get("background", "white"))))
        elif op == "flip":
            im = own(ImageOps.mirror(im) if opts["axis"] == "horizontal" else ImageOps.flip(im))
        elif op == "pad":
            w, h = opts["width"], opts["height"]
            dimensions(w, h)
            x, y = opts.get("x", (w - im.width) // 2), opts.get("y", (h - im.height) // 2)
            if x < 0 or y < 0 or x + im.width > w or y + im.height > h:
                raise MediaError("invalid_argument: canvas must contain the entire image at the requested position")
            canvas = own(Image.new("RGBA", (w, h), color(opts.get("background", "white"))))
            canvas.alpha_composite(im, (x, y))
            im = canvas
        elif op == "concat":
            gap = opts.get("gap", 0)
            layout = opts.get("layout", "horizontal")
            columns = len(images) if layout == "horizontal" else 1 if layout == "vertical" else opts.get("columns", 2)
            columns = min(columns, len(images))
            rows = math.ceil(len(images) / columns)
            widths = [max(images[i][0].width for i in range(col, len(images), columns)) for col in range(columns)]
            heights = [max(image.height for image, _ in images[row * columns:(row + 1) * columns]) for row in range(rows)]
            w, h = sum(widths) + gap * (columns - 1), sum(heights) + gap * (rows - 1)
            dimensions(w, h)
            canvas = own(Image.new("RGBA", (w, h), color(opts.get("background", "white"))))
            for index, (image, _) in enumerate(images):
                check_cancel(cancelled)
                row, col = divmod(index, columns)
                canvas.alpha_composite(image, (sum(widths[:col]) + gap * col, sum(heights[:row]) + gap * row))
            im = canvas
        elif op == "overlay":
            has_image, has_text = "overlay" in args, "text" in opts
            if has_image == has_text:
                raise MediaError("invalid_argument: supply exactly one of overlay media or options.text")
            if has_image:
                if "font" in args:
                    raise MediaError("invalid_argument: font is only valid for text overlay")
                layer, _ = load(args["overlay"])
                if "width" in opts or "height" in opts:
                    if not all(k in opts for k in ("width", "height")):
                        raise MediaError("invalid_argument: overlay sizing needs both width and height")
                    dimensions(opts["width"], opts["height"])
                    layer = own(layer.resize((opts["width"], opts["height"]), Image.Resampling.LANCZOS))
            else:
                if "width" in opts or "height" in opts:
                    raise MediaError("invalid_argument: text sizing uses font_size, not width/height")
                if "font" not in args:
                    raise MediaError("invalid_argument: text overlay requires a TTF/OTF font media with the needed glyphs")
                font_path = Path(args["font"]["path"])
                if font_path.stat().st_size > 8 * 1024 * 1024:
                    raise MediaError("resource_limit: font exceeds 8 MiB")
                try:
                    font = ImageFont.truetype(str(font_path), opts.get("font_size", 32))
                    from PIL import ImageDraw
                    draw = ImageDraw.Draw(im)
                    bounds = draw.multiline_textbbox((0, 0), opts["text"], font=font)
                    dimensions(max(1, math.ceil(bounds[2] - bounds[0])), max(1, math.ceil(bounds[3] - bounds[1])))
                    # FreeType renders an absent character as its .notdef glyph. Reject that
                    # rendering rather than silently painting replacement boxes (e.g. missing CJK).
                    missing = font.getmask("\U0010ffff")
                    missing_pixels = bytes(missing)
                    for character in set(opts["text"]) - {"\n"}:
                        check_cancel(cancelled)
                        glyph = font.getmask(character)
                        if glyph.size == missing.size and bytes(glyph) == missing_pixels:
                            raise MediaError(f"unsupported_text: font has no distinct glyph for U+{ord(character):04X}")
                    layer = own(Image.new("RGBA", (max(1, math.ceil(bounds[2] - bounds[0])),
                                                       max(1, math.ceil(bounds[3] - bounds[1])))))
                    ImageDraw.Draw(layer).multiline_text((-bounds[0], -bounds[1]), opts["text"], font=font,
                                                        fill=color(opts.get("color", "white")))
                except OSError as e:
                    raise MediaError(f"invalid_media: font cannot be decoded: {e}") from e
            x, y = opts.get("x", 0), opts.get("y", 0)
            if x + layer.width > im.width or y + layer.height > im.height:
                raise MediaError("invalid_argument: overlay must fit entirely within the base image")
            opacity = opts.get("opacity", 1)
            if opacity != 1:
                layer = own(layer.copy())
                layer.putalpha(layer.getchannel("A").point(lambda v: round(v * opacity)))
            im.alpha_composite(layer, (x, y))
        elif op == "adjust":
            alpha = own(im.getchannel("A"))
            rgb = own(im.convert("RGB"))
            for key, enhancer in (("brightness", ImageEnhance.Brightness),
                                  ("contrast", ImageEnhance.Contrast), ("saturation", ImageEnhance.Color)):
                if opts.get(key, 1) != 1:
                    adjusted = own(enhancer(rgb).enhance(opts[key]))
                    rgb.close()
                    rgb = adjusted
            if opts.get("grayscale"):
                rgb = own(ImageOps.grayscale(rgb))
            im = own(rgb.convert("RGBA"))
            im.putalpha(alpha)
        elif op != "convert":
            raise MediaError(f"unsupported_operation: image.{op}")

        check_cancel(cancelled)
        dimensions(*im.size)
        fmt = opts.get("format", "png").upper()
        if "quality" in opts and fmt not in {"JPEG", "WEBP"}:
            raise MediaError("invalid_argument: quality is only supported for JPEG and WebP")
        if fmt in {"JPEG", "BMP"}:
            bg = color(opts.get("background", "white"))
            if bg[3] != 255:
                raise MediaError("invalid_argument: JPEG/BMP background must be opaque")
            canvas = own(Image.new("RGBA", im.size, bg))
            canvas.alpha_composite(im)
            im = own(canvas.convert("RGB"))
        parameters = {}
        if fmt in {"JPEG", "WEBP"}:
            parameters["quality"] = opts.get("quality", 85)
        if fmt == "PNG":
            parameters["compress_level"] = 3  # Fast default; no exhaustive optimizer pass.
        if fmt == "TIFF":
            parameters["compression"] = "tiff_deflate"
        buffer = stack.enter_context(BoundedBuffer())
        try:
            # Every output is a fresh normalized image: no stale EXIF orientation or ICC profile.
            im.save(buffer, format=fmt, **parameters)
        except OSError as e:
            raise MediaError(f"unsupported_format: cannot encode {fmt}: {e}") from e
        check_cancel(cancelled)
        payload = buffer.getvalue()
        return {
            "image": {"kind": "media", "content_type": MIMES[fmt],
                      "data_base64": base64.b64encode(payload).decode("ascii")},
            "metadata": {"kind": "json", "value": {
                "width": im.width, "height": im.height, "format": fmt.lower(), "mode": im.mode,
                "has_alpha": "A" in im.getbands(), "bytes": len(payload),
            }},
        }
