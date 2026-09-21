# 宿主接入 · OpenClaw（内网路径）

> 对应 [实施计划.md](实施计划.md) S3 与 M1 定义第 1 条。研究于 2026-09-21，基于 OpenClaw 2026.7.2-beta.7 的插件文档（`docs/plugins/hooks.md`、`docs/nodes/media-understanding.md`）。

## 内网侧的两类宿主

| 宿主 | 网络 | 接法 | 连哪个 B |
|---|---|---|---|
| OpenClaw（本机） | 真内网 | 本文：`@mmp/openclaw`（`mmp-triage` CLI 条目 + 一个钩子） | A/B 绑定的 B-py（GPU 机内网地址 :8765，api_key） |
| 世纳终端 agent | tailnet | 与 production manager 相同：直接 `@mmp/client` | 同上（tailnet 地址） |
| production manager | 公网 | `@mmp/client` | 公网枢纽 B-ts |

B-py 从只绑 127.0.0.1 改为绑内网地址 + `api_key`；GPU 机在 NAT 后、防火墙规则只放本网段与 tailnet，A 仍零公网监听（公网 nmap 0 open，见 [结果-公网端到端.md](结果-公网端到端.md)）。

## OpenClaw 的两个事实

1. `before_prompt_build` 事件只有 `prompt` 与 `messages`，**没有媒体**；媒体（本地 `path`、`contentType`、`transcribed`）只出现在 `message_received` 的 `media[]`，两者靠 `sessionKey` 关联。
2. 媒体理解层（`tools.media.models[]`）支持 **`type: "cli"` 条目**：对每个入站音频执行一个命令，`{{AttachmentPath}}` 为文件路径，stdout 成为用户消息里 `[Audio]` 块的转写（`{{Transcript}}`）；条目失败 / 超时 / 超过 `maxBytes` 自动回落到下一个条目；每个附件只处理一次，结果随会话历史保留。

## 设计

```
入站语音 ──OpenClaw 媒体理解──▶ mmp-triage {{AttachmentPath}} ──@mmp/client──▶ B-py（内网）──▶ A triage.audio
                                    │ exit 0：stdout = renderDigest(digest) → [Audio] 块
                                    └ exit 2：回落到下一个条目（平台 STT）
                                                    │
                     before_prompt_build 钩子：有 [Audio] 块但无 [mmp:digest start] → prependContext "⚠ 本次多模态预检不可用…"
```

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
| 本机 OpenClaw 发一段音频 → B-py → A → digest 注入回对话 | 飞书 / 微信发语音，看回复是否体现时间轴信息 | 待联调（要动本地 OpenClaw，需用户点头） |
| 关掉 A 后 OpenClaw 仍能回复且注入块标注降级 | 停 lab 的 `mmp-node` 任务再发语音 | 待联调 |
| 同一媒体连续追问 5 轮不重复推理 | 看 A 日志：同一 media_id 只有一次 triage 任务 | 待联调 |
| `mmp-triage` 三条路径 | `--check`、真实 m4a、B 不可达 exit 2 | ✅ 2026-09-21 |

## 限制

- 附件 > 8 MiB 走回落：本机没有能给 A 拉取的 URL。要突破需要 B 提供上传口（架构 §6 留位）。
- `before_prompt_build` 的 `prompt` 是否包含 `[Audio]` 块文本，文档说是（body 变成 `[Audio]` 块），联调时确认；不含则钩子改用 `message_received` 记 `sessionKey`。
