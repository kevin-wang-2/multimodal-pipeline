# @mmp/openclaw

OpenClaw 宿主的适配层。两件东西：

1. **`mmp-triage` CLI**——作为 `tools.media.models[]` 的 `cli` 条目。OpenClaw 对每个入站音频调用它（`{{AttachmentPath}}`），stdout 就是用户消息里 `[Audio]` 块的"转写"——实际是带时间轴、缺口、可用能力的注入块。失败时 exit 2，OpenClaw 自动回落到下一个条目（平台自己的 STT）。
2. **插件 `mmp`**——一个 `before_prompt_build` 钩子，无状态、不碰媒体：prompt 里有 `[Audio]`/`[Video]` 块但没有我们的边界标记（走了平台兜底）→ 前置"⚠ 本次多模态预检不可用：转写来自平台兜底…"；连块都没有、只剩附件引用（两级都失败）→ 前置"⚠ …平台转写也没有产出…"。

为什么不在插件里自己"收消息 → 提交 → 注入"：`before_prompt_build` 事件里没有媒体，媒体只在 `message_received` 里；而 OpenClaw 的媒体理解层本来就负责"每个附件跑一次、结果进 prompt、失败回落、多轮不重跑"。借它的机制比自己再实现一套可靠。

## 安装

```ini
# ~/.npmrc 或项目 .npmrc
@mmp:registry=https://mmp.seanartech.com/npm/
```

```bash
openclaw plugins install npm:@mmp/openclaw
```

## 配置

`~/.openclaw/mmp.json`（`chmod 600`；也可用环境变量 `MMP_BASE_URL` / `MMP_API_KEY`）：

```json
{ "baseUrl": "http://<A/B 绑定的 B-py 内网地址>:8765", "apiKey": "…", "waitSec": 50 }
```

`~/.openclaw/openclaw.json`：

```json5
{
  tools: {
    media: {
      models: [
        // capabilities 要同时写 audio 与 video：飞书 / 微信的 voice.m4a 会被 OpenClaw 按魔数（ftyp）判成 video/mp4；A 侧 ffmpeg 只取音轨
        { type: "cli", command: "mmp-triage", args: ["{{AttachmentPath}}"], capabilities: ["audio", "video"], timeoutSeconds: 60, maxBytes: 8388608 },
        // 平台 STT 条目放在后面作兜底；capabilities 同样要含 video（语音 m4a 会被判成 video/mp4），whisper 接口吃得下 mp4 容器
        { provider: "openai", model: "gpt-4o-mini-transcribe", capabilities: ["audio", "video"] },
      ],
      audio: { enabled: true, attachments: { mode: "all", maxAttachments: 3 } },
    },
  },
  plugins: {
    entries: {
      mmp: { enabled: true, hooks: { allowConversationAccess: true } },
    },
  },
}
```

`waitSec` 要小于条目的 `timeoutSeconds`。`mmp-triage` 在 OpenClaw Gateway 的 PATH 里要能找到（`openclaw plugins install` 会把 bin 链到插件目录；否则写绝对路径）。

## 验证

```bash
mmp-triage --check                       # health=ok nodes=gpu-lab capabilities=…
mmp-triage some-voice-note.m4a           # 打印注入块；exit 0
MMP_BASE_URL=http://127.0.0.1:1 mmp-triage x.m4a; echo $?   # 2：OpenClaw 会回落
```

## 行为

- 附件 > 8 MiB（协议 inline 上限）：exit 2 回落——本机没有可给 A 拉取的 URL。
- 多轮追问：OpenClaw 对每个附件只做一次媒体理解，注入块随会话历史保留，不重复推理；块里的 `media=sha256:…` 就是后续能力调用可用的 `ref`。
- 服务不可用：OpenClaw 回落到平台 STT，插件钩子在 prompt 前显式标注降级（架构 §13）。
