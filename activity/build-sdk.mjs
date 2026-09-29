import { fileURLToPath } from "node:url";

import { build } from "esbuild";

await build({
  entryPoints: [
    fileURLToPath(new URL("./node_modules/@discord/embedded-app-sdk/output/index.mjs", import.meta.url)),
  ],
  outfile: fileURLToPath(new URL("./discord-sdk.js", import.meta.url)),
  bundle: true,
  platform: "browser",
  format: "esm",
  target: "es2022",
  legalComments: "none",
  minify: true,
  logLevel: "info",
});
