#!/usr/bin/env python3
"""真模型冒烟（CI gpu job / 本机）：起 A + 进程内 B，把 M0 素材过一遍 triage.audio，检查 S2 验收项。

用法：python tools/smoke_triage_audio.py [wav ...]
环境：MMP_MODELS_DIR（模型目录）、MMP_ENGINE_PYTHON（引擎解释器，默认当前）、MMP_SMOKE_T2_MS（26s 素材 T2 基线，默认 1248）、
      MMP_SMOKE_DUMP（目录；给了就把每条 digest 写成 <目录>/<wav 名>.digest.json）
退出码非零 = 验收不过。输出每条媒体的 digest 摘要与 timings。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

NODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE_DIR))

from mmp_broker.core import Broker  # noqa: E402
from mmp_broker.inproc import InProcLink  # noqa: E402
from mmp_node.config import Config  # noqa: E402
from mmp_node.node import Node  # noqa: E402
from mmp_node import schemas  # noqa: E402

KEY = "smoke-node-key-0123456789"
T2_26S_MS = int(os.environ.get("MMP_SMOKE_T2_MS", "1248"))   # 结果-T2.md：26.3s 素材 VAD+ASR 1248 ms（不含加载）


def cfg(tmp: Path) -> Config:
    eng = {"module": "engines.triage_audio", "timeout_sec": 120}
    if os.environ.get("MMP_ENGINE_PYTHON"):
        eng["python"] = os.environ["MMP_ENGINE_PYTHON"]
    return Config.model_validate({
        "node": {"id": "smoke", "key": KEY, "data_dir": str(tmp)},
        "scheduler": {"engine_start_timeout_sec": 300},
        "engines": {"triage_audio": eng},
    })


async def main(wavs: list[Path]) -> int:
    tmp = NODE_DIR / ".smoke-cache"
    node = Node(cfg(tmp), NODE_DIR)
    await node.start()
    broker = Broker(KEY)
    link = InProcLink(node, broker)
    await link.connect()
    failures: list[str] = []
    v = schemas.validator_for_ref(schemas.DIGEST_ID)
    try:
        for wav in wavs:
            data = wav.read_bytes()
            t0 = time.monotonic()
            status, body = await broker.submit({"type": "triage.audio", "media": {"inline": base64.b64encode(data).decode()}, "wait": 120})
            wall = int((time.monotonic() - t0) * 1000)
            print(f"\n=== {wav.name} → HTTP {status} ({wall} ms wall, cached={body.get('cached')})")
            if status != 200:
                print(json.dumps(body, ensure_ascii=False, indent=2)); failures.append(f"{wav.name}: HTTP {status}"); continue
            d = body["result"]
            if os.environ.get("MMP_SMOKE_DUMP"):
                out = Path(os.environ["MMP_SMOKE_DUMP"]) / (wav.stem + ".digest.json")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            errs = schemas.errors(v, d)
            if errs:
                failures.append(f"{wav.name}: digest schema: {errs[:3]}")
            for s in d["segments"]:
                asr = s.get("asr")
                print(f"  [{s['start']:7.3f}, {s['end']:7.3f})  {s['label_status']:12s} {[(l['tag'], l['score']) for l in s['labels']][:2]}"
                      f"  {'' if asr is None else 'asr=' + repr(asr.get('text'))}")
            for g in d["gaps"]:
                print("  gap:", g)
            print("  tools:", d["tools"], " timings_ms:", body.get("timings_ms"), " degraded:", d["source"]["degraded"])
            # ---- 验收 ----
            speech = [s for s in d["segments"] if s.get("asr") is not None]
            if wav.name == "m0_hum_then_speech.wav":
                if len(d["segments"]) != 2 or d["segments"][0]["asr"] is not None or not speech:
                    failures.append("m0: expected [non-speech, speech]")
                else:
                    cut = speech[0]["start"]
                    if abs(cut - 2.0) > 0.05:
                        failures.append(f"m0: VAD cut {cut} not within 2.00±0.05")
                    if len(d["gaps"]) < 2 or not any("结构事实" in g for g in d["gaps"]):
                        failures.append("m0: gaps missing structure fact")
                    if "谱子" not in (speech[0]["asr"]["text"] or ""):
                        failures.append(f"m0: asr text unexpected: {speech[0]['asr']['text']!r}")
            if wav.name == "m0_long_26s.wav":
                t = body.get("timings_ms", {})
                infer = t.get("vad", 0) + t.get("tagging", 0) + t.get("asr", 0)
                print(f"  26s: vad+tagging+asr = {infer} ms (limit {int(T2_26S_MS * 1.5)} = 1.5× T2)")
                if infer > T2_26S_MS * 1.5:
                    failures.append(f"26s: {infer} ms > 1.5× T2 ({T2_26S_MS})")
                if len(speech) != 6:
                    failures.append(f"26s: expected 6 speech segments, got {len(speech)}")
            # 第二次提交必须命中缓存
            status2, body2 = await broker.submit({"type": "triage.audio", "media": {"inline": base64.b64encode(data).decode()}, "wait": 30})
            if not (status2 == 200 and body2.get("cached") is True):
                failures.append(f"{wav.name}: second submit not cached")
    finally:
        await link.close()
        await node.close()
    print("\n" + ("SMOKE FAILED:\n  - " + "\n  - ".join(failures) if failures else "SMOKE OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    args = [Path(a) for a in sys.argv[1:]]
    if not args:
        td = Path(os.environ.get("MMP_SMOKE_WAVS") or NODE_DIR.parent / "testdata")
        args = [td / "m0_hum_then_speech.wav", td / "m0_long_26s.wav"]
    sys.exit(asyncio.run(main(args)))
