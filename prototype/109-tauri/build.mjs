import { build } from "esbuild";
import { cp, mkdir } from "node:fs/promises";

await mkdir("dist", { recursive: true });
await build({
  entryPoints: ["src/main.ts"],
  bundle: true,
  format: "esm",
  target: "chrome105",
  outfile: "dist/main.js",
});
await cp("index.html", "dist/index.html");
await cp("src/style.css", "dist/style.css");
