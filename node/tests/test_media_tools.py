"""Real CPU media operations, adversarial inputs, and A/B delivery/cache integration."""
from __future__ import annotations

import asyncio
import base64
import io
import math
import struct
import sys
import wave
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from PIL import Image, ImageFont

from conftest import build_stack, inline

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines.common.media_tools import MediaError
from engines.media_audio import CAPABILITIES as AUDIO_CAPS
from engines.media_audio import runner as audio
from engines.media_image import CAPABILITIES as IMAGE_CAPS
from engines.media_image import runner as image
from mmp_node import schemas


def image_file(tmp_path, name="image.bin", mode="RGBA", size=(12, 8), fill=(255, 0, 0, 128), fmt="PNG"):
    path = tmp_path / name
    with Image.new(mode, size, fill) as im:
        im.save(path, format=fmt)
    return {"path": str(path)}


def image_job(op, item, **opts):
    return {"task_type": f"image.{op}", "arguments": {"image": item, "options": opts}}


def image_result(result):
    return Image.open(io.BytesIO(base64.b64decode(result["image"]["data_base64"])))


def wav_file(tmp_path, name="audio.bin", duration=0.5, rate=16000, channels=2, amplitude=0.2,
             leading=0, trailing=0):
    path = tmp_path / name
    with wave.open(str(path), "wb") as wav:
        wav.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        frames = bytearray()
        for i in range(round((duration + leading + trailing) * rate)):
            active = leading <= i / rate < leading + duration
            for channel in range(channels):
                value = amplitude * math.sin(2 * math.pi * (440 + channel * 220) * i / rate) if active else 0
                frames.extend(struct.pack("<h", round(value * 32767)))
        wav.writeframes(frames)
    return {"path": str(path)}


def audio_job(op, item, **opts):
    return {"task_type": f"audio.{op}", "arguments": {"audio": item, "options": opts}}


def samples(result, index=None):
    output = result["audio"] if index is None else result["audio"][index]
    with wave.open(io.BytesIO(base64.b64decode(output["data_base64"]))) as wav:
        return wav.getparams(), struct.unpack(f"<{wav.getnframes() * wav.getnchannels()}h", wav.readframes(wav.getnframes()))


def test_capabilities_validate_without_loading_models():
    caps = IMAGE_CAPS + AUDIO_CAPS
    assert len({cap["id"] for cap in caps}) == 21
    for cap in caps:
        assert not schemas.errors(schemas.validator_for_ref("urn:mmp:protocol:3:capability"), cap)
        for field in cap["input"]["fields"]:
            if field["kind"] == "json":
                Draft202012Validator.check_schema(field["schema"])
        assert cap["tiers"][0]["tier"] == "cpu"
        assert not any(f.get("preprocessor") for f in cap["input"]["fields"])


@pytest.mark.parametrize("fmt,mode,fill", [("PNG", "RGBA", (255, 0, 0, 128)),
    ("JPEG", "CMYK", (0, 255, 255, 0)), ("BMP", "RGB", (255, 0, 0)), ("TIFF", "I;16", 1024)])
def test_image_content_sniffing_and_unusual_modes(tmp_path, fmt, mode, fill):
    item = image_file(tmp_path, name="wrong.mp3", mode=mode, fill=fill, fmt=fmt)
    result = image.process(image_job("convert", item))
    with image_result(result) as im:
        assert im.size == (12, 8) and im.format == "PNG"


def test_inspect_reports_actual_source_mode_and_exif_orientation(tmp_path):
    item = image_file(tmp_path, mode="RGB", fill=(255, 0, 0), fmt="JPEG")
    with Image.open(item["path"]) as im:
        exif = Image.Exif()
        exif[274] = 6
        im.save(item["path"], format="JPEG", exif=exif)
    result = image.process(image_job("inspect", item))["metadata"]["value"]
    assert (result["width"], result["height"], result["mode"], result["has_alpha"]) == (8, 12, "RGB", False)
    with image_result(image.process(image_job("convert", item))) as im:
        assert im.size == (8, 12) and im.getexif().get(274) is None


@pytest.mark.parametrize("op,opts,size", [
    ("crop", {"x": 2, "y": 1, "width": 4, "height": 3}, (4, 3)),
    ("resize", {"width": 6, "height": 6}, (6, 4)),
    ("resize", {"width": 6, "height": 6, "mode": "stretch"}, (6, 6)),
    ("resize", {"width": 6, "height": 6, "mode": "pad", "background": "transparent"}, (6, 6)),
    ("rotate", {"angle": 90}, (8, 12)),
    ("rotate", {"angle": 45}, (16, 16)),
    ("flip", {"axis": "horizontal"}, (12, 8)),
    ("flip", {"axis": "vertical"}, (12, 8)),
    ("pad", {"width": 20, "height": 20}, (20, 20)),
    ("adjust", {"brightness": 0.5, "contrast": 1.2, "saturation": 0.5, "grayscale": True}, (12, 8)),
])
def test_image_geometry_and_adjustments(tmp_path, op, opts, size):
    with image_result(image.process(image_job(op, image_file(tmp_path), **opts))) as im:
        assert im.size == size


def test_alpha_and_jpeg_background(tmp_path):
    item = image_file(tmp_path, fill=(255, 0, 0, 0))
    result = image.process(image_job("convert", item, format="jpeg", background="blue", quality=95))
    with image_result(result) as im:
        red, green, blue = im.getpixel((0, 0))
        assert red < 5 and green < 5 and blue > 250
    result = image.process(image_job("adjust", item, grayscale=True))
    with image_result(result) as im:
        assert im.getpixel((0, 0))[3] == 0


@pytest.mark.parametrize("fmt", ["png", "jpeg", "webp", "tiff", "bmp"])
def test_image_output_formats(tmp_path, fmt):
    result = image.process(image_job("convert", image_file(tmp_path), format=fmt))
    with image_result(result) as im:
        assert im.format.lower() == fmt


def test_extremely_thin_image_resize_does_not_round_to_zero(tmp_path):
    item = image_file(tmp_path, size=(1, 32768))
    with image_result(image.process(image_job("resize", item, width=1, height=1))) as im:
        assert im.size == (1, 1)


def test_high_bit_depth_grayscale_maps_full_range(tmp_path):
    item = image_file(tmp_path, mode="I;16", fill=32768, fmt="TIFF")
    with image_result(image.process(image_job("convert", item))) as im:
        assert im.getpixel((0, 0)) == (128, 128, 128, 255)


@pytest.mark.parametrize("layout,size", [("horizontal", (25, 8)), ("vertical", (12, 17)), ("grid", (25, 8))])
def test_image_concat(tmp_path, layout, size):
    job = {"task_type": "image.concat", "arguments": {
        "images": [image_file(tmp_path), image_file(tmp_path, name="second.bin")],
        "options": {"layout": layout, "gap": 1}}}
    with image_result(image.process(job)) as im:
        assert im.size == size


def test_image_overlay_pixels(tmp_path):
    job = image_job("overlay", image_file(tmp_path, fill=(0, 0, 0, 255)), x=2, y=1, opacity=0.5)
    job["arguments"]["overlay"] = image_file(tmp_path, name="overlay.bin", size=(3, 2), fill=(255, 0, 0, 255))
    with image_result(image.process(job)) as im:
        assert im.getpixel((0, 0)) == (0, 0, 0, 255)
        assert im.getpixel((2, 1)) == (128, 0, 0, 255)


def test_text_overlay_with_explicit_font(tmp_path):
    # Pillow's bundled font is used as a portable test input, not as a product fallback.
    font = ImageFont.load_default()
    font_path = tmp_path / "font.bin"
    font_path.write_bytes(font.font_bytes)
    job = image_job("overlay", image_file(tmp_path, size=(100, 60)), text="Test", font_size=16, color="black")
    job["arguments"]["font"] = {"path": str(font_path)}
    with image_result(image.process(job)) as im:
        assert im.size == (100, 60)
    job["arguments"]["options"]["text"] = "中文"
    with pytest.raises(MediaError, match="unsupported_text"):
        image.process(job)


@pytest.mark.parametrize("op,opts,message", [
    ("crop", {"x": 11, "y": 0, "width": 3, "height": 1}, "outside"),
    ("pad", {"width": 2, "height": 2}, "entire image"),
    ("resize", {"width": 32768, "height": 32768}, "resource_limit"),
    ("convert", {"format": "jpeg", "background": "transparent"}, "opaque"),
    ("convert", {"format": "png", "quality": 80}, "quality"),
    ("overlay", {}, "exactly one"),
    ("overlay", {"text": "hello"}, "requires a TTF"),
    ("pad", {"width": 20, "height": 20, "background": "invalid-color"}, "invalid color"),
])
def test_image_rejects_invalid_operations(tmp_path, op, opts, message):
    with pytest.raises(MediaError, match=message):
        image.process(image_job(op, image_file(tmp_path), **opts))


def test_image_rejects_animation_garbage_truncation_and_resource_exhaustion(tmp_path, monkeypatch):
    path = tmp_path / "hostile.bin"
    path.write_bytes(b"not an image")
    with pytest.raises(MediaError, match="unsupported_format"):
        image.process(image_job("convert", {"path": str(path)}))
    with Image.new("RGB", (12, 8), "red") as first, Image.new("RGB", (12, 8), "blue") as second:
        first.save(path, format="GIF", save_all=True, append_images=[second])
    with pytest.raises(MediaError, match="animated or multipage"):
        image.process(image_job("convert", {"path": str(path)}))
    item = image_file(tmp_path, size=(100, 100))
    data = Path(item["path"]).read_bytes()
    Path(item["path"]).write_bytes(data[:len(data) // 2])
    with pytest.raises(MediaError, match="invalid_media"):
        image.process(image_job("convert", item))
    item = image_file(tmp_path)
    monkeypatch.setattr(image, "MAX_TOTAL_PIXELS", 10)
    with pytest.raises(MediaError, match="total input pixels"):
        image.process(image_job("convert", item))
    monkeypatch.setattr(image, "MAX_TOTAL_PIXELS", 10000)
    monkeypatch.setattr(image, "MAX_BYTES", 10)
    with pytest.raises(MediaError, match="resource_limit"):
        image.process(image_job("convert", item))


async def test_audio_content_sniffing_and_inspect(tmp_path):
    result = await audio.process(audio_job("inspect", wav_file(tmp_path, name="wrong.jpg")))
    info = result["metadata"]["value"]["files"][0]
    assert info["sample_rate"] == 16000 and info["channels"] == 2 and info["duration_sec"] == 0.5


@pytest.mark.parametrize("fmt,rate", [("wav", 16000), ("flac", 16000), ("mp3", 16000),
                                      ("ogg", 16000), ("opus", 48000), ("m4a", 16000)])
async def test_audio_formats_real_encoders(tmp_path, fmt, rate):
    result = await audio.process(audio_job("convert", wav_file(tmp_path, rate=rate), format=fmt))
    actual = result["metadata"]["value"]["files"][0]
    assert actual["sample_rate"] == rate and actual["channels"] == 2
    assert actual["duration_sec"] == pytest.approx(0.5, abs=0.12)
    assert len(base64.b64decode(result["audio"]["data_base64"])) == actual["bytes"]


@pytest.mark.parametrize("op,opts,rate,duration", [
    ("crop", {"start_sec": 0.1, "end_sec": 0.3}, 16000, 0.2),
    ("resample", {"sample_rate": 8000}, 8000, 0.5),
    ("fade", {"in_sec": 0.1, "out_sec": 0.1}, 16000, 0.5),
    ("volume", {"gain_db": -6}, 16000, 0.5),
    ("silence", {"mode": "pad", "before_sec": 0.1, "after_sec": 0.2}, 16000, 0.8),
    ("silence", {"mode": "mute", "start_sec": 0.1, "end_sec": 0.2}, 16000, 0.5),
])
async def test_audio_operations_preserve_duration_or_change_it_explicitly(tmp_path, op, opts, rate, duration):
    result = await audio.process(audio_job(op, wav_file(tmp_path), format="wav", **opts))
    params, _ = samples(result)
    assert params.framerate == rate and params.nchannels == 2
    assert params.nframes / rate == pytest.approx(duration, abs=1 / rate)


async def test_volume_fades_and_sample_exact_mute(tmp_path):
    item = wav_file(tmp_path, channels=1)
    original = struct.unpack("<8000h", Path(item["path"]).read_bytes()[44:])
    _, attenuated = samples(await audio.process(audio_job("volume", item, gain_db=-6, format="wav")))
    assert max(attenuated) == pytest.approx(max(original) * 10 ** (-6 / 20), abs=2)
    _, muted = samples(await audio.process(audio_job("silence", item, mode="mute", start_sec=0.101,
                                                         end_sec=0.202, format="wav")))
    assert all(v == 0 for v in muted[1616:3232])
    assert muted[100:1500] == original[100:1500]
    assert muted[3300:] == original[3300:]
    _, faded = samples(await audio.process(audio_job("fade", item, in_sec=0.1, out_sec=0.1, format="wav")))
    assert max(abs(v) for v in faded[:160]) < max(original) * 0.11
    assert max(abs(v) for v in faded[-160:]) < max(original) * 0.11


@pytest.mark.parametrize("mode,count,channels", [("extract", 1, 1), ("split", 2, 1), ("mono", 1, 1), ("stereo", 1, 2)])
async def test_channel_modes(tmp_path, mode, count, channels):
    opts = {"mode": mode, "format": "wav", **({"channel": 1} if mode == "extract" else {})}
    item = wav_file(tmp_path)
    result = await audio.process(audio_job("channels", item, **opts))
    assert len(result["audio"]) == count
    for i in range(count):
        params, values = samples(result, i)
        assert params.nchannels == channels and params.nframes == 8000
        if mode in {"extract", "split"}:
            with wave.open(item["path"]) as wav:
                original = struct.unpack("<16000h", wav.readframes(8000))
            assert values == original[(1 if mode == "extract" else i)::2]


@pytest.mark.parametrize("op,opts,expected", [
    ("concat", {}, 1.0), ("concat", {"gap_sec": 0.1}, 1.1),
    ("concat", {"crossfade_sec": 0.1}, 0.9), ("mix", {"offsets_sec": [0, 0.1]}, 0.6),
    ("mix", {"gains_db": [-6, -6], "normalize": True}, 0.5),
])
async def test_audio_concat_and_mix_with_different_rates_and_channels(tmp_path, op, opts, expected):
    first = wav_file(tmp_path, channels=2)
    second = wav_file(tmp_path, name="second.bin", rate=22050, channels=1)
    result = await audio.process(audio_job(op, [first, second], format="wav", **opts))
    params, _ = samples(result)
    assert params.framerate == 16000 and params.nchannels == 2
    assert params.nframes / params.framerate == pytest.approx(expected, abs=1 / 16000)


async def test_silence_trim_only_edges_keeps_interior_pause(tmp_path):
    item = wav_file(tmp_path, duration=0.6, leading=0.2, trailing=0.3, channels=1)
    with wave.open(item["path"], "rb") as wav:
        params, frames = wav.getparams(), bytearray(wav.readframes(wav.getnframes()))
    frames[round(0.4 * 16000) * 2:round(0.6 * 16000) * 2] = b"\0" * (round(0.2 * 16000) * 2)
    with wave.open(item["path"], "wb") as wav:
        wav.setparams(params)
        wav.writeframes(frames)
    result = await audio.process(audio_job("silence", item, mode="trim", format="wav"))
    params, values = samples(result)
    assert params.nframes / 16000 == pytest.approx(0.6, abs=0.002)
    assert all(v == 0 for v in values[3300:6300])


async def test_silence_trim_keeps_audio_after_a_pause_near_the_end(tmp_path):
    item = wav_file(tmp_path, duration=0.5, channels=1)
    with wave.open(item["path"], "rb") as wav:
        params, frames = wav.getparams(), bytearray(wav.readframes(wav.getnframes()))
    frames[round(0.3 * 16000) * 2:round(0.48 * 16000) * 2] = b"\0" * (round(0.18 * 16000) * 2)
    with wave.open(item["path"], "wb") as wav:
        wav.setparams(params)
        wav.writeframes(frames)
    result = await audio.process(audio_job("silence", item, mode="trim", format="wav"))
    params, values = samples(result)
    assert params.nframes == 8000
    assert any(v != 0 for v in values[-300:])


async def test_loudness_normalization_and_silence_rejection(tmp_path):
    item = wav_file(tmp_path, duration=4, channels=1, amplitude=0.1)
    result = await audio.process(audio_job("normalize", item, lufs=-20, true_peak_db=-1, format="wav"))
    output = tmp_path / "normalized.wav"
    output.write_bytes(base64.b64decode(result["audio"]["data_base64"]))
    _, log = await audio.execute([*audio.ffmpeg(), *audio.input_args(output), "-af",
        "loudnorm=I=-20:TP=-1:LRA=50:print_format=json", "-f", "null", "-"])
    import json
    import re
    report = json.loads(re.findall(r'\{\s*"input_i".*?\}', log, re.S)[-1])
    assert float(report["input_i"]) == pytest.approx(-20, abs=0.3)
    with pytest.raises(MediaError, match="silence|too short"):
        await audio.process(audio_job("normalize", wav_file(tmp_path, name="silent.bin", duration=4, amplitude=0)))


async def test_clipping_is_rejected_or_explicitly_limited(tmp_path):
    item = wav_file(tmp_path, channels=1, amplitude=0.8)
    with pytest.raises(MediaError, match="clip"):
        await audio.process(audio_job("volume", item, gain_db=12, format="wav"))
    result = await audio.process(audio_job("volume", item, gain_db=12, clipping="limit", format="wav"))
    _, values = samples(result)
    assert max(values) <= 32767
    with pytest.raises(MediaError, match="clip"):
        await audio.process(audio_job("mix", [item, item], format="wav"))


async def test_audio_conversion_stream_copy_preserves_samples(tmp_path):
    item = wav_file(tmp_path)
    result = await audio.process(audio_job("convert", item, format="wav"))
    params, values = samples(result)
    with wave.open(item["path"]) as original:
        assert values == struct.unpack("<16000h", original.readframes(8000))
    assert params.framerate == 16000


@pytest.mark.parametrize("op,opts,message", [
    ("crop", {"start_sec": 0.4, "end_sec": 0.2}, "interval"),
    ("crop", {"start_sec": 0, "end_sec": 1}, "interval"),
    ("channels", {"mode": "extract", "channel": 2}, "channel"),
    ("channels", {"mode": "extract"}, "channel"),
    ("fade", {"in_sec": 0.4, "out_sec": 0.2}, "overlap"),
    ("convert", {"format": "opus"}, "48000"),
    ("convert", {"format": "mp3", "bit_depth": "24"}, "bit_depth"),
    ("convert", {"format": "wav", "bitrate_kbps": 128}, "bitrate"),
    ("silence", {"mode": "mute"}, "start_sec"),
])
async def test_audio_invalid_parameters_fail_explicitly(tmp_path, op, opts, message):
    with pytest.raises(MediaError, match=message):
        await audio.process(audio_job(op, wav_file(tmp_path), **opts))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_nonfinite_parameters_are_explicit_errors(tmp_path, value):
    with pytest.raises(MediaError, match="finite"):
        image.process(image_job("rotate", image_file(tmp_path), angle=value))
    with pytest.raises(MediaError, match="finite"):
        await audio.process(audio_job("volume", wav_file(tmp_path), gain_db=value))


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
async def test_nonfinite_float_audio_samples_fail_instead_of_becoming_zero(tmp_path, value):
    data = struct.pack("<8000f", *([0.1] * 4000 + [value] + [0.1] * 3999))
    fmt = struct.pack("<HHIIHH", 3, 1, 16000, 64000, 4, 32)
    path = tmp_path / "invalid-float.wav"
    path.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<I", 16) +
                     fmt + b"data" + struct.pack("<I", len(data)) + data)
    with pytest.raises(MediaError, match="NaN|infinite"):
        await audio.process(audio_job("convert", {"path": str(path)}))


async def test_audio_invalid_content_playlists_truncation_and_limits(tmp_path, monkeypatch):
    path = tmp_path / "not-audio.bin"
    path.write_bytes(b"not audio")
    with pytest.raises(MediaError, match="invalid_media|processing_failed"):
        await audio.process(audio_job("convert", {"path": str(path)}))
    path.write_text("#EXTM3U\nhttp://127.0.0.1/secret.wav\n")
    with pytest.raises(MediaError):
        await audio.process(audio_job("convert", {"path": str(path)}))
    item = wav_file(tmp_path)
    original = Path(item["path"]).read_bytes()
    Path(item["path"]).write_bytes(original[:-123])
    with pytest.raises(MediaError):
        await audio.process(audio_job("convert", item, format="flac"))
    item = wav_file(tmp_path)
    monkeypatch.setattr(audio, "MAX_DURATION", 0.1)
    with pytest.raises(MediaError, match="resource_limit"):
        await audio.process(audio_job("convert", item))
    monkeypatch.setattr(audio, "MAX_DURATION", 3600)
    monkeypatch.setattr(audio, "MAX_BYTES", 100)
    with pytest.raises(MediaError, match="resource_limit"):
        await audio.process(audio_job("convert", item))


async def test_missing_executable_is_an_environment_failure(monkeypatch):
    audio.executable.cache_clear()
    monkeypatch.setenv("MMP_FFMPEG", "does-not-exist-mmp-ffmpeg")
    try:
        with pytest.raises(RuntimeError, match="dependency_unavailable"):
            audio.executable("ffmpeg")
    finally:
        audio.executable.cache_clear()


async def test_cancel_kills_ffmpeg_child(monkeypatch, tmp_path):
    pid = tmp_path / "pid"
    script = tmp_path / "sleeper.py"
    script.write_text("import os, pathlib, sys, time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)\n")
    processes = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(audio.execute([sys.executable, str(script), str(pid)]))
    for _ in range(100):
        if pid.exists():
            break
        await asyncio.sleep(0.02)
    assert pid.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


@pytest.fixture
async def media_stack(tmp_path):
    stack = await build_stack(tmp_path, engines={
        "media_image": {"module": "engines.media_image", "timeout_sec": 10},
        "media_audio": {"module": "engines.media_audio", "timeout_sec": 10},
    }, media__max_inline_result_bytes=1024 * 1024)
    try:
        yield stack
    finally:
        await stack.client.aclose()
        await stack.link.close()
        await stack.node.close()


async def test_real_engine_image_cache_media_ref_and_put(media_stack, tmp_path, media_server):
    item = image_file(tmp_path)
    request = {"type": "image.resize", "arguments": {"image": inline(Path(item["path"]).read_bytes()),
        "options": {"width": 6, "height": 6}}, "wait": 5}
    first = (await media_stack.submit(**request)).json()
    assert first["status"] == "done", first
    assert first["outputs"]["metadata"]["value"]["width"] == 6
    cached = (await media_stack.submit(**request)).json()
    assert cached["status"] == "done" and cached["cached"] and cached["usage"]["compute_ms"] == 0
    done = (await media_stack.submit(type="image.crop", arguments={
        "image": {"ref": first["outputs"]["image"]["media_id"]},
        "options": {"x": 1, "y": 0, "width": 3, "height": 2}},
        delivery={"image": media_server.put_endpoint("/crop.png")}, wait=5)).json()
    assert done["status"] == "done", done
    with Image.open(io.BytesIO(media_server.puts["/crop.png"])) as im:
        assert im.size == (3, 2)
    assert done["outputs"]["image"]["delivered"]


async def test_real_audio_split_array_and_cached_continuation(media_stack, tmp_path):
    item = wav_file(tmp_path)
    result = (await media_stack.submit(type="audio.channels", arguments={
        "audio": inline(Path(item["path"]).read_bytes()), "options": {"mode": "split"}}, wait=5)).json()
    assert result["status"] == "done", result
    assert len(result["outputs"]["audio"]) == 2
    handles = [{"ref": output["media_id"]} for output in result["outputs"]["audio"]]
    done = (await media_stack.submit(type="audio.concat", arguments={"audio": handles}, wait=5)).json()
    assert done["status"] == "done", done
    assert done["outputs"]["metadata"]["value"]["files"][0]["duration_sec"] == pytest.approx(1)


async def test_large_selected_split_references_then_batch_delivery(media_stack, tmp_path, media_server):
    media_stack.node.cfg.media.max_inline_result_bytes = 16
    item = wav_file(tmp_path, channels=3)
    job = {"type": "audio.channels", "arguments": {
        "audio": inline(Path(item["path"]).read_bytes()),
        "options": {"mode": "split", "channels": [2, 0], "format": "wav"}}, "wait": 5}
    for cached in [False, True]:
        done = (await media_stack.submit(**job)).json()
        assert done["status"] == "done" and done["cached"] is cached, done
        assert [f["source_channel"] for f in done["outputs"]["metadata"]["value"]["files"]] == [2, 0]
        outputs = done["outputs"]["audio"]
        assert len(outputs) == 2
        assert all("inline" not in output and "delivered" not in output for output in outputs)
    # Select only one of the produced files for export; the remaining output stays on A.
    selected = outputs[:1]
    delivered = (await media_stack.submit(type="media.deliver", node_id=media_stack.node.node_id,
        arguments={"media": [{"ref": output["media_id"]} for output in selected]},
        delivery={"media": [media_server.put_endpoint("/selected.wav")]}, wait=5)).json()
    assert delivered["status"] == "done", delivered
    assert delivered["outputs"]["media"][0]["media_id"] == selected[0]["media_id"]
    with wave.open(io.BytesIO(media_server.puts["/selected.wav"])) as result:
        assert result.getnchannels() == 1
        actual = struct.unpack(f"<{result.getnframes()}h", result.readframes(result.getnframes()))
    with wave.open(item["path"]) as original:
        source = struct.unpack(f"<{original.getnframes() * 3}h", original.readframes(original.getnframes()))
    assert actual == source[2::3]
    # Existing direct PUT remains available for callers that know their output count.
    direct = (await media_stack.submit(**{**job, "delivery": {"audio": [
        media_server.put_endpoint("/two.wav"), media_server.put_endpoint("/zero.wav")]}})).json()
    assert direct["status"] == "done" and direct["cached"] is True
    assert all(output["delivered"] for output in direct["outputs"]["audio"])


@pytest.mark.parametrize("channels", [[], [0, 0], [3], [-1], [True]])
async def test_split_rejects_invalid_selection(tmp_path, channels):
    with pytest.raises(MediaError, match="channels"):
        await audio.process(audio_job("channels", wav_file(tmp_path, channels=3),
                                      mode="split", channels=channels))


async def test_real_invalid_media_fails_and_engine_recovers(media_stack, tmp_path):
    bad = (await media_stack.submit(type="image.convert", arguments={"image": inline(b"not-image")}, wait=5)).json()
    assert bad["status"] == "failed" and bad["error"] == "bad_request", bad
    assert "unsupported_format" in bad["message"]
    item = image_file(tmp_path)
    good = (await media_stack.submit(type="image.convert", arguments={
        "image": inline(Path(item["path"]).read_bytes())}, wait=5)).json()
    assert good["status"] == "done", good
    bad = (await media_stack.submit(type="audio.convert", arguments={"audio": inline(b"not-audio")}, wait=5)).json()
    assert bad["status"] == "failed" and bad["error"] == "bad_request", bad
    invalid = await media_stack.submit(type="image.crop", arguments={"image": inline(b"not-fetched"),
        "options": {"x": -1, "y": 0, "width": 2, "height": 2}}, wait=5)
    assert invalid.status_code == 400
