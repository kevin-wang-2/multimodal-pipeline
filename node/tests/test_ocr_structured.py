"""ocr.structured：质量信号 / 页加载的单测（不碰 paddle）；真模型在 lab 上由 tools/smoke_ocr_structured.py 跑。"""
from __future__ import annotations

import io
import sys

import numpy as np
import pytest

from conftest import NODE_DIR, validator

sys.path.insert(0, str(NODE_DIR))
from engines.ocr_structured import CAPABILITIES, OUTPUT_SCHEMA  # noqa: E402
from engines.ocr_structured.pages import UnsupportedDocument, load_pages, sniff  # noqa: E402
from engines.ocr_structured.quality import boxes_mask, flags_for, page_quality  # noqa: E402

PIL = pytest.importorskip("PIL.Image")


def test_capability_is_valid_and_declares_vram():
    v = validator("urn:mmp:protocol:1:capability")
    errs = list(v.iter_errors(CAPABILITIES[0]))
    assert not errs, [e.message for e in errs]
    tiers = {t["tier"]: t for t in CAPABILITIES[0]["tiers"]}
    assert tiers["gpu"]["vram_mb"] == 12288 and tiers["gpu"]["max_concurrency"] == 1   # T5：VL 单卡并发只能是 1
    assert tiers["gpu-fast"]["vram_mb"] + tiers["gpu"]["vram_mb"] <= 16303              # 快慢同时在卡上放得下


def _page(h=200, w=300):
    """白纸黑字的合成页：两条"文本行"，每行是 15px 字块 + 5px 间隙、高 8px（要像文字，不能是实心长条——那会被当成框线）。"""
    gray = np.full((h, w), 255, dtype=np.uint8)
    for x in range(20, 280, 20):
        gray[46:54, x:x + 15] = 0
    for x in range(20, 200, 20):
        gray[126:134, x:x + 15] = 0
    return gray


def test_quality_full_coverage_no_flags():
    gray = _page()
    boxes = [[[20, 40], [280, 40], [280, 60], [20, 60]], [[20, 120], [200, 120], [200, 140], [20, 140]]]
    q = page_quality(gray, ["第一行", "第二行"], [0.95, 0.9], boxes, 0.7)
    assert q["lines"] == 2 and q["chars"] == 6 and q["coverage"] == 1.0 and q["low_conf_ratio"] == 0.0
    assert flags_for(q, 0.2, 0.5) == []


def test_quality_detects_silent_content_loss():
    """T5 §2.4：旋转 3° 后文字量从 1914 掉到 721 且不报错——检测框只盖住一部分墨迹。"""
    gray = _page()
    boxes = [[[20, 40], [280, 40], [280, 60], [20, 60]]]   # 第二行没检出来
    q = page_quality(gray, ["第一行"], [0.95], boxes, 0.7)
    assert 0.5 < q["coverage"] < 0.7
    assert flags_for(q, 0.2, 0.7) == ["coverage_anomaly"]
    assert flags_for(q, 0.2, 0.5) == []          # 阈值是参数


def test_quality_low_confidence_and_empty():
    gray = _page()
    boxes = [[[20, 40], [280, 40], [280, 60], [20, 60]], [[20, 120], [200, 120], [200, 140], [20, 140]]]
    q = page_quality(gray, ["a", "b"], [0.95, 0.3], boxes, 0.7)
    assert q["low_conf_ratio"] == 0.5 and flags_for(q, 0.2, 0.5) == ["low_confidence"]
    q = page_quality(gray, [], [], [], 0.7)
    assert flags_for(q, 0.2, 0.5) == ["empty"]                       # 有墨迹却零行
    blank = page_quality(np.full((50, 50), 255, np.uint8), [], [], [], 0.7)
    assert flags_for(blank, 0.2, 0.5) == []                          # 空白页不算异常


def test_rules_are_not_text_ink():
    """发票 / 表单：框线、分隔线是墨迹但不是文字，不能压低覆盖率（lab 上 3 张发票原本 0.39–0.53 → 误报）。"""
    from engines.ocr_structured.quality import rule_mask
    gray = np.full((600, 400), 255, dtype=np.uint8)
    for x in range(20, 380, 20):          # 一行"文字"：15px 的字块、5px 间隙，高 8px
        gray[100:108, x:x + 15] = 0
    gray[300, 10:390] = 0                 # 横框线
    gray[50:550, 200] = 0                 # 竖框线
    ink = gray < 128
    rules = rule_mask(ink)
    assert rules[300, 100] and rules[400, 200]
    assert not rules[104, 25]             # 字块不是框线
    q = page_quality(gray, ["一行文字"], [0.9], [[[20, 100], [380, 100], [380, 108], [20, 108]]], 0.7)
    assert q["coverage"] == 1.0           # 框线的墨迹没被算成"漏认"


def test_boxes_mask_clips_to_page():
    m = boxes_mask((10, 10), [[[-5, -5], [50, -5], [50, 3], [-5, 3]]])
    assert m[:3, :].all() and not m[3:, :].any()


def test_load_pages_png_tiff_and_sniff(tmp_path):
    img = PIL.new("RGB", (30, 20), "white")
    png = tmp_path / "a.png"; img.save(png)
    n, pages = load_pages(str(png), None, 200)
    assert n == 1 and pages[0].index == 1 and pages[0].image.size == (30, 20)
    tiff = tmp_path / "m.tiff"
    PIL.new("L", (10, 10), 255).save(tiff, save_all=True, append_images=[PIL.new("L", (10, 10), 0), PIL.new("L", (10, 10), 128)])
    n, pages = load_pages(str(tiff), [2, 3, 9], 200)
    assert n == 3 and [p.index for p in pages] == [2, 3]
    with pytest.raises(UnsupportedDocument):
        sniff(b"ID3\x03garbage")
    assert sniff(b"%PDF-1.7\n") == "pdf"


def test_output_schema_accepts_engine_shape():
    v = validator.__globals__["Draft202012Validator"](OUTPUT_SCHEMA)
    ok = {"page_count": 2, "suggest_upgrade_pages": [2], "pages": [
        {"page": 1, "tier": "gpu-fast", "text": "x", "lines": [{"text": "x", "score": 0.9, "box": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
         "quality": {"lines": 1, "chars": 1, "coverage": 1.0}, "flags": []},
        {"page": 2, "tier": "gpu", "text": "# t", "markdown": "# t", "elements": [{"type": "table", "text": "|a|", "box": [0, 0, 1, 1]}],
         "quality": {"lines": 1, "chars": 3}, "flags": []}]}
    assert not list(v.iter_errors(ok))
