# Broker 行为契约场景

B 是无状态转发层，它的全部行为 = 对五个操作的响应 + 对 A 消息的处理。这里每个 JSON 是一段脚本，用**假 A** 驱动 Broker，B-py（`node/tests/test_broker_scenarios.py`）与 B-ts（`ts/packages/broker/test/scenarios.test.ts`）跑同一批，结果必须一致。

```jsonc
{
  "description": "…",
  "broker": { "node_key": "…", "protocol_version": "1.1", "inflight_grace_sec": 5, "heartbeat_interval_sec": 10, "missed_heartbeats": 3 },
  "steps": [
    { "register": <ab-message 信封>, "replies": { "submit": {"http_status": 202, "body": {…}}, "get": …, "cancel": … }, "delay_sec": 0 },
    { "register": <信封>, "expect_reject": 4001 },            // 期望被拒（进程内即抛错 / ws 即关闭码）
    { "heartbeat": <信封> },
    { "disconnect": "<node_id>" },
    { "advance_sec": 31 },                                    // 拨快 Broker 的时钟（心跳超时判定）
    { "op": "submit", "args": <JobRequest>, "expect": {"http_status": 503, "body": {"error": "no_node"}}, "expect_routed_to": "<node_id>" },
    { "op": "get" | "cancel", "args": {"job_id": "…", "wait": 0}, "expect": {…} },
    { "op": "capabilities" | "health", "expect": {…} }
  ]
}
```

- `replies`：假 A 对每种 `op` 的固定应答。`body` 里 `"$echo_job_id"` 占位 = 请求里的 `job_id`；`submit` 没有 job_id，则为 `<node_id>-01J7ZQ9K3W8B6Q4M2N1P5R7S9T`。没给的 op 回 `404 not_found`。
- `expect.body` 是**子集匹配**（对象递归取子集、数组精确相等）。
- `expect_routed_to`：本步的 request 必须落到这个假 A（没落到任何 A 时用 `null`）。
- 时间：`advance_sec` 拨快 Broker 注入的时钟；在途超时用真实等待（`delay_sec` 让假 A 迟回）。请求超时只回 `504 timeout`，节点是否离线只看心跳。
