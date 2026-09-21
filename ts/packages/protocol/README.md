# @mmp/protocol

multimodal-pipeline 的协议包：六个 JSON Schema（随包打包）、ajv 校验器、从 schema 生成的 TypeScript 类型、错误码 → HTTP 状态表。唯一源头是仓库根的 `protocol/schemas`；人读版 `docs/协议.md`。

```ts
import { validator, ID, HTTP_STATUS, type JobRequest, type Digest } from "@mmp/protocol";
const ok = validator(`${ID.jobApi}#/$defs/JobRequest`)(body);
```
