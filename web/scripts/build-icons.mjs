// Console icons from the desktop shortcut icon (Rev12 F12-1: the browser tab, the
// installed app and the desktop shortcut show one image). Source:
// ai-gateway/assets/AI网关控制台.ico, whose frames are PNG (16-256 px).
// - public/favicon.ico: the .ico copied byte for byte (the browser picks a frame).
// - public/icons/*.png: the 256 px frame scaled in Chrome. The maskable icon keeps
//   the art inside the 80% safe zone on a white plate so launchers can crop it.
// Chrome comes from Playwright: PLAYWRIGHT_MODULE, or the workspace design-QA tools.
import { readFileSync, writeFileSync, mkdirSync, copyFileSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const source = resolve("../../assets/AI网关控制台.ico");
const ico = readFileSync(source);
const iconVersion = createHash("sha256").update(ico).digest("hex").slice(0, 12);
const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
let best = null;
for (let i = 0; i < ico.readUInt16LE(4); i++) {
  const at = 6 + i * 16;
  const size = ico[at] || 256, bytes = ico.readUInt32LE(at + 8), offset = ico.readUInt32LE(at + 12);
  const isPng = ico.subarray(offset, offset + 8).equals(PNG_MAGIC);
  if (isPng && (!best || size > best.size)) best = { size, data: ico.subarray(offset, offset + bytes) };
}
if (!best) throw Error(`${source}: no PNG frame to scale`);

const playwright = process.env.PLAYWRIGHT_MODULE || resolve("../../.ai-design-qa/tools/node_modules/playwright/index.mjs");
const { chromium } = await import(pathToFileURL(playwright).href);
const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage();
const art = `data:image/png;base64,${best.data.toString("base64")}`;
const render = (size, inset) => page.evaluate(async ({ art, size, inset }) => {
  const img = new Image();
  img.src = art;
  await img.decode();
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const cx = canvas.getContext("2d");
  cx.fillStyle = "#ffffff";
  cx.fillRect(0, 0, size, size);
  cx.imageSmoothingEnabled = true;
  cx.imageSmoothingQuality = "high";
  const side = Math.round(size * (1 - 2 * inset));
  cx.drawImage(img, Math.round((size - side) / 2), Math.round((size - side) / 2), side, side);
  return canvas.toDataURL("image/png").split(",")[1];
}, { art, size, inset });

mkdirSync("public/icons", { recursive: true });
copyFileSync(source, "public/favicon.ico");
for (const [name, size, inset] of [["apple-touch-icon", 180, 0], ["icon-192", 192, 0], ["icon-512", 512, 0], ["icon-maskable-512", 512, 0.1]])
  writeFileSync(`public/icons/${name}.png`, Buffer.from(await render(size, inset), "base64"));
// Keep the desktop art transparent inside the existing 42px brand slot.
writeFileSync("public/icons/icon-brand.png", best.data);
writeFileSync("public/icons/desktop-brand.css", [
  ".workbench .wb-brand-mark {",
  "  background-color: transparent !important;",
  `  background-image: url(\"./icon-brand.png?v=${iconVersion}\");`,
  "  background-size: contain;",
  "  background-position: center;",
  "  background-repeat: no-repeat;",
  "}",
  ".workbench .wb-brand-mark > svg { visibility: hidden; }",
  "",
].join("\n"));

function updateIconLinks(path) {
  let html = readFileSync(path, "utf8");
  const newline = html.includes("\r\n") ? "\r\n" : "\n";
  for (const asset of ["/favicon.ico", "/icons/apple-touch-icon.png", "/manifest.webmanifest"]) {
    const escaped = asset.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    html = html.replace(new RegExp(`href="${escaped}(?:\\?[^"\\s]*)?"`, "g"), `href="${asset}?v=${iconVersion}"`);
  }
  const link = `    <link rel="stylesheet" data-desktop-brand href="/icons/desktop-brand.css?v=${iconVersion}" />`;
  const existing = /^.*<link[^>]*data-desktop-brand[^>]*>[^\r\n]*/m;
  html = existing.test(html) ? html.replace(existing, link)
    : html.replace("  </head>", `${link}${newline}  </head>`);
  writeFileSync(path, html);
}

let manifest = readFileSync("public/manifest.webmanifest", "utf8");
manifest = manifest.replace(/"src": "(icons\/[^"?]+\.png)(?:\?[^"\s]*)?"/g,
  (_, asset) => `"src": "${asset}?v=${iconVersion}"`);
writeFileSync("public/manifest.webmanifest", manifest);
updateIconLinks("index.html");

// Explicit, asset-only hosting update: preserve every existing JS/CSS bundle.
// A normal icon export never deploys a page or restarts a service.
if (process.argv.includes("--sync-hosted-icons")) {
  for (const output of ["dist", "release/candidate"]) {
    if (!existsSync(`${output}/index.html`)) continue;
    mkdirSync(`${output}/icons`, { recursive: true });
    copyFileSync("public/favicon.ico", `${output}/favicon.ico`);
    copyFileSync("public/manifest.webmanifest", `${output}/manifest.webmanifest`);
    for (const name of ["apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "icon-brand.png", "desktop-brand.css"])
      copyFileSync(`public/icons/${name}`, `${output}/icons/${name}`);
    updateIconLinks(`${output}/index.html`);
  }
}
await browser.close();
console.log(`Console desktop icon ${iconVersion} exported (brand, favicon and home-screen icons).`);
