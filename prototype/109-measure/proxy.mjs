// Fixed-port TCP proxy in front of the isolated candidate console. Killing it
// simulates a console outage without touching the running 8001 console.
//
// Usage:
//   node proxy.mjs --listen 18010 --target 127.0.0.1:51726

import net from "node:net";

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const listenPort = Number(arg("listen", "18010"));
const target = arg("target", "127.0.0.1:8001");
const [targetHost, targetPortText] = target.split(":");
const targetPort = Number(targetPortText);

if (!Number.isInteger(listenPort) || listenPort <= 0) throw new Error("--listen must be a port");
if (!targetHost || !Number.isInteger(targetPort)) throw new Error("--target must be host:port");

const server = net.createServer((client) => {
  const upstream = net.connect({ host: targetHost, port: targetPort }, () => {
    client.pipe(upstream);
    upstream.pipe(client);
  });
  const close = () => {
    client.destroy();
    upstream.destroy();
  };
  client.on("error", close);
  upstream.on("error", close);
  client.on("close", close);
  upstream.on("close", close);
});

server.listen(listenPort, "127.0.0.1", () => {
  console.log(`proxy listening on 127.0.0.1:${listenPort} -> ${target}`);
});
