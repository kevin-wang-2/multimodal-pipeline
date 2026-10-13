from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import build_stack
from engines.qwen_image_internal import CAPABILITIES, ENGINE, GENERATE_OPTIONS_SCHEMA
from engines.qwen_image_internal.runner import request
from engines.qwen_image_internal.setup_env import (
    FORBIDDEN_FILES,
    MODEL_FILES,
    MODEL_SHA256,
    PYTORCH_PACKAGES,
    download,
)


def test_qwen_capabilities_are_separate_internal_generation_and_editing():
    assert [cap["id"] for cap in CAPABILITIES] == ["image.generate.qwen", "image.edit.qwen"]
    assert all(cap["visibility"] == "internal" for cap in CAPABILITIES)
    assert all(cap["tiers"][0]["engine"] == ENGINE for cap in CAPABILITIES)
    edit_image = CAPABILITIES[1]["input"]["fields"][0]
    assert edit_image["kind"] == "media" and edit_image["accepts"] == ["image/*"]


def test_qwen_generation_preserves_width_height_interface_and_area_limit():
    mode, prompt, image, params = request({
        "task_type": "image.generate.qwen",
        "arguments": {"prompt": "portrait", "options": {"width": 768, "height": 1344, "seed": 7}},
    })
    assert (mode, prompt, image) == ("generate", "portrait", None)
    assert params == {"width": 768, "height": 1344, "seed": 7}
    assert set(GENERATE_OPTIONS_SCHEMA["properties"]) == {"width", "height", "seed"}
    with pytest.raises(ValueError, match="1048576"):
        request({"task_type": "image.generate.qwen", "arguments": {
            "prompt": "too large", "options": {"width": 1536, "height": 1536}}})


def test_qwen_edit_accepts_resolved_media_path(tmp_path: Path):
    source = tmp_path / "source.png"
    source.write_bytes(b"png")
    mode, prompt, image, params = request({
        "task_type": "image.edit.qwen",
        "arguments": {"image": {"path": str(source), "media_id": "sha256:" + "a" * 64},
                      "prompt": "add a necklace"},
    })
    assert (mode, prompt, image) == ("edit", "add a necklace", source)
    assert params == {"resolution": 1024, "seed": 42}


async def test_qwen_and_krea_internal_capabilities_coexist_but_remote_registration_hides_both(tmp_path):
    stack = await build_stack(tmp_path, engines={
        "image_generate_internal": {"module": "engines.image_generate_internal", "timeout_sec": 30},
        "qwen_image_internal": {"module": "engines.qwen_image_internal", "timeout_sec": 30},
    }, scheduler__vram_total_mb=16303)
    try:
        bound = (await stack.client.get("/capabilities")).json()["capabilities"]
        assert {item["capability"]["id"] for item in bound} == {
            "image.generate.internal", "image.generate.qwen", "image.edit.qwen",
        }
        assert stack.node.register_message()["payload"]["capabilities"] == []
        assert stack.node.register_message()["payload"]["engine_versions"] == {}
    finally:
        await stack.client.aclose()
        await stack.link.close()
        await stack.node.close()


def test_setup_installs_only_pinned_turbo_int8_dependencies():
    assert MODEL_FILES == (
        "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
        "text_encoders/qwen3vl_8b_int8_convrot.safetensors",
        "vae/qwen_image_2.1_vae_bf16.safetensors",
    )
    assert all(name not in MODEL_FILES for name in FORBIDDEN_FILES)
    assert set(MODEL_SHA256) == set(MODEL_FILES)
    assert all(len(digest) == 64 for digest in MODEL_SHA256.values())
    assert PYTORCH_PACKAGES == (("torch", "2.14.1"), ("torchvision", "0.29.1"))


def test_setup_download_uses_native_curl_with_resume(tmp_path: Path):
    destination = tmp_path / "model.safetensors"
    partial = tmp_path / "model.safetensors.partial"
    partial.write_bytes(b"complete")
    with patch("engines.qwen_image_internal.setup_env.subprocess.run") as run:
        download("https://example.invalid/model", destination)
    command = run.call_args.args[0]
    assert command[:2] == ["curl.exe", "--fail"]
    assert command[command.index("--continue-at") + 1] == "-"
    assert destination.read_bytes() == b"complete"
    assert not partial.exists()
