"""M1 §1 第 3、4 条的实机验收（在枢纽机上跑，扮演公网宿主）。只用标准库。
用法：python3 m1_checks.py <api_key> <media_token> <phase>   phase ∈ cache | queue
"""
import base64, hashlib, json, os, sys, time, urllib.request, concurrent.futures as cf
BASE, KEY, TOKEN, PHASE = "https://mmp.seanartech.com", sys.argv[1], sys.argv[2], sys.argv[3]
MEDIA = f"{BASE}/_media/{TOKEN}"
IN = os.path.expanduser(f"~/mmp/media/{TOKEN}/in")
def call(method, path, body=None, timeout=180):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {KEY}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"raw": raw[:200].decode(errors="replace")}
def wait_done(job_id, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, b = call("GET", f"/jobs/{job_id}?wait=30")
        if b.get("status") in ("done", "failed", "cancelled"): return st, b
    raise SystemExit(f"{job_id} timeout")
def inline(b: bytes, ct): return {"inline": base64.b64encode(b).decode(), "content_type": ct}
def variant(path, salt: bytes):
    """同一张图追加几个字节 → 新的 media_id（PNG 解码器忽略 IEND 后的尾巴），避开历史缓存。"""
    b = open(path, "rb").read() + salt
    name = f"v_{hashlib.sha256(b).hexdigest()[:8]}{os.path.splitext(path)[1]}"
    open(os.path.join(IN, name), "wb").write(b)
    return b, name
png = os.path.join(IN, "synth_doc.png"); wav = os.path.join(IN, "m0_hum_then_speech.wav")

if PHASE == "cache":
    # 第 3 条：同一份媒体经内联与经 URL 提交，命中同一条缓存
    b, name = variant(png, b"m1-cache-" + os.urandom(4))
    sha = "sha256:" + hashlib.sha256(b).hexdigest()
    t0 = time.time(); st1, r1 = call("POST", "/jobs", {"type": "ocr.structured", "media": inline(b, "image/png"), "wait": 60})
    if st1 == 202: st1, r1 = wait_done(r1["job_id"])
    w1 = time.time() - t0
    t0 = time.time(); st2, r2 = call("POST", "/jobs", {"type": "ocr.structured", "media": {"get": {"url": f"{MEDIA}/in/{name}"}}, "wait": 60})
    if st2 == 202: st2, r2 = wait_done(r2["job_id"])
    w2 = time.time() - t0
    print(f"inline: HTTP {st1} media_id={r1.get('media_id')} cached={r1.get('cached')} wall={w1:.1f}s timings={r1.get('timings_ms')}")
    print(f"get   : HTTP {st2} media_id={r2.get('media_id')} cached={r2.get('cached')} wall={w2:.1f}s timings={r2.get('timings_ms')}")
    ok = st1 == st2 == 200 and r1["media_id"] == r2["media_id"] == sha and r1["cached"] is False and r2["cached"] is True
    print("本地 sha256 一致:", r1.get("media_id") == sha)
    print("CACHE CHECK", "OK" if ok else "FAILED")

elif PHASE == "queue":
    # 第 4 条：批量 OCR 排队中，交互式预检仍被调度；队列超限 429
    N = int(os.environ.get("N", "230"))
    # 基线：空队列时的交互式预检耗时
    wb, wname = variant(wav, b"m1-base-" + os.urandom(4))
    t0 = time.time(); st, r = call("POST", "/jobs", {"type": "triage.audio", "media": inline(wb, "audio/wav"), "priority": "interactive", "wait": 60})
    if st == 202: st, r = wait_done(r["job_id"])
    base_wall = time.time() - t0
    print(f"基线 interactive triage.audio（空队列）: HTTP {st} wall={base_wall:.1f}s timings={r.get('timings_ms')}")
    # 灌批量 OCR：媒体只内联传一次，之后全部 ref + 不同 params → 不同缓存键、零载荷（第一版用 230 个 100 KB 内联并发提交，
    # 跨境链路把 submit 的 ack 拖过 wait+5s，B 判节点无响应把链路踢掉——见 结果-M1.md 的发现）
    pb, _ = variant(png, b"m1-batch-" + os.urandom(4))
    st, r = call("POST", "/jobs", {"type": "ocr.structured", "media": inline(pb, "image/png"), "priority": "batch", "wait": 60})
    if st == 202: st, r = wait_done(r["job_id"])
    mid = r["media_id"]; print(f"批量素材入库: HTTP {st} media_id={mid}")
    salt = int.from_bytes(os.urandom(2), "big") % 400
    def submit(i): return call("POST", "/jobs", {"type": "ocr.structured", "media": {"ref": mid}, "priority": "batch", "wait": 0,
                                                 "params": {"low_conf_threshold": round(0.3 + (salt + i) * 0.001, 3)}}, timeout=60)
    t0 = time.time()
    with cf.ThreadPoolExecutor(16) as ex: rs = list(ex.map(submit, range(N)))
    codes = {}
    for st, r in rs: codes[st] = codes.get(st, 0) + 1
    accepted = [r["job_id"] for st, r in rs if st == 202]
    r429 = [r for st, r in rs if st == 429]
    print(f"批量提交 {N} 个 ocr.structured(batch) 用时 {time.time()-t0:.1f}s：状态码分布 {codes}")
    if r429: print("   429 样例:", json.dumps(r429[0], ensure_ascii=False)[:200])
    st, h = call("GET", "/health"); print("   /health:", json.dumps(h, ensure_ascii=False)[:200])
    # 队列满时提交交互式预检
    wb2, _ = variant(wav, b"m1-inter-" + os.urandom(4))
    t0 = time.time(); st, r = call("POST", "/jobs", {"type": "triage.audio", "media": inline(wb2, "audio/wav"), "priority": "interactive", "wait": 60})
    if st == 202: st, r = wait_done(r["job_id"])
    inter_wall = time.time() - t0
    print(f"队列有 {len(accepted)} 个批量任务时 interactive triage.audio: HTTP {st} status={r.get('status')} wall={inter_wall:.1f}s timings={r.get('timings_ms')} queue_position={r.get('queue_position')}")
    # 看一眼批量任务的推进，然后取消剩余
    st, q = call("GET", f"/jobs/{accepted[-1]}"); print(f"   最后一个批量任务此刻: status={q.get('status')} queue_position={q.get('queue_position')} eta_sec={q.get('eta_sec')}")
    t0 = time.time()
    with cf.ThreadPoolExecutor(16) as ex: cs = list(ex.map(lambda j: call("DELETE", f"/jobs/{j}", timeout=60), accepted))
    ccodes = {}
    for st, _ in cs: ccodes[st] = ccodes.get(st, 0) + 1
    print(f"取消剩余批量任务 {len(accepted)} 个用时 {time.time()-t0:.1f}s：状态码分布 {ccodes}")
    st, h = call("GET", "/health"); print("   /health:", json.dumps(h, ensure_ascii=False)[:200])
    ok = st == 200 and len(r429) > 0 and inter_wall < max(3 * base_wall, base_wall + 5)
    print("QUEUE CHECK", "OK" if ok else "FAILED", f"(interactive {inter_wall:.1f}s vs 基线 {base_wall:.1f}s；429 {len(r429)} 个)")
