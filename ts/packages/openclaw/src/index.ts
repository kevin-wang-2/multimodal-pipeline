import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { unavailableKind, unavailableNote } from "./hook.js";

export { needsUnavailableNote, unavailableKind, unavailableNote } from "./hook.js";
export { triageBytes, makeClient } from "./triage.js";
export { loadConfig } from "./config.js";

export default definePluginEntry({
  id: "mmp",
  name: "multimodal-pipeline",
  description: "预检不可用时显式标注降级（音频预检由 tools.media 的 mmp-triage 条目完成）",
  register(api) {
    api.on("before_prompt_build", (event) => {
      const kind = unavailableKind(event.prompt);
      if (kind) return { prependContext: unavailableNote(kind) };
    }, { priority: 10, timeoutMs: 1000 });
  },
});
