import { build } from "esbuild";

// Harness 的浏览器宿主执行模块工厂；React 必须取自宿主模块表，避免第二份 React。
await build({
  entryPoints: ["src/client.ts"], outfile: "dist/client.js", bundle: true,
  platform: "browser", target: "es2022", format: "cjs", external: ["react"], sourcemap: true,
  banner: { js: 'window.__ModuleLoader__.load({ id: "@mmp/deepseek-harness", factory: (require) => { var module = { exports: {} }; var exports = module.exports;' },
  footer: { js: "return module.exports; } });" },
});
