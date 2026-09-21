"""第 6 条前半：把一个 batch 任务排在队里并 wait=60 挂着，外部此时停掉 A → 期望在途请求收到 503 node_offline，而不是等满 60s。"""
import json, sys, time, urllib.request, concurrent.futures as cf
BASE, KEY, MID = "https://mmp.seanartech.com", sys.argv[1], sys.argv[2]
def call(method, path, body=None, timeout=180):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {KEY}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def sub(i): return call("POST", "/jobs", {"type": "ocr.structured", "media": {"ref": MID}, "priority": "batch", "wait": 0, "params": {"low_conf_threshold": round(0.8 + i * 0.001, 3)}}, timeout=60)
with cf.ThreadPoolExecutor(8) as ex: rs = list(ex.map(sub, range(60)))
print("垫底批量任务:", {st: sum(1 for s, _ in rs if s == st) for st, _ in rs}, flush=True)
t0 = time.time()
st, r = call("POST", "/jobs", {"type": "ocr.structured", "media": {"ref": MID}, "priority": "batch", "wait": 60, "params": {"low_conf_threshold": 0.999}}, timeout=120)
print(f"在途 submit(wait=60) 返回: HTTP {st} {json.dumps(r, ensure_ascii=False)[:160]} 经过 {time.time()-t0:.1f}s（{time.strftime('%H:%M:%S')}）", flush=True)
