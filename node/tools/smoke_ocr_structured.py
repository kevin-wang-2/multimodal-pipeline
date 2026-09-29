#!/usr/bin/env python3
"""ocr.structured 真模型冒烟（GPU 机 / CI gpu job）。起 A + 进程内 B，两个引擎（ocr_structured、triage_audio），检查 S5 验收：

1. T5 的发票（MMP_OCR_INVOICES 下每个子目录：p-001.png + _gt.txt）快档字段全对：发票号码 / 开票日期 / 价税合计 / 合计金额
2. 结果 PUT 回句柄：把内联上限压到很小，结果必须走 result_ref，PUT 上来的内容过 output.schema
3. VL（tier=gpu）并发 2 提交：第二个排队而不是 OOM，两个都完成且有 markdown / elements
4. M1 定义第 4 条：批量 OCR 排队中，交互式 triage.audio 仍按时完成
5. 升级触发：每页有 flags / suggest_upgrade_pages 字段（信号是否触发取决于素材，只检查存在与类型）

环境：MMP_OCR_PYTHON（ocrlab 解释器）、MMP_OCR_INVOICES、MMP_ENGINE_PYTHON + MMP_MODELS_DIR + MMP_SMOKE_WAVS（音频引擎，第 4 项用；缺则跳过第 4 项）
      MMP_SMOKE_DUMP（可选，导出结果 JSON）。退出码非零 = 验收不过。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE_DIR))

from jsonschema import Draft202012Validator  # noqa: E402

from engines.ocr_structured import OUTPUT_SCHEMA  # noqa: E402
from mmp_broker.core import Broker  # noqa: E402
from mmp_broker.inproc import InProcLink  # noqa: E402
from mmp_node.config import Config  # noqa: E402
from mmp_node.node import Node  # noqa: E402

KEY = "smoke-node-key-0123456789"
FIELDS = {   # 与 T5 run_inv.py（report.json 的 3/3 就是它算的）同一套正则与口径：只删空格保留换行
    "发票号码": [r"发票号码[:：]?\s*([0-9]{15,25})", r"\b(\d{20})\b"],
    "开票日期": [r"开票日期[:：]?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"],
    # 顺序要紧：GT 文本层里标签与数值分离，"价税合计…¥" 的窗口会抓到合计金额；"圆整¥x" 才是可靠锚
    "价税合计": [r"[（(]\s*小写\s*[)）]\s*[¥￥]?\s*([\d,]+\.\d{2})", r"圆整\s*(?:[（(]\s*小写\s*[)）])?\s*[¥￥]?\s*([\d,]+\.\d{2})", r"价税合计[^¥￥]{0,20}[¥￥]\s*([\d,]+\.\d{2})"],
    "税额": [r"税\s*额\s*[¥￥]?\s*([\d,]+\.\d{2})", r"[¥￥]\s*[\d,]+\.\d{2}\s*[¥￥]\s*([\d,]+\.\d{2})"],
}


CN_DIGITS = {c: i for i, c in enumerate("零壹贰叁肆伍陆柒捌玖")}
CN_UNITS = {"拾": 10, "佰": 100, "仟": 1000, "万": 10_000, "亿": 100_000_000}


def cn_upper_to_amount(s: str) -> str | None:
    """大写金额 → "598.00"。GT 文本层里数值顺序不可靠，大写金额是价税合计唯一稳的锚（T5 report 里第三张就是被顺序坑了）。"""
    m = re.search(r"([零壹贰叁肆伍陆柒捌玖拾佰仟万亿]+)[圆元]([零壹贰叁肆伍陆柒捌玖角分]*)整?", s)
    if not m:
        return None
    total, section, num = 0, 0, 0
    for ch in m.group(1):
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch in ("万", "亿"):
            total = (total + section + num) * CN_UNITS[ch]
            section = num = 0
        else:
            section += (num or 1) * CN_UNITS[ch]
            num = 0
    yuan = total + section + num
    tail = m.group(2)
    jiao = CN_DIGITS[tail[tail.index("角") - 1]] if "角" in tail else 0
    fen = CN_DIGITS[tail[tail.index("分") - 1]] if "分" in tail else 0
    return f"{yuan}.{jiao}{fen}"


def gt_field(name: str, gt: str):
    if name == "价税合计":
        return cn_upper_to_amount(re.sub(r"\s+", "", gt))
    return find(FIELDS[name], gt)


def find(pats: list[str], text: str):
    t = re.sub(r"[ \t\u3000]+", "", text)
    for pat in pats:
        m = re.search(pat, t)
        if m:
            return "-".join(g for g in m.groups() if g)
    return None


class _Put(BaseHTTPRequestHandler):
    puts: dict[str, bytes] = {}

    def log_message(self, *a):
        pass

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", "0"))
        _Put.puts[self.path] = self.rfile.read(n)
        self.send_response(201); self.end_headers()


def cfg(tmp: Path, inline_limit: int) -> Config:
    engines = {"ocr_structured": {"module": "engines.ocr_structured", "timeout_sec": 240, "python": os.environ["MMP_OCR_PYTHON"],
                                  "env": {"PADDLE_PDX_MODEL_SOURCE": "modelscope"}}}
    if os.environ.get("MMP_ENGINE_PYTHON") and os.environ.get("MMP_MODELS_DIR"):
        engines["triage_audio"] = {"module": "engines.triage_audio", "timeout_sec": 120, "python": os.environ["MMP_ENGINE_PYTHON"],
                                   "env": {"MMP_MODELS_DIR": os.environ["MMP_MODELS_DIR"]}}
    return Config.model_validate({
        "node": {"id": "smoke-ocr", "key": KEY, "data_dir": str(tmp)},
        "scheduler": {"vram_total_mb": 16303, "engine_start_timeout_sec": 900, "idle_unload_sec": 3600},
        "media": {"max_inline_result_bytes": inline_limit, "use_env_proxy": False},
        "engines": engines,
    })


def inline(path: Path) -> dict:
    return {"inline": base64.b64encode(path.read_bytes()).decode()}


async def main() -> int:
    failures: list[str] = []
    dump = Path(os.environ["MMP_SMOKE_DUMP"]) if os.environ.get("MMP_SMOKE_DUMP") else None
    inv_root = Path(os.environ["MMP_OCR_INVOICES"])
    invoices = sorted(d for d in inv_root.iterdir() if d.is_dir() and (d / "p-001.png").exists() and (d / "_gt.txt").exists())
    if len(invoices) < 3:
        print(f"need >=3 invoices under {inv_root}, found {len(invoices)}"); return 1

    node = Node(cfg(NODE_DIR / ".smoke-cache-ocr", inline_limit=4096), NODE_DIR)
    await node.start()
    broker = Broker(KEY)
    link = InProcLink(node, broker)
    await link.connect()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Put)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    put_base = f"http://127.0.0.1:{httpd.server_address[1]}"
    vout = Draft202012Validator(OUTPUT_SCHEMA)

    put_paths: dict[str, str] = {}
    n_put = [0]

    async def submit(**job):
        # 所有提交都给 put 端点：内联上限压到 4 KB，结果必然走 result_ref，顺带验"结果 PUT 回句柄"
        n_put[0] += 1
        path = f"/out/{n_put[0]}.json"
        job["media"] = {**job["media"], "put": {"url": put_base + path}}
        st, body = await broker.submit(job)
        if "job_id" in body:
            put_paths[body["job_id"]] = path
        return st, body

    async def finish(job_id: str, timeout: float = 600) -> tuple[int, dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st, body = await broker.get(job_id, 5)
            if body.get("status") in ("done", "failed", "cancelled") or "error" in body and "status" not in body:
                return st, body
        raise AssertionError(f"{job_id} did not finish in {timeout}s")

    def fetched_result(body: dict) -> dict:
        if "result" in body:
            return body["result"]
        ref = body["result_ref"]
        blob = _Put.puts[urlpath_of(body)]
        assert len(blob) == ref["bytes"], "PUT size mismatch"
        return json.loads(blob)

    def urlpath_of(body: dict) -> str:
        return put_paths[body["job_id"]]

    try:
        # ---- 1 + 2：发票快档字段（结果强制走 PUT）----
        t_first = time.monotonic()
        first_job_ids: list[str] = []
        for d in invoices:
            st, body = await submit(type="ocr.structured", media=inline(d / "p-001.png"), params={"lang": "ch"}, wait=120)
            wall = int((time.monotonic() - t_first) * 1000)
            print(f"\n=== {d.name} → HTTP {st} ({wall} ms wall)")
            if st != 200:
                print(json.dumps(body, ensure_ascii=False)[:800]); failures.append(f"{d.name}: HTTP {st}"); continue
            first_job_ids.append(body["job_id"])
            if "result_ref" not in body:
                failures.append(f"{d.name}: expected result_ref (inline limit 4096), got inline result")
            res = fetched_result(body)
            errs = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in vout.iter_errors(res)]
            if errs:
                failures.append(f"{d.name}: output schema: {errs[:3]}")
            page = res["pages"][0]
            print(f"  tier={page['tier']} lines={page['quality']['lines']} mean_score={page['quality'].get('mean_score')} "
                  f"coverage={page['quality'].get('coverage')} flags={page['flags']} suggest={res['suggest_upgrade_pages']} timings={body.get('timings_ms')}")
            gt = (d / "_gt.txt").read_text(encoding="utf-8", errors="ignore")
            for name, pat in FIELDS.items():
                a, b = gt_field(name, gt), find(pat, page["text"])
                ok = a is not None and a == b
                print(f"  {name:6s} gt={a!s:>22}  ocr={b!s:>22}  {'OK' if ok else 'FAIL'}")
                if not ok:
                    failures.append(f"{d.name}: field {name} gt={a} ocr={b}")
            if dump:
                dump.mkdir(parents=True, exist_ok=True)
                (dump / f"{d.name}.fast.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

        # ---- 2b：覆盖率探测器校准：把第一张发票旋转 3°（T5 §2.4 Tesseract 在这上面静默丢了 62% 的字）----
        print("\n=== coverage detector calibration: invoice 1 rotated 3 degrees")
        from io import BytesIO
        from PIL import Image
        rot = Image.open(invoices[0] / "p-001.png").convert("RGB").rotate(3, resample=Image.BICUBIC, expand=True, fillcolor="white")
        buf = BytesIO(); rot.save(buf, format="PNG")
        st, body = await submit(type="ocr.structured", media={"inline": base64.b64encode(buf.getvalue()).decode()}, wait=120)
        if st == 200:
            page = fetched_result(body)["pages"][0]
            base_chars = json.loads(_Put.puts[put_paths[first_job_ids[0]]])["pages"][0]["quality"]["chars"]
            ratio = page["quality"]["chars"] / max(1, base_chars)
            print(f"  chars {page['quality']['chars']} vs upright {base_chars} ({ratio:.2f}), mean_score={page['quality'].get('mean_score')} "
                  f"coverage={page['quality'].get('coverage')} flags={page['flags']}")
            if ratio < 0.6 and not page["flags"]:
                failures.append(f"rotated page lost {1 - ratio:.0%} of chars silently and no flag fired")
            # 校准数据（2026-09-21，lab）：正立发票 coverage 0.56–0.64；旋转 3° 无字符丢失时 0.47（框线不再是直线，被算作文字墨迹）。
            # 默认 coverage_min=0.45 落在"无损几何扰动"与"真丢内容"之间；真丢 60% 时应远低于 0.45。
        else:
            failures.append(f"rotated page: HTTP {st}")

        # ---- 3：VL 并发 2 → 第二个排队 ----
        print("\n=== VL tier=gpu, 2 concurrent submits")
        a, b = invoices[0], invoices[1]
        (sa, ba), (sb, bb) = await asyncio.gather(
            submit(type="ocr.structured", media=inline(a / "p-001.png"), tier="gpu", wait=0),
            submit(type="ocr.structured", media=inline(b / "p-001.png"), tier="gpu", wait=0))
        print(f"  first: {sa} {ba.get('status')}  second: {sb} {bb.get('status')} queue_position={bb.get('queue_position')}")
        if not (sa == 202 and sb == 202):
            failures.append(f"VL submits: {sa} {sb}")
        await asyncio.sleep(0.5)
        s1, b1 = await broker.get(ba["job_id"]); s2, b2 = await broker.get(bb["job_id"])
        states = sorted([b1.get("status"), b2.get("status")])
        print(f"  after 0.5s: {states}")
        if states not in (["queued", "running"], ["done", "running"], ["done", "queued"]):
            failures.append(f"VL concurrency: expected one running one queued, got {states}")
        for jid, name in ((ba["job_id"], a.name), (bb["job_id"], b.name)):
            st, body = await finish(jid)
            if st != 200:
                failures.append(f"VL {name}: HTTP {st} {json.dumps(body, ensure_ascii=False)[:300]}"); continue
            res = fetched_result(body)
            page = res["pages"][0]
            print(f"  {name}: tier={page['tier']} elements={len(page.get('elements', []))} md_chars={len(page.get('markdown', ''))} "
                  f"types={sorted(set(e['type'] for e in page.get('elements', [])))} timings={body.get('timings_ms')} degraded={body['source']['degraded']}")
            if page["tier"] != "gpu" or not page.get("markdown"):
                failures.append(f"VL {name}: no markdown / wrong tier")
            if dump:
                (dump / f"{name}.vl.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

        # ---- 4：批量 OCR 排队中，交互式预检仍按时完成 ----
        wavs = Path(os.environ.get("MMP_SMOKE_WAVS", "")) if os.environ.get("MMP_SMOKE_WAVS") else None
        if "triage_audio" in node.registry.engines and wavs and (wavs / "m0_hum_then_speech.wav").exists():
            print("\n=== batch OCR queue vs interactive triage.audio")
            batch = []
            for i in range(6):
                d = invoices[i % len(invoices)]
                st, body = await submit(type="ocr.structured", media=inline(d / "p-001.png"), params={"dpi": 200 + i}, priority="batch")
                batch.append(body["job_id"])
            t0 = time.monotonic()
            st, body = await submit(type="triage.audio", media=inline(wavs / "m0_hum_then_speech.wav"), wait=60)
            tri = time.monotonic() - t0
            pending = 0
            for j in batch:
                if (await broker.get(j))[1].get("status") in ("queued", "running"):
                    pending += 1
            print(f"  triage.audio: HTTP {st} in {tri:.2f}s while {pending}/6 batch OCR still pending")
            if st != 200 or tri > 15:
                failures.append(f"interactive triage took {tri:.1f}s / HTTP {st} under batch load")
            if pending == 0:
                print("  (note) batch queue drained before triage finished — load too light to prove scheduling, not a failure")
            for j in batch:
                st, _ = await finish(j)
                if st != 200:
                    failures.append(f"batch job {j}: HTTP {st}")
        else:
            print("\n(skip) batch-vs-interactive: triage_audio engine or M0 wavs not configured")
    finally:
        httpd.shutdown()
        await link.close()
        await node.close()
    print("\n" + ("SMOKE FAILED:\n  - " + "\n  - ".join(failures) if failures else "SMOKE OK"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
