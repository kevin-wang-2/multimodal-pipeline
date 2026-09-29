/* 由 scripts/gen-types.ts 从 protocol/schemas 生成，不要手改：pnpm gen-types */
/* eslint-disable */

/**
 * A↔B 消息（架构.md §4）。一条 ws 连接，四类消息，统一信封 {type, protocol_version, ts, payload}。A/B 绑定时同样的信封走进程内调用。根 schema 即信封。
 */
export type AbMessage = {
  type: "register" | "heartbeat" | "request" | "response";
  /**
   * major.minor。major 不同即不兼容；minor 只做向后兼容的追加。
   */
  protocol_version: string;
  ts: string;
  payload: {
    [k: string]: unknown;
  };
} & (
  | {
      type?: "register";
      payload?: RegisterPayload;
      [k: string]: unknown;
    }
  | {
      type?: "heartbeat";
      payload?: HeartbeatPayload;
      [k: string]: unknown;
    }
  | {
      type?: "request";
      payload?: RequestPayload;
      [k: string]: unknown;
    }
  | {
      type?: "response";
      payload?: ResponsePayload;
      [k: string]: unknown;
    }
);
/**
 * B→A。C 的三种任务操作都映射成 request；提交 / 查询 / 取消以 op 区分，req_id 由 B 生成并用于配对。
 */
export type RequestPayload = {
  /**
   * B 生成，连接内唯一即可。
   */
  req_id: string;
  op: "submit" | "get" | "cancel";
  job?: JobRequest;
  /**
   * 由 A 生成：<node_id>-<ULID>。B 不生成 id，靠前缀把 GET / DELETE 路由回原节点。
   */
  job_id?: string;
  /**
   * 最长阻塞秒数。B 侧在途超时 = wait + 5s。
   */
  wait?: number;
} & (
  | {
      op?: "submit";
      [k: string]: unknown;
    }
  | {
      op?: "get" | "cancel";
      [k: string]: unknown;
    }
);
/**
 * 媒体句柄（架构.md §6）。一种契约、多种实例：A 只做 HTTP GET / PUT，不认识任何存储厂商。inline / get / ref 至少一个；ref 是内容引用（此前某次响应里的 media_id），A 本地已有则不再取；ref 与 inline/get 同时给时，A 缺内容才取，取到后校验 hash。put 可选，供大输出写回。内容 hash 由 A 取到字节后计算，与来源无关。
 */
export type MediaHandle = {
  [k: string]: unknown;
} & {
  /**
   * base64 编码的媒体字节。解码后上限 8 MiB（8388608 字节 → base64 最长 11184812 字符）；超过必须走 get。
   */
  inline?: string;
  get?: Endpoint;
  /**
   * 内容引用：A 本地已有这份媒体则跳过 inline / get（v1.1）。只给 ref 而 A 没有 → 422 media_not_found；与 inline / get 一起给而取回的内容 hash 不符 → 422 media_hash_mismatch。
   */
  ref?: string;
  put?: Endpoint1;
  /**
   * 可选的 MIME 提示。A 仍以字节嗅探为准。
   */
  content_type?: string;
};
/**
 * 200：任务完成。result 与 result_ref 二选一。
 */
export type JobDone = {
  /**
   * 由 A 生成：<node_id>-<ULID>。B 不生成 id，靠前缀把 GET / DELETE 路由回原节点。
   */
  job_id: string;
  /**
   * 任务类型 id。点号分命名空间（triage.audio / ocr.structured），下划线分词（pitch_transcribe）。
   */
  type: string;
  status: "done";
  /**
   * 内容 hash，作为 media_id 与缓存键；同一份媒体经内联与经 URL 提交得到同一个值。
   */
  media_id: string;
  cached: boolean;
  source: Source;
  /**
   * 该任务类型 output.schema 的实例；预检类型即 digest。
   */
  result?: {
    [k: string]: unknown;
  };
  result_ref?: ResultRef;
  agent_context?: AgentContext;
  timings_ms?: {
    [k: string]: number;
  };
} & {
  [k: string]: unknown;
};
/**
 * 自动预检产出的任务无关结构化摘要。audio 用时间段锚定事实；image 用 surface + bbox 锚定视觉索引。gaps 始终显式声明未覆盖信息。
 */
export type Digest = AudioDigest | ImageDigest;
/**
 * 规则生成的缺口清单，是 Harness 判断是否显式调用下一任务的主要依据；MMP 不据此推断用户意图。
 */
export type Gaps = string[];
/**
 * 任务无关的能力清单：这份媒体上能执行什么。项为任务类型 id，不代表 MMP 建议调用。
 */
export type CapabilitiesAvailable = string[];
/**
 * A 与引擎子进程之间的 JSON-lines 协议（实施计划 §4.2）。每行一个对象，以 type 区分。A 内部使用，不暴露给 B / C；引擎只依赖标准库即可实现。媒体以本机临时文件路径传给引擎——这是进程内的实现细节，不是媒体句柄契约的一部分。
 */
export type EngineIo = {
  type: "hello" | "run" | "cancel" | "shutdown" | "result" | "error" | "log";
  [k: string]: unknown;
} & (Hello | Run | Cancel | Shutdown | Result | Error | Log);
/**
 * 引擎→A。result 是该任务类型 output.schema 的实例（digest 由引擎组装，A 只补 source）。
 */
export type Result = {
  type: "result";
  id: string;
  result?: unknown;
  /**
   * 引擎请求 A 在释放当前阶段资源后执行一次内部质量兜底；不是对外任务升级。
   */
  retry?: {
    engine: string;
    reason: string;
  };
  timings_ms?: {
    [k: string]: number;
  };
  /**
   * 仅供节点调度与验收观测的内部执行指标，不进入任务结果协议。
   */
  metrics?: {
    [k: string]: number;
  };
} & {
  [k: string]: unknown;
};

export interface Protocol {
  "ab-message"?: AbMessage;
  "ab-message__RegisterPayload"?: RegisterPayload;
  "ab-message__HeartbeatPayload"?: HeartbeatPayload;
  "ab-message__RequestPayload"?: RequestPayload;
  "ab-message__ResponsePayload"?: ResponsePayload;
  /**
   * B 生成，连接内唯一即可。
   */
  "ab-message__ReqId"?: string;
  /**
   * B 拒绝 A 时使用的 ws 关闭码（4000–4999 为应用私有区）。A 收到 4001 / 4002 不应重试，除非配置或版本变更。
   */
  "ab-message__CloseCode"?: 4001 | 4002 | 4003;
  capability?: Capability;
  /**
   * 任务类型 id。点号分命名空间（triage.audio / ocr.structured），下划线分词（pitch_transcribe）。
   */
  capability__TaskTypeId?: string;
  /**
   * 引擎档位。cpu 便宜档常开；gpu-fast 快档；gpu 慢档 / 重模型；remote 由 A 转调外部服务。
   */
  capability__Tier?: "cpu" | "gpu-fast" | "gpu" | "remote";
  capability__TierSpec?: TierSpec;
  digest?: Digest;
  digest__ToolStatus?: "ok" | "partial" | "failed" | "skipped";
  digest__Label?: Label;
  digest__Asr?: Asr;
  digest__Segment?: Segment;
  digest__AudioDigest?: AudioDigest;
  digest__InferenceProvenance?: InferenceProvenance;
  digest__GeneratedDescription?: GeneratedDescription;
  digest__ImageSurface?: ImageSurface;
  digest__BoundingBoxPx?: BoundingBoxPx;
  digest__ImageRegion?: ImageRegion;
  digest__QualitySignal?: QualitySignal;
  digest__ImageDigest?: ImageDigest;
  digest__Tools?: Tools;
  digest__Gaps?: Gaps;
  digest__CapabilitiesAvailable?: CapabilitiesAvailable;
  digest__Timings?: Timings;
  "engine-io"?: EngineIo;
  "engine-io__ReqId"?: string;
  "engine-io__Hello"?: Hello;
  "engine-io__Run"?: Run;
  "engine-io__Cancel"?: Cancel;
  "engine-io__Shutdown"?: Shutdown;
  "engine-io__Result"?: Result;
  "engine-io__Error"?: Error;
  "engine-io__Log"?: Log;
  /**
   * A 节点 id，由部署配置指定。小写字母、数字、连字符。
   */
  "job-api__NodeId"?: string;
  /**
   * 由 A 生成：<node_id>-<ULID>。B 不生成 id，靠前缀把 GET / DELETE 路由回原节点。
   */
  "job-api__JobId"?: string;
  "job-api__Priority"?: "interactive" | "batch";
  /**
   * queued → running → done | failed | cancelled；queued → cancelled。
   */
  "job-api__JobStatus"?: "queued" | "running" | "done" | "failed" | "cancelled";
  /**
   * 错误码表；HTTP 状态一一对应，见 docs/协议.md。
   */
  "job-api__ErrorCode"?:
    | "bad_request"
    | "unsupported_type"
    | "unauthorized"
    | "not_found"
    | "media_too_large"
    | "media_fetch_failed"
    | "media_not_found"
    | "media_hash_mismatch"
    | "backpressure"
    | "engine_failed"
    | "no_node"
    | "node_offline"
    | "timeout";
  /**
   * 最长阻塞秒数。B 侧在途超时 = wait + 5s。
   */
  "job-api__Wait"?: number;
  "job-api__JobRequest"?: JobRequest;
  "job-api__Source"?: Source;
  "job-api__ResultRef"?: ResultRef;
  "job-api__AgentContext"?: AgentContext;
  "job-api__JobDone"?: JobDone;
  "job-api__JobPending"?: JobPending;
  "job-api__JobFailed"?: JobFailed;
  "job-api__JobCancelled"?: JobCancelled;
  /**
   * POST /jobs、GET /jobs/{id}、DELETE /jobs/{id} 的任务态响应体。
   */
  "job-api__JobResponse"?: JobDone | JobPending | JobFailed | JobCancelled;
  "job-api__ErrorBody"?: ErrorBody;
  /**
   * 任务端点可能返回的任何响应体。
   */
  "job-api__AnyResponse"?: (JobDone | JobPending | JobFailed | JobCancelled) | ErrorBody;
  "job-api__CapabilitiesResponse"?: CapabilitiesResponse;
  "job-api__HealthResponse"?: HealthResponse;
  /**
   * major.minor。major 不同即不兼容；minor 只做向后兼容的追加。
   */
  "job-api__ProtocolVersion"?: string;
  "media-handle"?: MediaHandle;
  "media-handle__Endpoint"?: Endpoint2;
  /**
   * 内容 hash，作为 media_id 与缓存键；同一份媒体经内联与经 URL 提交得到同一个值。
   */
  "media-handle__MediaId"?: string;
}
/**
 * A→B。连接建立后的第一条消息；重连后必须重发，B 用 node_id 覆盖旧表项。B 校验失败直接关连接（见 CloseCode），不回消息。
 */
export interface RegisterPayload {
  /**
   * A 节点 id，由部署配置指定。小写字母、数字、连字符。
   */
  node_id: string;
  /**
   * 静态节点密钥，配置下发。
   */
  node_key: string;
  /**
   * @minItems 1
   */
  capabilities: [Capability, ...Capability[]];
  /**
   * 引擎名 → 版本，是 capabilities[].tiers[].engine_version 的汇总，便于 B 在 /health 里展示。
   */
  engine_versions: {
    [k: string]: string;
  };
}
/**
 * 任务类型元数据（架构.md §10）。A 的注册表里每个任务类型一条，register 时整表上报；B 的 GET /capabilities 汇总后给 C。预检（triage.*）与能力是同一套任务系统里的不同任务类型。
 */
export interface Capability {
  /**
   * 任务类型 id。点号分命名空间（triage.audio / ocr.structured），下划线分词（pitch_transcribe）。
   */
  id: string;
  /**
   * 稳定的能力意图；客户端用 purpose + Content-Type 发现任务，不依赖任务 id 或封闭模态枚举。
   */
  purpose: string;
  /**
   * 给上层 agent 看的一句话说明；agent 按能力名从 /capabilities 按需拉取。
   */
  description?: string;
  /**
   * 该能力适用的媒体特征；预检据此筛出 capabilities_available（M2）。省略表示对该模态的任何媒体都适用。
   */
  consumes?: {
    /**
     * 适用的粗类别标签（AudioSet 等），任一命中即可。
     */
    labels?: string[];
    granularity?: "whole" | "segment" | "page" | "region";
  };
  /**
   * 该任务类型在本节点可用的引擎档位，按偏好排序：第一项是默认档，后面的是回落 / 升级候选。
   *
   * @minItems 1
   */
  tiers: [TierSpec, ...TierSpec[]];
  input: {
    /**
     * 媒体输入声明。accepts 是开放的 MIME 匹配器，可用 audio/* 或 * /*，同一能力可接受多种媒体。
     */
    media: {
      presence: "required" | "optional" | "none";
      accepts: string[];
      /**
       * A 侧可选预处理器 id；调用方不应解释此字段。
       */
      preprocessor?: string;
    };
    /**
     * params 的 JSON Schema（内联）。省略表示不接受任何 params。
     */
    params_schema?: {
      [k: string]: unknown;
    };
  };
  output: {
    /**
     * 结果的 schema：本协议内 schema 的 $id，或内联 JSON Schema。
     */
    schema:
      | string
      | {
          [k: string]: unknown;
        };
    /**
     * 该任务是否生成可直接交给基础模型的通用 agent context。
     */
    agent_context: "required" | "optional" | "none";
  };
}
export interface TierSpec {
  /**
   * 引擎档位。cpu 便宜档常开；gpu-fast 快档；gpu 慢档 / 重模型；remote 由 A 转调外部服务。
   */
  tier: "cpu" | "gpu-fast" | "gpu" | "remote";
  /**
   * 引擎标识，如 silero-vad+sensevoice-int8+zipformer-tagging-int8、pp-ocrv6、paddleocr-vl。
   */
  engine: string;
  /**
   * 进缓存键；升级引擎自动失效旧缓存。
   */
  engine_version: string;
  cost: "low" | "medium" | "high";
  /**
   * 人读的延迟提示，如 "~2s"、"~9s/page"。
   */
  latency_hint?: string;
  /**
   * 单任务显存预算（MiB）；调度器据此串行。cpu 档省略。
   */
  vram_mb?: number;
  /**
   * 该档位在本节点的并发上限。
   */
  max_concurrency?: number;
}
/**
 * A→B，每 10s 一次；B 连续 3 次未收到视为断线。
 */
export interface HeartbeatPayload {
  /**
   * A 节点 id，由部署配置指定。小写字母、数字、连字符。
   */
  node_id: string;
  /**
   * 排队中（未开始）的任务数，含所有优先级。
   */
  queue_len: number;
  running: number;
  /**
   * 空余显存（MiB）。无 GPU 的节点省略。
   */
  vram_free_mb?: number;
  engines_loaded: string[];
}
/**
 * POST /jobs 请求体。
 */
export interface JobRequest {
  /**
   * 任务类型 id。点号分命名空间（triage.audio / ocr.structured），下划线分词（pitch_transcribe）。
   */
  type: string;
  media: MediaHandle;
  /**
   * 任务类型专有参数，按该类型 capability.input.params_schema 校验。进缓存键（规范化后 hash）。
   */
  params?: {
    [k: string]: unknown;
  };
  priority?: "interactive" | "batch";
  /**
   * 引擎档位。cpu 便宜档常开；gpu-fast 快档；gpu 慢档 / 重模型；remote 由 A 转调外部服务。
   */
  tier?: "cpu" | "gpu-fast" | "gpu" | "remote";
  /**
   * 最长阻塞秒数。B 侧在途超时 = wait + 5s。
   */
  wait?: number;
}
/**
 * 带认证的 GET 端点：对象存储 presigned URL、宿主自带 token 的接口等都是它的实例。
 */
export interface Endpoint {
  url: string;
  headers?: {
    [k: string]: string;
  };
}
/**
 * 可选：大输出的 PUT 端点。存在时 A 可把超限输出 PUT 到这里，响应里只给引用。
 */
export interface Endpoint1 {
  url: string;
  headers?: {
    [k: string]: string;
  };
}
/**
 * A→B。与 HTTP 语义一一对应：B 把 http_status 与 body 原样透传给 C。
 */
export interface ResponsePayload {
  /**
   * B 生成，连接内唯一即可。
   */
  req_id: string;
  http_status: number;
  /**
   * 任务端点可能返回的任何响应体。
   */
  body: (JobDone | JobPending | JobFailed | JobCancelled) | ErrorBody;
}
/**
 * 结果的来源标注（铁律 5）。描述的是产物本身：哪个引擎、哪一档、何时、是否降级。"是否命中缓存"属于本次交付而非产物，放在 JobDone.cached。
 */
export interface Source {
  /**
   * 引擎档位。cpu 便宜档常开；gpu-fast 快档；gpu 慢档 / 重模型；remote 由 A 转调外部服务。
   */
  tier: "cpu" | "gpu-fast" | "gpu" | "remote";
  engine: string;
  engine_version: string;
  generated_at: string;
  /**
   * true = 未按请求的档位 / 完整流程产出（超时回落、部分工具失败等）。降级必须显式。
   */
  degraded: boolean;
  degraded_reason?: "timeout" | "tier_unavailable" | "partial_failure";
  /**
   * 实际生效的参数（规范化后），含默认值；如 label_confidence_threshold。
   */
  params?: {
    [k: string]: unknown;
  };
}
/**
 * 大输出已 PUT 到请求方给的 media.put 端点时，响应里只给这份引用。
 */
export interface ResultRef {
  content_type: string;
  bytes: number;
  sha256: string;
}
/**
 * 由任务实现确定性生成、可直接交给基础模型的上下文；客户端只透传，不解析其中的模态语义。
 */
export interface AgentContext {
  format: "mmp-agent-context-v1";
  content_type: "text/plain; charset=utf-8";
  text: string;
}
/**
 * 202：排队中或运行中。
 */
export interface JobPending {
  /**
   * 由 A 生成：<node_id>-<ULID>。B 不生成 id，靠前缀把 GET / DELETE 路由回原节点。
   */
  job_id: string;
  /**
   * 任务类型 id。点号分命名空间（triage.audio / ocr.structured），下划线分词（pitch_transcribe）。
   */
  type: string;
  /**
   * 内容 hash，作为 media_id 与缓存键；同一份媒体经内联与经 URL 提交得到同一个值。
   */
  media_id?: string;
  status: "queued" | "running";
  eta_sec?: number;
  queue_position?: number;
  /**
   * DELETE 已收到但任务在运行中，A 尽力而为；后续 GET 会得到 cancelled 或 done。
   */
  cancel_requested?: boolean;
}
/**
 * 任务已入队但最终失败；HTTP 状态按 error 对应。
 */
export interface JobFailed {
  /**
   * 由 A 生成：<node_id>-<ULID>。B 不生成 id，靠前缀把 GET / DELETE 路由回原节点。
   */
  job_id: string;
  /**
   * 任务类型 id。点号分命名空间（triage.audio / ocr.structured），下划线分词（pitch_transcribe）。
   */
  type: string;
  /**
   * 内容 hash，作为 media_id 与缓存键；同一份媒体经内联与经 URL 提交得到同一个值。
   */
  media_id?: string;
  status: "failed";
  /**
   * 错误码表；HTTP 状态一一对应，见 docs/协议.md。
   */
  error:
    | "bad_request"
    | "unsupported_type"
    | "unauthorized"
    | "not_found"
    | "media_too_large"
    | "media_fetch_failed"
    | "media_not_found"
    | "media_hash_mismatch"
    | "backpressure"
    | "engine_failed"
    | "no_node"
    | "node_offline"
    | "timeout";
  message?: string;
  retry_after_sec?: number;
}
/**
 * 200：任务已取消。
 */
export interface JobCancelled {
  /**
   * 由 A 生成：<node_id>-<ULID>。B 不生成 id，靠前缀把 GET / DELETE 路由回原节点。
   */
  job_id: string;
  /**
   * 任务类型 id。点号分命名空间（triage.audio / ocr.structured），下划线分词（pitch_transcribe）。
   */
  type: string;
  /**
   * 内容 hash，作为 media_id 与缓存键；同一份媒体经内联与经 URL 提交得到同一个值。
   */
  media_id?: string;
  status: "cancelled";
}
/**
 * 没有 job_id 的错误（请求没被接受或找不到任务）。有 job_id 的失败用 JobFailed。
 */
export interface ErrorBody {
  /**
   * 错误码表；HTTP 状态一一对应，见 docs/协议.md。
   */
  error:
    | "bad_request"
    | "unsupported_type"
    | "unauthorized"
    | "not_found"
    | "media_too_large"
    | "media_fetch_failed"
    | "media_not_found"
    | "media_hash_mismatch"
    | "backpressure"
    | "engine_failed"
    | "no_node"
    | "node_offline"
    | "timeout";
  message?: string;
  retry_after_sec?: number;
}
export interface AudioDigest {
  /**
   * 内容 hash，作为 media_id 与缓存键；同一份媒体经内联与经 URL 提交得到同一个值。
   */
  media_id: string;
  kind: "audio";
  duration_sec: number;
  /**
   * 统一为秒（浮点）；区间左闭右开 [start, end)。
   */
  timeline_unit: "sec";
  /**
   * 覆盖全时长、按 start 升序、彼此不重叠的段。空媒体允许为空数组。
   */
  segments: Segment[];
  /**
   * 整条音频级别的统计。音频便宜档不设 caption。
   */
  global?: {
    speech_ratio?: number;
  };
  tools: Tools;
  gaps: Gaps;
  capabilities_available: CapabilitiesAvailable;
  source: Source;
  timings_ms?: Timings;
}
export interface Segment {
  start: number;
  end: number;
  /**
   * 置信度 ≥ 阈值（source.params.label_confidence_threshold，默认 0.30）的粗类别标签，按 score 降序。
   */
  labels: Label[];
  /**
   * ok：labels 非空且可信。unclassified：top 分数低于阈值，labels 必须为空，并在 gaps 里说明。failed：打标工具在本段失败，labels 为空。
   */
  label_status: "ok" | "unclassified" | "failed";
  /**
   * null = 本段不适用（非语音段）。省略 = 应有但取不到（语音段 ASR 失败，须在 tools.asr 与 gaps 体现）。
   */
  asr?: null | Asr;
}
export interface Label {
  tag: string;
  score: number;
}
/**
 * 语音段的转写。字段取不到用 "n/a" 或省略，不用 null（null 只表示"不适用"，见 Segment.asr）。
 */
export interface Asr {
  text: string;
  /**
   * BCP-47 语言标签的主语言子标签（zh / en / ja …），或 "n/a"。
   */
  lang: string;
  confidence?: number | "n/a";
}
/**
 * 每个参与工具的状态。失败项必须显式标注，不许静默。
 */
export interface Tools {
  [k: string]: "ok" | "partial" | "failed" | "skipped";
}
export interface Timings {
  [k: string]: number;
}
export interface ImageDigest {
  /**
   * 内容 hash，作为 media_id 与缓存键；同一份媒体经内联与经 URL 提交得到同一个值。
   */
  media_id: string;
  kind: "image";
  /**
   * 输入图片的 MIME，例如 image/jpeg、image/png、image/tiff。文档容器不属于图片 digest。
   */
  format: string;
  /**
   * @minItems 1
   */
  surfaces: [ImageSurface, ...ImageSurface[]];
  description?: GeneratedDescription;
  regions: ImageRegion[];
  /**
   * @minItems 1
   */
  quality_signals: [QualitySignal, ...QualitySignal[]];
  tools: Tools;
  gaps: Gaps;
  capabilities_available: CapabilitiesAvailable;
  source: Source;
  timings_ms?: Timings;
}
/**
 * 可被区域引用的一张像素平面：普通图片只有一个 image surface；多页文档每页一个 page surface。尺寸与 bbox 均指完成 rotation_deg 归正后的像素坐标。
 */
export interface ImageSurface {
  id: string;
  kind: "image" | "page";
  index: number;
  width_px: number;
  height_px: number;
  rotation_deg: 0 | 90 | 180 | 270;
}
export interface GeneratedDescription {
  text: string;
  provenance: InferenceProvenance;
}
/**
 * 明确标识生成内容是模型推断而非确定性事实；具体模型、版本和档位由 digest.source 给出。
 */
export interface InferenceProvenance {
  kind: "model_inference";
  /**
   * @minItems 1
   */
  tools: [string, ...string[]];
}
/**
 * 模型推断的语义区域。id 在同一 digest 内稳定唯一；后续能力按 surface_id + bbox_px 或 id 引用。
 */
export interface ImageRegion {
  id: string;
  surface_id: string;
  bbox_px: BoundingBoxPx;
  label: string;
  description?: string;
  provenance: InferenceProvenance;
}
/**
 * 归正后 surface 上的左闭右开像素框 [x_min, y_min, x_max, y_max)。实现还必须保证 x_max > x_min、y_max > y_min 且不越界。
 */
export interface BoundingBoxPx {
  x_min: number;
  y_min: number;
  x_max: number;
  y_max: number;
}
/**
 * 可解释的执行质量信号，不是伪装成校准概率的总 confidence。failed 可触发 MMP 内部一次质量兜底。
 */
export interface QualitySignal {
  code: string;
  status: "ok" | "warning" | "failed";
  value?: boolean | number | string;
  detail?: string;
}
/**
 * 引擎→A。启动后的第一行；A 收到前不发任务。engine_version 必须与注册表一致，否则 A 拒绝该引擎。
 */
export interface Hello {
  type: "hello";
  engine: string;
  engine_version: string;
  /**
   * major.minor。major 不同即不兼容；minor 只做向后兼容的追加。
   */
  protocol_version: string;
}
/**
 * A→引擎。一个任务；同一进程内可有多个在途 run，以 id 配对。
 */
export interface Run {
  type: "run";
  id: string;
  job: {
    /**
     * 由 A 生成：<node_id>-<ULID>。B 不生成 id，靠前缀把 GET / DELETE 路由回原节点。
     */
    job_id: string;
    /**
     * 任务类型 id。点号分命名空间（triage.audio / ocr.structured），下划线分词（pitch_transcribe）。
     */
    task_type: string;
    /**
     * 引擎档位。cpu 便宜档常开；gpu-fast 快档；gpu 慢档 / 重模型；remote 由 A 转调外部服务。
     */
    tier: "cpu" | "gpu-fast" | "gpu" | "remote";
    /**
     * 内容 hash，作为 media_id 与缓存键；同一份媒体经内联与经 URL 提交得到同一个值。
     */
    media_id: string;
    /**
     * 本机文件路径；input.media=none 的类型省略。
     */
    media_path?: string;
    params: {
      [k: string]: unknown;
    };
    internal?: {
      [k: string]: unknown;
    };
    timeout_sec?: number;
  };
}
/**
 * A→引擎。尽力而为；引擎随后以 error{cancelled} 或 result 收尾。
 */
export interface Cancel {
  type: "cancel";
  id: string;
}
/**
 * A→引擎。引擎应尽快退出；A 超时后 kill。显存释放唯一可靠的方式是结束进程。
 */
export interface Shutdown {
  type: "shutdown";
}
export interface Error {
  type: "error";
  id: string;
  error: "engine_failed" | "bad_params" | "cancelled";
  message?: string;
}
/**
 * 引擎→A。结构化日志；引擎也可以直接写 stderr。
 */
export interface Log {
  type: "log";
  level: "debug" | "info" | "warning" | "error";
  message: string;
}
/**
 * GET /capabilities：B 把当前已连接 A 的注册表按任务类型合并。B 无状态，这只是此刻的视图。
 */
export interface CapabilitiesResponse {
  /**
   * major.minor。major 不同即不兼容；minor 只做向后兼容的追加。
   */
  protocol_version: string;
  capabilities: {
    capability: Capability;
    /**
     * 提供该任务类型的节点。
     *
     * @minItems 1
     */
    nodes: [string, ...string[]];
  }[];
}
/**
 * GET /health：已连接 A 及其最近心跳摘要。
 */
export interface HealthResponse {
  /**
   * ok = 至少一个节点在线；no_node = 没有节点。
   */
  status: "ok" | "no_node";
  /**
   * major.minor。major 不同即不兼容；minor 只做向后兼容的追加。
   */
  protocol_version: string;
  nodes: {
    /**
     * A 节点 id，由部署配置指定。小写字母、数字、连字符。
     */
    node_id: string;
    connected_since: string;
    last_heartbeat: string;
    heartbeat?: HeartbeatPayload;
  }[];
}
export interface Endpoint2 {
  url: string;
  headers?: {
    [k: string]: string;
  };
}
