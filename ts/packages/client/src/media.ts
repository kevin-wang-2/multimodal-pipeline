import type { MediaHandle } from "@mmp/protocol";

/** 媒体句柄构造器（协议.md §5）。 */
export const media = {
  /** 小媒体内联（≤ 8 MiB）。接受字节或已编码的 base64。 */
  inline(data: Uint8Array | ArrayBuffer | string, contentType?: string): MediaHandle {
    const b64 = typeof data === "string" ? data : toBase64(data instanceof Uint8Array ? data : new Uint8Array(data));
    return contentType ? { inline: b64, content_type: contentType } : { inline: b64 };
  },
  /** 带认证的 GET 端点（presigned URL、宿主自带 token 的接口）。 */
  get(url: string, headers?: Record<string, string>, contentType?: string): MediaHandle {
    const h: MediaHandle = { get: headers ? { url, headers } : { url } };
    if (contentType) h.content_type = contentType;
    return h;
  },
  /** 内容引用：此前任一响应里的 media_id。A 本地有就不再拉（v1.1）。 */
  ref(mediaId: string): MediaHandle {
    return { ref: mediaId };
  },
  /** ref + 来源：A 没有时才拉，拉完校验 hash。 */
  refOr(mediaId: string, source: MediaHandle): MediaHandle {
    return { ...source, ref: mediaId };
  },
  /** 给句柄加大输出的 PUT 端点。 */
  withPut(handle: MediaHandle, url: string, headers?: Record<string, string>): MediaHandle {
    return { ...handle, put: headers ? { url, headers } : { url } };
  },
};

function toBase64(bytes: Uint8Array): string {
  if (typeof Buffer !== "undefined") return Buffer.from(bytes).toString("base64");
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
