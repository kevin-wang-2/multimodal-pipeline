"""triage.image：纯组装规则、注册和内部分阶段兜底。"""
from __future__ import annotations

import base64
import io
import sys

from mmp_node.engine_io import EngineResult
from mmp_node.schemas import DIGEST_ID

from conftest import NODE_DIR, build_stack, validator

sys.path.insert(0, str(NODE_DIR))
from engines.triage_image import CAPABILITIES  # noqa: E402
from engines.triage_image.digest import RawRegion, build_digest  # noqa: E402
from engines.triage_image.runner import _surfaces  # noqa: E402

V_DIGEST = validator(DIGEST_ID)
MID = "sha256:" + "1" * 64
SURFACES = [{"id": "image_0", "kind": "image", "index": 0, "width_px": 100,
             "height_px": 80, "rotation_deg": 0}]


def _source(d: dict) -> dict:
    d["source"] = {"tier": "gpu-fast", "engine": "florence-2-base-ft", "engine_version": "1",
                   "generated_at": "2026-09-29T00:00:00Z", "degraded": False}
    return d


def test_image_digest_dedupes_regions_and_marks_unread_text():
    raw = [RawRegion("image_0", "contract text", [5, 5, 60, 40], "phrase_grounding"),
           RawRegion("image_0", "contract text", [5, 5, 60, 40], "object_detection"),
           RawRegion("image_0", "person", [65, 4, 95, 75], "object_detection")]
    digest, retry = build_digest(MID, "image/png", SURFACES, ["A person holds a contract."], raw, 0, .8, {})
    assert retry is None
    assert len(digest["regions"]) == 2
    assert digest["regions"][0]["provenance"]["tools"] == ["phrase_grounding", "object_detection"]
    assert digest["gaps"] == ["图片中的文字内容未读取"]
    assert not list(V_DIGEST.iter_errors(_source(digest)))


def test_image_digest_quality_failure_requests_objective_retry():
    digest, retry = build_digest(MID, "image/jpeg", SURFACES, [""], [], 0, .8, {})
    assert retry == "description_nonempty"
    assert digest["tools"]["visual_caption"] == "failed"
    assert "视觉描述失败，图片内容未知" in digest["gaps"]
    src = {"tier": "gpu", "engine": "florence-2-large-ft", "engine_version": "1",
           "generated_at": "2026-09-29T00:00:00Z", "degraded": True,
           "degraded_reason": "partial_failure"}
    digest["source"] = src
    assert not list(V_DIGEST.iter_errors(digest))


def test_capability_only_exposes_base_tier():
    assert len(CAPABILITIES) == 1
    assert CAPABILITIES[0]["id"] == "triage.image"
    assert [t["tier"] for t in CAPABILITIES[0]["tiers"]] == ["gpu-fast"]


def test_multiframe_tiff_becomes_page_surfaces(tmp_path):
    from PIL import Image
    path = tmp_path / "two-pages.tiff"
    Image.new("RGB", (20, 10), "white").save(path, save_all=True,
                                               append_images=[Image.new("RGB", (30, 15), "black")])
    kind, items = _surfaces(str(path), 144)
    assert kind == "tiff"
    assert [s["id"] for _, s in items] == ["page_0", "page_1"]
    assert [(s["width_px"], s["height_px"]) for _, s in items] == [(20, 10), (30, 15)]


async def test_internal_large_stage_records_actual_source_and_cache(tmp_path):
    engines = {
        "triage_image": {"module": "engines.triage_image", "timeout_sec": 5,
                         "resident_vram_mb": 512},
        "triage_image_large": {"module": "engines.triage_image_large", "timeout_sec": 5,
                               "resident_vram_mb": 1536, "run_vram_mb": 2048},
    }
    stack = await build_stack(tmp_path, engines=engines, scheduler__vram_total_mb=4096)
    calls = []

    async def fake_run(spec, job, timeout, vram):
        calls.append((spec.name, vram))
        if spec.name == "triage_image":
            return EngineResult(None, {"caption": 1}, {"engine": "triage_image_large",
                                                       "reason": "description_nonempty"})
        digest, _ = build_digest(job["media_id"], "image/png", SURFACES, ["一张测试图片"],
                                 [RawRegion("image_0", "object", [1, 1, 20, 20], "phrase_grounding")], 0, .8, {})
        return EngineResult(digest, {"caption": 2})

    stack.node.pool.run = fake_run
    media = {"inline": base64.b64encode(b"not-decoded-by-fake-engine").decode()}
    first = await stack.submit(type="triage.image", media=media, wait=5)
    body = first.json()
    assert first.status_code == 200 and body["status"] == "done", body
    assert calls == [("triage_image", 768), ("triage_image_large", 2048)]
    assert body["result"]["source"]["tier"] == "gpu"
    assert body["result"]["source"]["engine"] == "florence-2-large-ft"
    assert body["result"]["source"]["params"]["fallback_reason"] == "description_nonempty"
    second = await stack.submit(type="triage.image", media=media, wait=5)
    assert second.json()["cached"] is True
    assert second.json()["result"]["source"] == body["result"]["source"]
    await stack.client.aclose(); await stack.link.close(); await stack.node.close()
