# multimodal-pipeline

给**纯文本主模型**的 agent 补一层多模态感知。

媒体附件进来 → 一组小模型自动产出**带时间轴的结构化 digest**（原子事实 + 缺口 + 可用能力清单）→ 上层 agent 自己判断信息够不够、自己挑更贵的能力（扒谱 / 指纹 / 分轨 / 结构化 OCR / VLM……）。agent 永远只消费文本。

一句话：**把"无时间轴的单工具转写"升级成"带时间轴、可扩展、有预算意识的多工具感知服务"。**

## 为什么

- 便宜的纯文本主模型（如 DeepSeek 系）听不到音频、看不到图。
- 平台自带的转写只给整段糊在一起的文本：没有时间轴、没有事件类型、非语音段会被幻觉成语音（[实测](docs/结果-T1.md)）。
- "每张图 / 每段音频都调一次外部多模态模型"的 agentic loop 太慢太贵，上下文会被描述文本撑爆。
- 这些痛点在任何纯文本主模型的 agent 平台上都一样，所以解决方案不绑平台。

## 架构：三个角色

```
A 算力节点 ──(主动出站 ws：注册能力 / 心跳 / 收请求)──▶ B 协调节点 ◀──(任务 API)── C 调用节点
  模型是 A 的插件                                    无状态：注册表 + 转发           agent 侧客户端包
  队列 / 缓存 / 引擎池都在 A                          可与 A 或 C 同进程
```

| 角色 | 职责 | 实现 |
|---|---|---|
| **A 算力节点** | 执行任务：队列、缓存、引擎池、任务类型注册表。**零公网监听**，只有一条主动出站的 ws | Python |
| **B 协调节点** | 接受 A 的注册与心跳，接受 C 的任务请求，按能力挑 A 转发，透传结果。**无状态** | 一套协议，两个实现：Python（与 A 绑定）、TypeScript（与 C 绑定或独立） |
| **C 调用节点** | agent 侧适配层：提交任务、等结果、注入 digest、兜底 | TypeScript 包 |

B 是**角色**不是部署单元：内网可达的 C 直连"A/B 绑定"的 B；公网上的 agent 宿主在自己进程里跑"B/C 绑定"的 B，A 主动连过去。独立 B 节点只在多 A 多 C 互不相识时才需要。

预检（自动触发）和能力（agent 显式调用）是**同一套任务系统**里的不同任务类型，共用队列、缓存、媒体句柄和结果格式。

详见 [docs/架构.md](docs/架构.md)。

## 设计铁律

1. **信息分层 / 预算阶梯**：便宜的永远可用，贵的按需解锁。
2. **预检只产出任务无关的原子事实**，不猜意图。"够不够"归上层 agent。
3. **覆盖全时长**：用户意图可能藏在另一个时间段里。
4. **放弃猜意图就必须全面 + 诚实**：digest 主动暴露 gaps、失败的工具、低置信度段。
5. **服务不承诺可用性，兜底归调用方**。服务只保证失败可见、结果标来源。
6. **原始媒体引用必须保留**，它是所有升级路径的弹药库。
7. **核心逻辑与平台解耦**，hook / 插件都是适配层。
8. **格式转换用确定性模板**，不用 LLM 总结。
9. **接口通用，实现专用**：调用方可见的契约不编码网络拓扑、存储厂商或机器名；实现层可以放心专用。

## 状态

- [x] **M0 手工验证**（2026-09-10）：VAD 定界 → 段级打标 → ASR → 组装 digest → 隔离模型实例盲测，**选对工具、指对时间段**；digest schema 收敛。
- [x] 文档 / 图像腿实测（2026-09-17）：三档 OCR 跑通，"快档预扫 + 按页升级"成立，runner 必须排队。
- [ ] **M1 infra 骨架**：A 的队列 / 缓存 / 引擎池 + 出站 ws；B 协议 + py/ts 两个实现；C 的 ts 包。只挂两个任务类型：`triage.audio`（CPU）与 `ocr.structured`（GPU）。
- [ ] M2 能力层（工具注册表 + 首批 head）
- [ ] M3 预算与反馈升级
- [ ] M4 模态同构（图片 / 视频复用同一套抽象）

## 已实测的关键数字

| 项 | 结果 | 出处 |
|---|---|---|
| 音频便宜档（CPU） | SenseVoiceSmall int8 + Silero VAD，M1 Mac 上 **21× 实时**，模型合计约 270MB，零 torch，中文词全对 | [T2](docs/结果-T2.md) |
| 音频 GPU 档 | whisper.cpp 原生 sm_120 构建，**21.8× 实时**；模型加载 0.9s 占短音频总时七成 | [T1](docs/结果-T1.md) |
| 边界检测 | Silero VAD 切点误差 10ms，7ms / 4.4s | [T1](docs/结果-T1.md) / [T3](docs/结果-T3.md) |
| 端到端盲测 | 只凭 digest 选对 `pitch_transcribe` 并指对 0.00–2.18s | [T4](docs/结果-T4.md) |
| 文档 OCR 快档（GPU） | PP-OCRv6 **0.4–0.5 s/页**，CER 约 1% | [T5](docs/结果-T5-文档OCR.md) |
| 文档 OCR 慢档（GPU） | PaddleOCR-VL 0.9B **约 9 s/页、12GB 显存**，出版面结构与印章 | [T5](docs/结果-T5-文档OCR.md) |

⚠️ 音频结论全部建立在**合成素材**（TTS 语音 + 正弦哼唱）上，真实音频复验待做。

## 仓库结构

```
docs/
  架构.md              设计文档（公开版）
  M0-测试计划.md        M0 四组实验的计划
  结果-T1.md … T5      每组实验的原始记录（负结果也记）
  结果-网络测试.md      一次网络吞吐排查的复盘（教训：别用 ssh 管道测网速）
tools/
  make_test_clip.py    生成 M0 合成素材（2s 哼唱 + 2s 口述）
  t2_sensevoice.py     VAD + SenseVoice 基准
  t3_aed_routing.py    滑窗打标做时序路由
  t4_build_digest.py   手工串三件套、组装 digest（M0 用）
  tcpbench.py          零依赖原生 TCP 吞吐测量
  nettest.sh           走 ssh 的快速对比（有失真，见脚本头部警告）
testdata/              合成素材（不入库，用 make_test_clip.py 生成）
models/                模型权重（不入库，见下）
```

## 复现 M0 实验

```bash
python3 -m venv .venv && .venv/bin/pip install sherpa-onnx numpy
python3 tools/make_test_clip.py            # macOS：借 say 生成语音段；其他平台需自备中文语音
```

模型放到 `models/`（均为 ONNX，无需 torch）：

| 模型 | 来源 |
|---|---|
| `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/` | Hugging Face `csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17` |
| `sherpa-onnx-zipformer-small-audio-tagging-2024-04-15/` | sherpa-onnx GitHub releases（audio-tagging-models） |
| `silero_vad.onnx` | sherpa-onnx GitHub releases（asr-models） |

```bash
.venv/bin/python tools/t2_sensevoice.py            # T2
.venv/bin/python tools/t3_aed_routing.py           # T3
.venv/bin/python tools/t4_build_digest.py          # T4：打印 digest JSON
```

## 托管

主仓库托管在自建 Gitea（CI runner 挂在 GPU 机上，方便跑模型相关的测试），GitHub 为只读镜像。Issue 与 PR 请到主仓库。

## 许可

[Apache-2.0](LICENSE)。

## 记录约定

- 每组实验写 `docs/结果-T{n}.md`，附原始命令与输出摘要。
- **负结果同样是结论**。
- 设计变更先改 [docs/架构.md](docs/架构.md)，再改代码。
