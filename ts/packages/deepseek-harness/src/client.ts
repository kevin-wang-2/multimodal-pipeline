import type { Context } from "@deepseek-ai/cordis";
import type { ToolCallViewProps } from "@deepseek-ai/dsh-client-ui-tool/client";
import type {} from "@deepseek-ai/dsh-client-ui-slots";
import type {} from "@deepseek-ai/dsh-client-ui-renderer/client";
import { createElement as h } from "react";
import type { RunResult } from "./runtime.js";

export const name = "mmp-media-view";
export const inject = ["slots"];

/** 只消费落盘的工具结果；不使用临时交付 URL，也不从最终回复猜测产物。 */
export function MediaResult(props: ToolCallViewProps) {
  if (props.phase !== "result") return h("div", { role: "status" }, "MMP 处理中…");
  const block = props.block;
  const text = block.content.filter((part) => part.type === "text").map((part) => part.text).join("\n");
  if (block.isError) return h("div", { role: "alert" }, text);
  const value = block.meta as RunResult | undefined;
  if (!value || !Array.isArray(value.assets)) return h("pre", { style: { whiteSpace: "pre-wrap" } }, text);
  const retained = new Set(block.content.flatMap((part) => part.type === "file" ? [part.attachment.attachmentId] : []));
  const assets = value.assets.filter((asset) => retained.has(asset.attachment.attachmentId));
  return h("section", { "aria-label": "MMP 结果" },
    h("details", null, h("summary", null, `${value.job.type} · ${assets.length} 个媒体文件`),
      h("pre", { style: { whiteSpace: "pre-wrap", overflowWrap: "anywhere" } }, text)),
    ...assets.map((asset) => {
      // Replay metadata must never select arbitrary URLs or executable markup.
      const href = `/api/mmp-media?asset=${encodeURIComponent(asset.id)}`;
      const label = `${asset.field} ${asset.index + 1} · ${asset.attachment.name}`;
      const view = /^(image\/(png|jpeg|gif|webp))$/.test(asset.contentType)
        ? h("img", { src: href, alt: label, loading: "lazy", style: { maxWidth: "100%", maxHeight: "480px", objectFit: "contain" } })
        : asset.contentType.startsWith("audio/")
          ? h("audio", { src: href, controls: true, preload: "metadata", "aria-label": label, style: { maxWidth: "100%" } })
          : asset.contentType.startsWith("video/")
            ? h("video", { src: href, controls: true, preload: "metadata", "aria-label": label, style: { maxWidth: "100%", maxHeight: "480px" } })
            : null;
      return h("figure", { key: asset.id, style: { margin: "12px 0" } }, view,
        h("figcaption", null, h("a", { href: `${href}&download=1`, download: asset.attachment.name }, `下载 ${label}`)));
    }));
}

export function apply(ctx: Context): void {
  ctx.slots.inject("tool.call.toolview", () => ctx.slots.register({ name: "tool.call.toolview", key: "mmp_run" }, MediaResult));
}
