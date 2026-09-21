# 宿主接入 · OpenClaw（内网路径）

> 对应 [实施计划.md](实施计划.md) S3 与 M1 定义第 1 条。研究于 2026-09-21，基于 OpenClaw 2026.7.2-beta.7 的插件文档（`docs/plugins/hooks.md`、`docs/nodes/media-understanding.md`）。

## 内网侧的两类宿主

| 宿主 | 网络 | 接法 | 连哪个 B |
|---|---|---|---|
| OpenClaw（本机） | 真内网 | 本文：`@mmp/openclaw`（`mmp-triage` CLI 条目 + 一个钩子） | A/B 绑定的 B-py（GPU 机内网地址 :8765，api_key） |
| 世纳终端 agent | tailnet | 直接 `@mmp/client`：[宿主接入-内网直连.md](宿主接入-内网直连.md) | 同上（tailnet 地址） |
| production manager | 公网 | `@mmp/client` | 公网枢纽 B-ts |

B-py 从只绑 127.0.0.1 改为绑内网地址 + `api_key`；GPU 机在 NAT 后、防火墙规则只放本网段与 tailnet，A 仍零公网监听（公网 nmap 0 open，见 [结果-公网端到端.md](结果-公网端到端.md)）。

## OpenClaw 的两个事实

1. `before_prompt_build` 事件只有 `prompt` 与 `messages`，**没有媒体**；媒体（本地 `path`、`contentType`、`transcribed`）只出现在 `message_received` 的 `media[]`，两者靠 `sessionKey` 关联。
2. 媒体理解层（`tools.media.models[]`）支持 **`type: "cli"` 条目**：对每个入站音频执行一个命令，`{{AttachmentPath}}` 为文件路径，stdout 成为用户消息里 `[Audio]` 块的转写（`{{Transcript}}`）；条目失败 / 超时 / 超过 `maxBytes` 自动回落到下一个条目；每个附件只处理一次，结果随会话历史保留。

## 设计

```
入站语音 ──OpenClaw 媒体理解──▶ mmp-triage {{AttachmentPath}} ──@mmp/client──▶ B-py（内网）──▶ A triage.audio
                                    │ exit 0：stdout = renderDigest(digest) → [Audio]/[Video] 块
                                    └ exit 2：回落到下一个条目（平台 STT，只接 audio）
                                                    │ 平台 STT 也失败 → prompt 里没有块，只剩附件引用
                     before_prompt_build 钩子：有 [Audio]/[Video] 块但无 [mmp:digest start] → "⚠ 预检不可用：转写来自平台兜底…"
                                                  没有块、只有附件引用（media://inbound/… 或音频文件名） → "⚠ 预检不可用，平台转写也没有产出…"
```

三条路径的取舍（2026-09-21 联调后定）：CLI 失败**必须 exit 2**，否则平台 STT 条目永远轮不到、等于没有兜底；"降级显式"完全交给钩子，钩子按 prompt 里剩下什么来区分两级降级。曾试过 CLI 在服务不可用时 exit 0 并输出 `[mmp:digest unavailable]` 块——降级是显式了，但兜底被短路，否决。

- **不需要**插件自己"收消息 → 提交 → 等结果 → 注入"，也**不需要**关掉平台转写——它就是兜底。实施计划 S3 原文的三条（`message_received` 提交、`before_prompt_build` 注入、关平台转写）由此改为：CLI 条目 + 一行降级标注 + 平台转写保留作回落。
- **多轮追问不重复推理**：OpenClaw 原生保证（附件只理解一次）。块里印着 `media=sha256:…`，M2 的能力调用直接 `ref`。
- **降级显式**：架构 §13 的要求由钩子满足；钩子无状态，只看 prompt 文本。
- **多插件 `prependContext` 合并语义**（架构.md 开放问题）：文档说可返回决定的处理器按 `priority` 降序串行、观察类并行；`prependContext` 由宿主拼接，本插件 `priority: 10`。实测结论待联调后补。

## 部署

- 包 `@mmp/openclaw`（bin `mmp-triage`、插件入口、`openclaw.plugin.json`）发布在 `https://mmp.seanartech.com/npm/`（匿名可读）。
- `openclaw plugins install npm:@mmp/openclaw`；升级 `openclaw plugins update mmp`。
- 配置：`~/.openclaw/mmp.json`（B 地址、api_key，600）+ `openclaw.json` 里的 `tools.media.models[]` cli 条目与 `plugins.entries.mmp`。完整片段见包内 README。

## 验收（S3 / M1 第 1 条）

| 项 | 怎么验 | 状态 |
|---|---|---|
| 本机 OpenClaw 发一段音频 → B-py → A → digest 注入回对话 | 发语音，看 A 日志有 triage 任务、回复体现时间轴信息 | ✅ 2026-09-21：mindweave 语音 → `mmp-triage` → 内网 B-py → A（日志 20:31:11、20:33:00 两条 triage.audio），回复引用了 VAD / ASR 结果 |
| 关掉 A 后 OpenClaw 仍能回复且注入块标注降级 | 停 lab 的 `mmp-node` 任务再发语音 | ✅ 2026-09-21：A 停后仍能回复；CLI exit 2 → 回落到下一条目（`openclaw infer audio transcribe` 实测 openai 条目被调用）。降级标注由钩子做，两级说明有单测；实况里 0.1.1 曾静默降级（附件被判 video，见上一节），0.1.2 的钩子按"无块只剩附件引用"补上，实况待下次 A 停机顺带看 |
| 同一媒体连续追问 5 轮不重复推理 | 看 A 日志：同一 media_id 只有一次 triage 任务 | ✅ 2026-09-21：对同一语音追问数轮，A 日志无新任务（OpenClaw 每个附件只理解一次，注入块随历史保留） |
| `mmp-triage` 三条路径 | `--check`、真实 m4a、B 不可达 exit 2 | ✅ 2026-09-21 |

## 附件被判成 video 的问题

OpenClaw 对 `chat.send` 附件**以魔数嗅探为准**，声明的 `mimeType` / `fileName` 只在嗅探不出具体类型时才用（`attachment-normalize`）。嗅探用 file-type：mp4 容器按 ftyp 主 brand 分——`M4A ` 是 `audio/x-m4a`，`iso5`/`isom` 等一律 `video/mp4`。iOS Safari 的 MediaRecorder 写 `M4A `，**macOS Safari 写 `iso5`**，于是同一段 `audio/mp4` 录音在 Mac 上会进 video 通道；video 通道里只有 google 提供者实现了 `describeVideo`，openai STT 条目无论声明什么 capabilities 都不会被调用（日志：`Video understanding provider "openai" not available`）。

结论：cli 条目声明 `audio + video` 只能保证 A 在线时能接；A 离线时平台 STT 兜底对这种文件永远缺席。根治在发送端——上传前把 ftyp 主 brand（字节 8–11）改成 `M4A `，文件仍是合法 MP4（兼容 brand 列表未动），OpenClaw 嗅探为 `audio/x-m4a`，`mmp-triage` 与 openai 两个条目都能接。实测（2026-09-21，A 停机）：原文件 → "No transcript returned"，无任何条目匹配；改 brand 后 → `mmp-triage` exit 2 → openai 条目被调用（本机被 SSRF 策略拦是 Clash fake-ip 的环境问题，不是链路问题）。

## 限制

- 附件 > 8 MiB 走回落：本机没有能给 A 拉取的 URL。要突破需要 B 提供上传口（架构 §6 留位）。
- `before_prompt_build` 的 `prompt` 含 `[Audio]`/`[Video]` 块文本（联调确认）；但两级媒体理解都失败时块不存在，prompt 只剩附件引用，钩子靠 `media://inbound/…` / 音频扩展名识别这种情况——启发式，非精确。更精确的做法是 `message_received` 记 `sessionKey`，留作后备。
