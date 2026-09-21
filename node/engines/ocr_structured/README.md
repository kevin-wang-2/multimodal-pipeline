# ocr.structured 引擎

文档 OCR，两档一个子进程（同一 conda 环境）：

| tier | 引擎 | 产出 | VRAM 预算 | 并发 |
|---|---|---|---|---|
| `gpu-fast` | PP-OCRv6（paddleocr 3.7.0） | 文本行 + 框 + 每页质量信号 / 升级建议 | 2 GiB | 2（GPU 上仍由锁串行，并发只用于解码） |
| `gpu` | PaddleOCR-VL 1.6（0.9B） | Markdown + 版面元素（`text / table / seal / image / figure_title …`） | 12 GiB | 1 |

调度器按 tier 的 `vram_mb` 记账：16 GiB 卡上快慢同时在跑放得下，两个慢档必然排队（S5 冒烟验证：第二个 VL 任务 `queued`，不是 OOM）。

## 升级不在引擎内自动发生

快档每页算两个信号（T5 §4.2 第一版），写进 `pages[].quality` 与 `pages[].flags`，值得升级的页列在 `suggest_upgrade_pages`：

| flag | 含义 | 参数（默认） |
|---|---|---|
| `low_confidence` | 低置信行占比过高 | `low_conf_threshold` 0.7、`low_conf_ratio_max` 0.2 |
| `coverage_anomaly` | 检测框覆盖的**文字墨迹**占比过低——有字没认出来（T5 §2.4 旋转 3° 静默丢内容的探测器）。框线 / 分隔线先被去掉，不算文字墨迹 | `coverage_min` 0.45 |
| `empty` | 有墨迹却一行没认出 | — |

上层决定是否再提一个 `tier: "gpu"` + `params.pages: [...]` 的任务（T5 §5.4：MVP 只做按需升级；铁律 2：预检只路由）。这样 VRAM 才能按 tier 记账，快档任务也不会突然占 12 GiB。

`coverage_min` 校准（lab，2026-09-21）：正立发票 0.56–0.64；旋转 3° 且 PP-OCRv6 一个字没丢时 0.47（框线不再是直线，被算成文字墨迹）；真丢 60% 内容（T5 的 Tesseract 案例）应远低于 0.45。探测器对几何扰动敏感、宁多升级。

## 输入

PDF（pypdfium2 按 `dpi` 栅格化，默认 200）、PNG / JPEG / TIFF（多帧）/ WEBP，按魔数判类型。`params.pages` 选页（1-based）。结果超过 A 的内联上限时走 `media.put`（S5 冒烟里把上限压到 4 KB 逼它走 PUT）。

## 环境（GPU 机，Windows + conda）

```bat
python node\engines\ocr_structured\setup_env.py create --env ocrlab --wheels C:\mmp\wheels
python node\engines\ocr_structured\setup_env.py verify --env ocrlab
```

`create` 把 T5 §6 的坑写死在脚本里：paddlepaddle-gpu 3.4.0 (cu129) 与 8 个 nvidia-* 轮子用 **curl 分片并行**从清华镜像下载（pip 会读 Windows 系统代理卡死）、`nvjitlink` 显式钉版本（METADATA 里没版本号）、`--no-index --find-links` 离线装、再装 `paddleocr==3.7.0` + `paddlex[ocr]==3.7.2`（VL 要 `[ocr]` 不是 `[ocr-core]`）、最后用空白图触发模型下载（`PADDLE_PDX_MODEL_SOURCE=modelscope`）。

验证状态：`verify` 对 lab 的 `ocrlab` 环境通过；`create` 的索引解析在两个源上都能定位到正确 wheel；**没有做过一次全新重装计时**（2.3 GB）。

模型缓存默认在 `~/.paddlex`；以别的账号跑 A（比如 CI runner 的服务账号）时用 `PADDLE_PDX_CACHE_HOME` 指过去，否则会重新下载 2 GB。

## 接到 A

```toml
[engines.ocr_structured]
module = "engines.ocr_structured"
python = "C:/ProgramData/miniconda3/envs/ocrlab/python.exe"
timeout_sec = 240
env = { PADDLE_PDX_MODEL_SOURCE = "modelscope", PADDLE_PDX_CACHE_HOME = "C:/Users/Admin/.paddlex", MMP_OCR_PRELOAD_VL = "1" }   # VL 冷加载 ~76s 是启动成本；不想常驻就设 0

[scheduler]
vram_total_mb = 16303
```

## 冒烟

`node/tools/smoke_ocr_structured.py`：T5 的 3 张发票快档字段（发票号码 / 开票日期 / 价税合计 / 税额，口径同 T5 `run_inv.py`；GT 的价税合计从大写金额换算）、结果走 PUT、VL 并发 2 排队、批量 OCR 排队时交互式 `triage.audio` 仍按时完成、旋转 3° 校准。发票素材只在 GPU 机上（`MMP_OCR_INVOICES`），不进仓库。
