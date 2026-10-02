# triage.audio 引擎

音频预检：Silero VAD 定界 → 段级 AudioSet 打标（Zipformer int8）→ 语音段 ASR（SenseVoiceSmall int8）→ 确定性模板组装 digest。tier `cpu`，零 torch。

## 环境（与 A 进程隔离）

```bash
# conda（conda-forge，不触发 Anaconda 默认频道的 ToS）
conda create -y --override-channels -c conda-forge -n mmp-audio python=3.12
<env>/python -m pip install -r node/engines/triage_audio/requirements.txt
# 或 venv：python3.12 -m venv .venv-audio && .venv-audio/bin/pip install -r node/engines/triage_audio/requirements.txt
```

模型三件套放到一个目录（来源见仓库 README「复现 M0 实验」），用环境变量 `MMP_MODELS_DIR` 指过去（默认仓库根 `models/`）：

```
<MMP_MODELS_DIR>/
  silero_vad.onnx
  sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/{model.int8.onnx, tokens.txt}
  sherpa-onnx-zipformer-small-audio-tagging-2024-04-15/{model.int8.onnx, class_labels_indices.csv}
```

## 接到 A

```toml
[engines.triage_audio]
module = "engines.triage_audio"
python = "/path/to/mmp-audio/python"     # 引擎解释器；A 自己的解释器可以没有 sherpa
timeout_sec = 120
env = { MMP_MODELS_DIR = "/path/to/models", MMP_ENGINE_THREADS = "4" }
```

启动即加载三件套并用 1s 静音预热（onnxruntime 首次推理有几百 ms 一次性开销），加载完才发 `hello`。

## params

| 名 | 默认 | 说明 |
|---|---|---|
| `label_confidence_threshold` | 0.30 | 段级标签低于此 → `labels: []`、`label_status: unclassified`，gaps 里写"无法判定类别" |
| `language` | `auto` | SenseVoice 语言提示：auto / zh / en / ja / ko / yue |

## VAD 参数（引擎内部，`vad.py`）

双阈值：起点 0.20、终点 0.50、32 ms 帧、起点回补一帧、最短语音 / 静音 0.25 s。为什么不用 sherpa 的封装、实测数字见 [docs/结果-T2.md](../../../docs/结果-T2.md) 的 2026-09-20 复测。

## 真实音频后加的两条规则（[结果-T6.md](../../../docs/结果-T6.md) 样本 #1）

- 语音段 `asr.confidence` = 打标 `Speech` 分数：喷麦 / 呼吸 / 口哨起音被 VAD 判成语音后，SenseVoice 会幻觉出一两个词；这些段 Speech 分数为 0，低于阈值进 gaps，文本保留不删。
- 不合并短间隔的语音段（`merge_gap_sec` 默认 0）：合并在打标前发生，会抹掉夹在两句话之间的一声口哨。改由 gaps 点出"夹在两段语音之间的短非语音（时长、标签）"。

## 边界

- 引擎本身只接 PCM WAV（8/16/24/32 bit，任意采样率与声道数，线性插值重采到 16k）；m4a / mp3 / ogg / flac / amr 等由 A 用 ffmpeg 转码后送进来（`mmp_node/transcode.py`），A 没有 ffmpeg 时 → `400 bad_request`。
- 引擎不决定 `capabilities_available`；A 在响应阶段按当前注册表、原始 MIME 与本 digest 的标签/锚点派生。
- 工具部分失败不中断：`tools` 里标 `failed` / `partial`，gaps 里说明，A 把 `source.degraded` 置 true。

## 冒烟

```bash
MMP_MODELS_DIR=... MMP_ENGINE_PYTHON=<env>/python python node/tools/smoke_triage_audio.py [wav ...]
```
默认跑 `testdata/` 的两段 M0 素材，检查：段结构、切点 2.00 ± 0.05 s、gaps 含结构事实、26 s 素材推理 ≤ 1.5 × T2、二次提交命中缓存。CI 的 `gpu` job 就是它。
