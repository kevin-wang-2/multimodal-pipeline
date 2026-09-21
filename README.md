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
- [x] **T6 真实音频复验**（2026-09-21，10 段 + 盲测 10/10）：口哨 `Whistling 0.95`、纯语音 / 英文 / 中英混 / 压在音乐上的话 ASR 基本逐字对；VAD 假阳性与漏检都靠打标识破并写进 gaps；A 侧 ffmpeg 转码。见 [docs/结果-T6.md](docs/结果-T6.md)。
- [x] **M1 infra 骨架**（2026-09-21）：A 的队列 / 缓存 / 引擎池 + 出站 ws；B 协议 + py/ts 两个实现；C 的 ts 包 + OpenClaw 适配。只挂两个任务类型：`triage.audio`（CPU）与 `ocr.structured`（GPU）。七条完成定义实机全过：[docs/结果-M1.md](docs/结果-M1.md)；步骤见 [docs/实施计划.md](docs/实施计划.md)。
  - [x] S0 协议定稿（2026-09-19）：[docs/协议.md](docs/协议.md) + `protocol/`，py / ts 契约测试全绿。
  - [x] S1 A 核心 + B-py 绑定模式 + CI（2026-09-20）：`node/`，`echo` 任务类型跑通队列 / 缓存 / 句柄 / 注册表 / 任务 API，18 项验收测试。
  - [x] S2 `triage.audio` 引擎（2026-09-20）：三件套跑在子进程，双阈值 VAD 切点 −16ms，26s 素材 847ms（0.68× T2）；`gpu` job 跑真模型冒烟。
  - [x] S5 `ocr.structured` 引擎（2026-09-21）：PP-OCRv6 快档 + PaddleOCR-VL 慢档同一子进程，VRAM 按 tier 记账（VL 并发 2 → 第二个排队），快档每页给升级信号；T5 发票 12/12 字段。
  - [x] S3 前半：`@mmp/client`（2026-09-21）：客户端包 + 注入模板；三个 ts 包发布在私有 registry `https://mmp.seanartech.com/npm/`（[docs/包发布.md](docs/包发布.md)）；宿主适配层待联调。
  - [x] 公网端到端（2026-09-21）：枢纽机 nginx 443 + B-ts，内网 GPU 机 A 出站 wss；echo / 音频 / OCR 两档经 get+put 句柄往返，结果 PUT 回宿主；公网 nmap A 机 0 open。见 [docs/结果-公网端到端.md](docs/结果-公网端到端.md)。
  - [x] S3 后半：OpenClaw 适配（2026-09-21）：`@mmp/openclaw`——`mmp-triage` cli 条目 + 降级标注钩子；A 停机时回落平台 STT 并显式标注。见 [docs/宿主接入-OpenClaw.md](docs/宿主接入-OpenClaw.md)。
  - [x] S6 收口（2026-09-21）：缓存同源、批量 200 排队 + 429、零监听、B 重启重注册、契约测试全绿；两个发现进 issue。
  - [x] S4 A↔B WebSocket + B-ts（2026-09-21）：`ts/packages/broker`，A 出站 ws 重连重注册，B-py / B-ts 跑同一批行为契约场景；协议 v1.1 加媒体 `ref` 避免重复拉取。
- [ ] **M2 能力层**（2026-09-22 立项）：骨架 + 九条调研线（分离、单音 / 多轨扒谱、和声结构、乐器音色、语音贵档、说话人、描述标签、识别），每条先调研再定集成 / 延后。见 [docs/实施计划.md §6](docs/实施计划.md)。
- [ ] M3 预算与反馈升级
- [ ] M4 模态同构（图片 / 视频复用同一套抽象）

## 已实测的关键数字

| 项 | 结果 | 出处 |
|---|---|---|
| 音频便宜档（CPU） | SenseVoiceSmall int8 + Silero VAD，M1 Mac 上 **21× 实时**，模型合计约 270MB，零 torch，中文词全对 | [T2](docs/结果-T2.md) |
| 音频 GPU 档 | whisper.cpp 原生 sm_120 构建，**21.8× 实时**；模型加载 0.9s 占短音频总时七成 | [T1](docs/结果-T1.md) |
| 边界检测 | 双阈值 Silero VAD 切点误差 −16ms（sherpa 默认封装 +182ms） | [T2 复测](docs/结果-T2.md) |
| 端到端盲测 | 只凭 digest 选对 `pitch_transcribe` 并指对 0.00–2.18s | [T4](docs/结果-T4.md) |
| 真实音频 | 口哨 `Whistling 0.957`；风噪 + 喷麦下指令句 ASR 逐字对；58 s 混合音频推理 984 ms | [T6](docs/结果-T6.md) |
| 文档 OCR 快档（GPU） | PP-OCRv6 **0.4–0.5 s/页**，CER 约 1% | [T5](docs/结果-T5-文档OCR.md) |
| 文档 OCR 慢档（GPU） | PaddleOCR-VL 0.9B **约 9 s/页、12GB 显存**，出版面结构与印章 | [T5](docs/结果-T5-文档OCR.md) |

音频腿的真实素材复验见 [T6](docs/结果-T6.md)：10 段手机录音，ASR 基本逐字对，VAD 假阳性 / 漏检靠打标识破；合成哼唱当年的 `Boing 0.12` 在真口哨上是 `Whistling 0.95`。

## 仓库结构

```
protocol/
  schemas/             六个 JSON Schema（A↔B 消息 / 任务 API / 媒体句柄 / digest / 能力元数据 / 引擎 IO），语言中立的唯一源头
  fixtures/            契约测试样本（含 M0 的 digest）
  tests/py, tests/ts   同一批 fixtures 在 jsonschema 与 ajv 两侧的契约测试
node/
  mmp_node/            A 算力节点：队列、缓存（SQLite）、引擎池（子进程）、注册表、媒体句柄、A↔B 分发
  mmp_broker/          B 的 Python 实现：操作层（core）、HTTP 绑定（FastAPI）、进程内绑定（inproc）
  engines/             引擎子进程，JSON-lines over stdio；echo（S1 测试引擎）、triage_audio（音频预检）、ocr_structured（文档 OCR 快 / 慢两档），各有 README
  tools/               smoke_triage_audio.py / smoke_ocr_structured.py：真模型冒烟（CI gpu job）
tools/m1/              M1 收口的实机验收脚本（在枢纽机上跑：缓存同源、队列 / 429、在途拔线）与模拟宿主媒体端点
  tests/               S1 验收测试（经完整栈：HTTP → Broker → 信封 → Node → 子进程）
  node.toml.example    全部阈值外置；MMP_<SECTION>__<KEY> 环境变量覆盖
ts/                    pnpm workspace
  packages/protocol/   schema 加载、ajv 校验、从 protocol/schemas 生成的 TS 类型（pnpm gen-types）
  packages/broker/     B-ts：core（操作层）、ws（A 连入）、http（任务 API）、cli（独立模式 mmp-broker）
  packages/client/     C：@mmp/client——submit / wait / cancel / capabilities、MmpError 分类、digest → 注入块模板
  packages/openclaw/   OpenClaw 适配：mmp-triage CLI（tools.media 的 cli 条目）+ 降级标注钩子
.gitea/workflows/      ci.yml（unit：契约 + ts + node 验收）、gpu.yml（windows runner：真模型冒烟 + Windows 全套）
docs/
  架构.md              设计文档（公开版）
  协议.md              协议 v1.1 人读版，与 protocol/schemas 同步
  实施计划.md          M1 的步骤拆分、验收与顺序（S0–S6）
  M0-测试计划.md        M0 四组实验的计划
  结果-T1.md … T5      每组实验的原始记录（负结果也记）
  结果-网络测试.md      一次网络吞吐排查的复盘（教训：别用 ssh 管道测网速）
  结果-公网端到端.md    S4/S5 公网验收：枢纽机 B-ts + 内网 A，零监听扫描、句柄往返
  包发布.md             @mmp/* 的私有 registry：安装、发布、账号
  接入指南.md           给宿主工程师：装包、配置、用法、必须遵守的规则
  宿主接入-OpenClaw.md  OpenClaw 的接法（cli 条目 + 钩子）、内网侧宿主与 B 的对应、验收清单
  宿主接入-内网直连.md  内网 / tailnet 宿主直接用 @mmp/client 连 B-py（世纳终端）：与公网接法的三处差异、可抄的接法、排障
  结果-T6.md            真实音频复验：10 段手机录音逐样本记录、由此改的规则、盲测 10/10（原文在附录）
  结果-M1.md            M1 收口：七条完成定义的实机证据、手工清单、两个发现、正式部署快照
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

## 运行 A + B-py（绑定模式）

```bash
python3 -m venv .venv && .venv/bin/pip install -e "node[dev]"
cp node/node.toml.example node/node.toml      # 改 node.id / node.key；默认只监听 127.0.0.1:8765
(cd node && ../.venv/bin/python -m mmp_broker.main node.toml)

# 提交一个 echo 任务（inline 媒体），wait 5 秒拿结果；再提交一次得到 cached: true
B64=$(head -c 3000 /dev/urandom | base64 | tr -d '\n')
curl -s -X POST localhost:8765/jobs -H 'content-type: application/json' \
  -d "{\"type\":\"echo\",\"media\":{\"inline\":\"$B64\"},\"params\":{\"sleep_ms\":100},\"wait\":5}"
curl -s localhost:8765/capabilities; curl -s localhost:8765/health
```

## 运行 B-ts（独立模式）+ A 连过去

```bash
(cd ts && pnpm install && pnpm -r build)
MMP_BROKER__NODE_KEY=<同 node.key 的密钥> MMP_BROKER__PORT=8766 node ts/packages/broker/dist/cli.js   # HTTP 任务 API 与 /ws 共用一个端口
# node.toml 里加：
# [[brokers]]
# url = "ws://<broker-host>:8766/ws"
curl -s localhost:8766/health          # 应看到 A 注册进来
```

测试：`(cd node && ../.venv/bin/python -m pytest -q)`（含 A ws 客户端对真 B-ts 的集成验收，需先构建 ts）；`(cd ts && pnpm -r test)`；协议契约测试见 [docs/协议.md](docs/协议.md) §9。

## 托管

开发在内部 Gitea 进行（CI runner 挂在 GPU 机上，方便跑模型相关的测试；issue 只用于内部里程碑跟踪，不对外）。[GitHub](https://github.com/kevin-wang-2/multimodal-pipeline) 是公开镜像，镜像不作为开发入口。

## 许可

[Apache-2.0](LICENSE)。

## 记录约定

- 每组实验写 `docs/结果-T{n}.md`，附原始命令与输出摘要。
- **负结果同样是结论**。
- 设计变更先改 [docs/架构.md](docs/架构.md)，再改代码。
