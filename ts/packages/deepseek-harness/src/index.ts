import type { Context } from "@deepseek-ai/cordis";
import type {} from "@deepseek-ai/dsh-agent";
import type {} from "@deepseek-ai/dsh-client-connection";
import type {} from "@deepseek-ai/dsh-system-prompt";
import type { ToolDefinition } from "@deepseek-ai/dsh-tools";
import type { JobRequest } from "@mmp/protocol";
import { configure, type Config } from "./config.js";
import { isRecord, resultContent, Runtime, type RunResult } from "./runtime.js";

export const name = "mmp";
export const inject = ["agents", "attachments", "tools", "fs", "systemPrompt", "connection"];
export type { Config } from "./config.js";

const object = { type: "object", additionalProperties: true } as const;
const string = { type: "string", minLength: 1 } as const;

/** 内网宿主插件：不提供 broker，不参与引擎或模型实现。 */
export async function apply(ctx: Context, config: Config): Promise<void> {
  const runtime = new Runtime(ctx, configure(config));
  await runtime.start();
  ctx.effect(() => () => runtime.close());
  ctx.effect(() => ctx.connection.fetch.register({
    path: "/api/mmp-media", methods: ["GET", "HEAD"], requestBody: "buffered",
    fetch: (request) => runtime.artifacts.serve(request),
  }));
  ctx.on("agent/pre-step", async ({ signal }, next) => {
    const decision = await next();
    if (decision.kind === "reject" || !decision.messages.some((message) => message.content.some((block) => block.type === "image" || block.type === "file"))) return decision;
    return { ...decision, messages: await runtime.preflight(decision.messages, signal) };
  });
  const jsonOutput: ToolDefinition["output"] = {
    schema: object,
    render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }],
  };
  ctx.tools.register({
    name: "mmp_list", description: "发现内网 MMP 当前可用能力，只返回 id、purpose 和简短说明。用 mmp_describe 按需读取单项完整契约。",
    parameters: { type: "object", properties: {}, additionalProperties: false }, output: jsonOutput,
    async execute(_args, exec) {
      return runtime.task(exec.signal, async (signal) => ({ capabilities: (await runtime.client.capabilities(signal)).capabilities.map(({ capability }) => ({ id: capability.id, purpose: capability.purpose, description: capability.description ?? "" })) }));
    },
  });
  ctx.tools.register({
    name: "mmp_describe", description: "读取一项 MMP 能力的命名输入、输出、schema、媒体 MIME/cardinality 与可用档位。执行前先读取契约。",
    parameters: { type: "object", properties: { id: string }, required: ["id"], additionalProperties: false }, output: jsonOutput,
    async execute(args, exec) {
      if (!isRecord(args) || typeof args.id !== "string") throw new Error("id is required");
      const id = args.id;
      return runtime.task(exec.signal, (signal) => runtime.client.describe(id, signal));
    },
  });
  ctx.tools.register({
    name: "mmp_run",
    description: "按 mmp_describe 的命名字段调用 MMP。text/JSON 按原值传入；media 使用 {attachment_id}（当前会话附件）、{asset_id}（此前 MMP 产物）、{file_path}（宿主可读文件）、{get:{url,headers?},content_type?} 或 {ref}。cardinality.max>1 的媒体字段传数组，否则传单个对象。ref 必须同时给原结果的 node_id。无需提供 base64 或 delivery：插件自动传输大文件并保存媒体输出；输出 asset_id 可直接用于后续调用。",
    parameters: {
      type: "object", properties: {
        type: string, arguments: object, node_id: string,
        tier: { type: "string", enum: ["cpu", "gpu-fast", "gpu", "remote"] },
      }, required: ["type", "arguments"], additionalProperties: false,
    },
    output: {
      schema: object,
      render: (_args, value) => resultContent(value as unknown as RunResult),
      presentationMeta: (_args, value) => value,
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("mmp_run requires an owning Agent");
      if (!isRecord(args) || typeof args.type !== "string" || !isRecord(args.arguments)) throw new Error("type and arguments are required");
      const input = { type: args.type, arguments: args.arguments, ...(typeof args.node_id === "string" ? { node_id: args.node_id } : {}), ...(typeof args.tier === "string" ? { tier: args.tier as JobRequest["tier"] } : {}) };
      const agent = exec.agent;
      return runtime.task(exec.signal, (signal) => runtime.run(input, agent, signal));
    },
  });
  ctx.systemPrompt.section({
    name: "mmp:media", order: 80,
    text: "MMP 提供内网多模态预检与显式能力。预检块是附件的事实、缺口和能力 id，不代表已执行用户任务。未知内容与降级必须明确；未预检的 PDF 不属于图片预检。先按需 mmp_describe，再用 mmp_run；没有附件时也可用 mmp_list 发现生成类能力。媒体输入优先使用 attachment_id / asset_id；跨轮复用 ref 时必须带原结果 node_id。媒体产物会作为附件保存并显示，可继续作为输入。不要把完整能力清单或媒体 base64 放入提示。",
  });
}
