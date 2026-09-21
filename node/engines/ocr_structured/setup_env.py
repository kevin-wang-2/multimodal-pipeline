#!/usr/bin/env python3
"""GPU 机上建 ocr.structured 的引擎环境（Windows + conda），把 T5 §6 的四个坑写成脚本。只依赖标准库 + curl + conda。

    python setup_env.py create   [--env ocrlab] [--wheels C:\\mmp\\wheels]   # 建 env + 装 paddlepaddle-gpu(离线) + paddleocr/paddlex + 拉模型
    python setup_env.py verify   [--env ocrlab]                              # 只验证：CUDA 可用、版本正确、模型在缓存里（PADDLE_PDX_CACHE_HOME 或 ~/.paddlex）

坑位（结果-T5.md §6）：
  1. pip 在 Windows 会读注册表里的系统代理，几百 MB 的 nvidia 轮子必卡死 → 用 curl 分片并行从清华镜像下载，再 `pip --no-index --find-links` 离线装
  2. nvidia-nvjitlink-cu12 在 cufft / cusolver / cusparse 的 METADATA 里不带版本号，pip 解析会报 Could not find a version → 显式钉版本一起下载
  3. paddle CPU + oneDNN 会炸 → 引擎只跑 GPU，不需要 FLAGS_use_mkldnn；这里不改
  4. VL 管线要 paddlex[ocr] 而不是 paddlex[ocr-core]；模型走 ModelScope（PADDLE_PDX_MODEL_SOURCE=modelscope）
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import re
import subprocess
import sys
import zipfile

TUNA = "https://pypi.tuna.tsinghua.edu.cn"
PADDLE_INDEX = "https://www.paddlepaddle.org.cn/packages/stable/cu129"
PY_TAG = "cp312"
PADDLE_GPU = ("paddlepaddle-gpu", "3.4.0")
# 与 lab 上验证过的 ocrlab 环境一致（pip freeze 2026-09-21）
NVIDIA = {
    "nvidia-cublas-cu12": "12.9.0.13", "nvidia-cuda-runtime-cu12": "12.9.37", "nvidia-cudnn-cu12": "9.9.0.52",
    "nvidia-cufft-cu12": "11.4.0.6", "nvidia-curand-cu12": "10.3.10.19", "nvidia-cusolver-cu12": "11.7.4.40",
    "nvidia-cusparse-cu12": "12.5.9.5", "nvidia-nvjitlink-cu12": "12.9.86",
}
PIP_PKGS = ["paddleocr==3.7.0", "paddlex[ocr]==3.7.2", "pypdfium2>=5", "pillow>=10", "numpy>=1.26"]
CHUNKS = 6


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def fetch(url: str) -> str:
    return sh(["curl", "-sL", "--max-time", "60", url]).stdout


def wheel_url(index: str, pkg: str, ver: str) -> str | None:
    page = fetch(f"{index}/simple/{pkg}/" if "tuna" in index else f"{index}/{pkg}/")
    mod = pkg.replace("-", "_")
    for m in re.finditer(r'href=["\']?([^"\'\s>]+\.whl)', page):
        u = m.group(1).split("#")[0]
        f = u.rsplit("/", 1)[-1]
        if f.startswith(f"{mod}-{ver}-") and "win_amd64" in f and (f"-{PY_TAG}-" in f or "py3-none" in f):
            if u.startswith("../../"):
                u = f"{index}/{u[6:]}"
            elif u.startswith("/"):
                u = index.split("/", 3)[0] + "//" + index.split("/", 3)[2] + u
            elif not u.startswith("http"):
                u = f"{index}/{pkg}/{u}"
            return u
    return None


def size_of(url: str) -> int:
    out = sh(["curl", "-sIL", "--max-time", "40", url]).stdout
    sizes = re.findall(r"[Cc]ontent-[Ll]ength:\s*(\d+)", out)
    return int(sizes[-1]) if sizes else 0


def download(url: str, dest: str) -> None:
    """curl 分片并行：pip 走系统代理会卡死，curl 不读注册表代理。"""
    size = size_of(url)
    if os.path.exists(dest) and size and os.path.getsize(dest) == size:
        print(f"[have] {os.path.basename(dest)} ({size >> 20} MB)", flush=True)
        return
    print(f"[get ] {os.path.basename(dest)} ({size >> 20} MB, {CHUNKS} conns)", flush=True)
    if not size:
        sh(["curl", "-sL", "--retry", "5", "-o", dest, url])
        return
    step = size // CHUNKS

    def part(i: int) -> None:
        s, e = i * step, (size - 1 if i == CHUNKS - 1 else (i + 1) * step - 1)
        sh(["curl", "-sL", "--max-time", "5400", "--retry", "5", "--retry-delay", "2", "-r", f"{s}-{e}", "-o", f"{dest}.part{i}", url])

    with cf.ThreadPoolExecutor(CHUNKS) as ex:
        list(ex.map(part, range(CHUNKS)))
    with open(dest, "wb") as w:
        for i in range(CHUNKS):
            with open(f"{dest}.part{i}", "rb") as r:
                w.write(r.read())
            os.remove(f"{dest}.part{i}")
    if os.path.getsize(dest) != size:
        sys.exit(f"size mismatch for {dest}")


def env_python(env: str) -> str:
    r = sh(["conda", "run", "-n", env, "python", "-c", "import sys; print(sys.executable)"])
    p = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if not p:
        sys.exit(f"conda env {env} not found ({r.stderr.strip()[:200]})")
    return p


def create(env: str, wheels: str) -> None:
    if not env_exists(env):
        print(f"[conda] create {env} (conda-forge, python 3.12)", flush=True)
        r = sh(["conda", "create", "-y", "-q", "--override-channels", "-c", "conda-forge", "-n", env, "python=3.12"])
        if r.returncode:
            sys.exit(r.stderr[-1500:])
    py = env_python(env)
    os.makedirs(wheels, exist_ok=True)
    # 1) paddlepaddle-gpu + nvidia-*：curl 下载，离线装（坑 1、2）
    url = wheel_url(PADDLE_INDEX, *PADDLE_GPU)
    if not url:
        sys.exit("paddlepaddle-gpu wheel not found on paddle index")
    download(url, os.path.join(wheels, url.rsplit("/", 1)[-1]))
    for pkg, ver in NVIDIA.items():
        u = wheel_url(TUNA, pkg, ver)
        if not u:
            sys.exit(f"{pkg}=={ver} not on mirror")
        download(u, os.path.join(wheels, u.rsplit("/", 1)[-1]))
    print("[pip] offline install paddlepaddle-gpu", flush=True)
    r = sh([py, "-m", "pip", "install", "--proxy", "", "--no-index", f"--find-links={wheels}", f"{PADDLE_GPU[0]}=={PADDLE_GPU[1]}"])
    if r.returncode:
        sys.exit(r.stdout[-1500:] + r.stderr[-1500:])
    # 2) paddleocr / paddlex[ocr]（坑 4）：小包走正常索引即可
    print("[pip] paddleocr / paddlex[ocr]", flush=True)
    r = sh([py, "-m", "pip", "install", "--proxy", "", "-i", f"{TUNA}/simple", *PIP_PKGS])
    if r.returncode:
        sys.exit(r.stdout[-1500:] + r.stderr[-1500:])
    # 3) 拉模型：用一张空白图触发 PP-OCRv6 与 PaddleOCR-VL 下载（ModelScope）
    print("[models] warm up (downloads PP-OCRv6 / PaddleOCR-VL into ~/.paddlex)", flush=True)
    warm = ("import os,numpy as np;os.environ['PADDLE_PDX_MODEL_SOURCE']='modelscope';"
            "from paddleocr import PaddleOCR,PaddleOCRVL;img=np.full((64,256,3),255,np.uint8);"
            "PaddleOCR(lang='ch',use_doc_orientation_classify=False,use_doc_unwarping=False,use_textline_orientation=False).predict(img);"
            "PaddleOCRVL(use_seal_recognition=True,use_chart_recognition=True,merge_layout_blocks=True).predict(img);print('models ok')")
    r = sh([py, "-c", warm])
    print(r.stdout[-600:], r.stderr[-600:])
    verify(env)


def env_exists(env: str) -> bool:
    return env in [line.split()[0] for line in sh(["conda", "env", "list"]).stdout.splitlines() if line and not line.startswith("#")]


def verify(env: str) -> None:
    py = env_python(env)
    code = ("import paddle,paddleocr,paddlex,pypdfium2,PIL;"
            "print('paddle',paddle.__version__,'cuda',paddle.device.is_compiled_with_cuda(),'paddleocr',paddleocr.__version__,'paddlex',paddlex.__version__)")
    r = sh([py, "-c", code])
    print(r.stdout.strip() or r.stderr[-800:])
    cache = os.path.join(os.environ.get("PADDLE_PDX_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".paddlex"), "official_models")
    have = sorted(os.listdir(cache)) if os.path.isdir(cache) else []
    need = ["PP-OCRv6_medium_det", "PP-OCRv6_medium_rec", "PP-DocLayoutV3", "PaddleOCR-VL-1.6"]
    missing = [n for n in need if n not in have]
    print(f"models cached: {have}\nmissing: {missing or 'none'}")
    ok = "cuda True" in r.stdout and "paddleocr 3.7.0" in r.stdout and not missing
    print("VERIFY", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["create", "verify"])
    ap.add_argument("--env", default="ocrlab")
    ap.add_argument("--wheels", default=os.path.join("C:\\mmp", "wheels") if os.name == "nt" else "/tmp/mmp-wheels")
    a = ap.parse_args()
    create(a.env, a.wheels) if a.cmd == "create" else verify(a.env)
