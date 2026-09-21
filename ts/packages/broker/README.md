# @mmp/broker

B 协调节点的 TypeScript 实现。无状态：只有已连接节点、能力表、在途请求三张内存表。

- **独立模式**：`mmp-broker broker.toml`（或 `MMP_BROKER__*` 环境变量）。HTTP 任务 API 与 A 连入的 `/ws` 共用一个端口，生产上放在 TLS 反代（nginx 443）后面，见 `docs/结果-公网端到端.md`。
- **绑定模式**（嵌入 agent 宿主进程）：

```ts
import { startBroker } from "@mmp/broker";
const b = await startBroker({ nodeKey: process.env.MMP_NODE_KEY!, apiKey: process.env.MMP_API_KEY, host: "0.0.0.0", port: 8766 });
// b.broker 是操作层：await b.broker.submit(job) → { status, body }，宿主内的 C 可以直接调，不走 HTTP
```

行为契约：`protocol/fixtures/broker-scenarios/` 的场景 B-py 与 B-ts 共跑。
