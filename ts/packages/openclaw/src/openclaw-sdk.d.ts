// 最小的 SDK 类型声明：插件在 OpenClaw 进程内加载，import 由宿主解析；这里只为 tsc 能编译。
declare module "openclaw/plugin-sdk/plugin-entry" {
  export interface PluginHookBeforePromptBuildEvent { prompt: string; messages: unknown[] }
  export interface PluginHookBeforePromptBuildResult { prependContext?: string; appendContext?: string }
  export interface PluginApi {
    on(name: "before_prompt_build", handler: (event: PluginHookBeforePromptBuildEvent, ctx: unknown) => PluginHookBeforePromptBuildResult | void | Promise<PluginHookBeforePromptBuildResult | void>, opts?: { priority?: number; timeoutMs?: number }): void;
    logger?: { info(msg: string): void; warn(msg: string): void };
  }
  export function definePluginEntry(def: { id: string; name: string; description?: string; register(api: PluginApi): void }): unknown;
}
