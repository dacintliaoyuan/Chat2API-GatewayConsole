// Issue #109 Tauri prototype: minimal status + live-log page.
//
// Read-only: the Rust command polls the local 8000/8001 and returns one
// snapshot. This page renders it and records measurement markers; it never logs
// in, writes a ledger or changes a service.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

type Tone = "good" | "bad" | "warn";

interface StatusView {
  value: string;
  detail: string;
  tone: Tone;
}

interface LogRow {
  at: number;
  level: string;
  text: string;
}

interface Snapshot {
  gateway: StatusView;
  console: StatusView;
  auth: StatusView;
  upstream: StatusView;
  logs: LogRow[];
  logError: string | null;
  statusError: string | null;
}

const POLL_MS = 250;
const MAX_ROWS = 400;

let visible = true;
let firstReady = false;
let polling = false;
const seen = new Map<string, number>();

function element(id: string): HTMLElement {
  const found = document.getElementById(id);
  if (!found) throw new Error(`missing element ${id}`);
  return found;
}

function clock(at: number): string {
  if (!Number.isFinite(at) || at <= 0) return "—";
  return new Date(at).toLocaleTimeString("zh-CN", { hour12: false });
}

function hash(row: LogRow): string {
  return `${row.at}|${row.level}|${row.text}`;
}

function marker(text: string): { id: string; written: number } | null {
  let id = "";
  let written = Number.NaN;
  for (const token of text.split(/\s+/)) {
    if (token.startsWith("id=")) {
      id = token.slice(3).replace(/[^A-Za-z0-9-]/g, "");
    } else if (token.startsWith("t=")) {
      written = Number(token.slice(2).replace(/[^0-9]/g, ""));
    }
  }
  return id && Number.isFinite(written) ? { id, written } : null;
}

function renderStatus(id: string, view: StatusView): void {
  const value = element(`${id}-value`);
  const detail = element(`${id}-detail`);
  value.textContent = view.value;
  value.dataset.tone = view.tone;
  detail.textContent = view.detail;
}

function rowElement(row: LogRow): HTMLElement {
  const root = document.createElement("div");
  root.className = "row";
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = clock(row.at);
  const level = document.createElement("span");
  level.className = "level";
  level.dataset.level = row.level.toLowerCase();
  level.textContent = row.level.toUpperCase();
  const text = document.createElement("span");
  text.className = "text";
  text.textContent = row.text;
  text.title = row.text;
  root.append(time, level, text);
  return root;
}

function renderLogs(rows: LogRow[]): void {
  const counts = new Map<string, number>();
  for (const row of rows) {
    const key = hash(row);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const fresh: LogRow[] = [];
  for (const row of rows) {
    const key = hash(row);
    const have = seen.get(key) ?? 0;
    const total = counts.get(key) ?? 0;
    if (total > have) {
      fresh.push(row);
      counts.set(key, total - 1);
      seen.set(key, total);
    }
  }
  if (fresh.length === 0) return;

  const list = element("logs");
  for (const row of [...fresh].reverse()) list.prepend(rowElement(row));
  while (list.children.length > MAX_ROWS) list.lastElementChild?.remove();
  element("log-count").textContent = `${list.children.length} 条`;

  const marked = fresh
    .map((row) => ({ marker: marker(row.text), row }))
    .filter((entry): entry is { marker: { id: string; written: number }; row: LogRow } =>
      entry.marker !== null
    );
  if (marked.length === 0) return;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      const at = Date.now();
      for (const { marker: found } of marked) {
        void invoke("record_marker", {
          name: "log_visible",
          atMs: at,
          extra: { id: found.id, writtenMs: found.written },
        });
      }
    });
  });
}

async function poll(): Promise<void> {
  if (!visible || polling) return;
  polling = true;
  try {
    const snapshot = await invoke<Snapshot>("snapshot");
    renderStatus("gateway", snapshot.gateway);
    renderStatus("console", snapshot.console);
    renderStatus("auth", snapshot.auth);
    renderStatus("upstream", snapshot.upstream);
    renderLogs(snapshot.logs);
    if (snapshot.logError) {
      element("poll-status").textContent = `日志读取失败：${snapshot.logError}`;
    } else if (snapshot.statusError) {
      element("poll-status").textContent = `状态读取失败：${snapshot.statusError}`;
    } else {
      element("poll-status").textContent = `轮询正常 · ${POLL_MS} ms · 只读`;
    }
    element("last-update").textContent = `最近更新 ${clock(Date.now())}`;
    if (!firstReady) {
      firstReady = true;
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          void invoke("record_marker", {
            name: "ui_interactive",
            atMs: Date.now(),
            extra: {},
          });
        });
      });
    }
  } catch (error) {
    element("poll-status").textContent = `连接失败：${String(error)}`;
  } finally {
    polling = false;
  }
}

void listen<number>("tray-restored", (event) => {
  visible = true;
  void invoke("record_marker", {
    name: "tray_restored_event",
    atMs: Date.now(),
    extra: { clickMs: event.payload },
  });
  void poll().then(() => {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        void invoke("record_marker", {
          name: "tray_restored_visible",
          atMs: Date.now(),
          extra: { clickMs: event.payload },
        });
      });
    });
  });
});

void listen("tray-hidden", () => {
  visible = false;
  void invoke("record_marker", {
    name: "tray_hidden_event",
    atMs: Date.now(),
    extra: {},
  });
});

let hiddenAt = 0;
document.addEventListener("visibilitychange", () => {
  void invoke("record_marker", {
    name: `visibility_${document.visibilityState}`,
    atMs: Date.now(),
    extra: {},
  });
  if (document.visibilityState === "hidden") {
    hiddenAt = Date.now();
  } else if (document.visibilityState === "visible" && hiddenAt) {
    const click = hiddenAt;
    hiddenAt = 0;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        void invoke("record_marker", {
          name: "tray_restored_visibility",
          atMs: Date.now(),
          extra: { hiddenMs: click },
        });
      });
    });
  }
});

element("hide").addEventListener("click", () => {
  void invoke("hide_to_tray");
});

element("quit").addEventListener("click", () => {
  void invoke("quit_app");
});

void invoke("record_marker", {
  name: "webview_ready",
  atMs: Date.now(),
  extra: {},
});
void poll();
setInterval(() => void poll(), POLL_MS);
