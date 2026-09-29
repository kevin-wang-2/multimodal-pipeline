# @mmp/client

multimodal-pipeline 的调用节点（C）客户端。只依赖全局 `fetch`（Node ≥ 20 / 浏览器 / edge runtime 都行），不知道也不关心 B 是绑定的还是远端的。

```ts
import { MmpClient, MmpError, media } from "@mmp/client";

const c = new MmpClient({ baseUrl: "https://mmp.example.com", apiKey: process.env.MMP_API_KEY });

// 客户端不知道具体模态和任务 id；用稳定 purpose + MIME 发现能力。
const capability = await c.resolveCapability({ purpose: "triage", contentType: attachment.type });
const done = await c.run(
  { type: capability.id, media: media.inline(bytes, attachment.type) },
  { pollWaitSec: 30 },
);
prompt = done.agent_context!.text + "\n\n" + userMessage; // 任务端生成，client 只透传

// 同一媒体的后续任务用 ref：A 本地有就不再上传 / 下载
await c.run({ type: "ocr.structured", media: media.ref(done.media_id), tier: "gpu", params: { pages: [2] } });
```

## API

| 方法 | 说明 |
|---|---|
| `submit(job)` | `POST /jobs`；返回 200 / 202 的任务态响应，4xx / 5xx 抛 `MmpError` |
| `get(jobId, waitSec?)` / `cancel(jobId, waitSec?)` | `GET` / `DELETE /jobs/{id}` |
| `capabilities()` / `health()` | B 的注册表汇总 / 在线节点 |
| `resolveCapability({purpose, contentType})` | 读取注册表并选出唯一匹配能力；未知或歧义显式抛错 |
| `wait(jobId, {timeoutMs, pollWaitSec, signal})` | 轮询到 `done`；`failed` / `cancelled` 抛错 |
| `run(job, opts)` | `submit` + `wait` |
| `media.inline / get / ref / refOr / withPut` | 媒体句柄构造（协议.md §5） |
| `matchesMediaType(pattern, contentType)` / `resolveCapability(response, query)` | 纯函数形式的 MIME 匹配与能力解析 |

### 错误

所有失败都是 `MmpError`：

- `code`：协议错误码（`no_node` / `node_offline` / `backpressure` / `engine_failed` / `timeout` / `media_not_found` …）或传输层 `network` / `timeout` / `unexpected`
- `status`、`retryAfterSec`、`jobId`（任务入队后失败才有）、`body`
- `retryable`：原样重试有意义（`node_offline` / `backpressure` / `network` / `timeout`）——客户端已按 `retries`（默认 2）自动做了，只对**还没入队**的请求和读操作
- `shouldFallback`：服务侧不可用的一类，调用方该走自己的兜底并显式标注

```ts
try { … } catch (e) {
  if (e instanceof MmpError && e.shouldFallback) prompt = explicitHostFallback(e.code) + …;
  else throw e;
}
```

`media_not_found`（`ref` 已被 A 淘汰）不算 fallback：带完整句柄（`media.refOr(id, media.get(url))`）重提即可。

## Agent context 长什么样

```
[mmp:agent-context start]
media=sha256:… kind=audio duration=4.39s engine=silero-vad+… tier=cpu degraded=no
时间轴（秒，[start,end)）：
- [0.00, 1.98) 非语音：Music 0.78, Synthesizer 0.68
- [1.98, 4.39) 语音：「帮我把这段的谱子扒出来。」 (zh)
缺口：
- 0.00–1.98s 为非语音段（Music、Synthesizer），只知类别、不知内容（无音高 / 谱面 / 歌词）
- 结构事实：非语音段位于时间轴前部、语音段在后（指令在后）
可用能力：pitch_transcribe, chord_recognize, source_separate, denoise, speaker_id, music_id, transcribe_segment
[mmp:agent-context end]
```

这个文本由具体任务实现生成；音频可以是时间轴，图片可以是整体描述、区域与 bbox。`@mmp/client` 不解析、不重组它。

## 冒烟

`examples/hub-smoke.mjs`：对一个真实 B 跑能力发现 → triage → ref 重提 → context 透传。
