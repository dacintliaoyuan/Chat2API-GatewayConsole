/* console · src/console/ui.ts：R9UI 移植到 TypeScript */
// G8: number format (null→"—", <¥0.01 threshold)
export function fmtNum(n: number): string {
  return n == null ? "—" : n.toLocaleString("zh-CN");
}
export function fmtCny(n: number | null): string {
  if (n == null) return "—";
  if (n === 0) return "¥0.00";
  if (n < 0.01) return "<¥0.01";
  return "¥" + n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
export function fmtTokens(n: number): string {
  if (n == null) return "—";
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
  return fmtNum(n);
}
// G8: non-zero small fraction shows "<0.1%", never "0.0%"
export function fmtPct(frac: number): string {
  if (!isFinite(frac) || frac <= 0) return "0%";
  if (frac < 0.001) return "<0.1%";
  return (frac * 100).toFixed(1) + "%";
}

// Pill colors via CSS: --pill-ok/warn/err/neutral/accent
export function pill(label: string, cls?: string): HTMLSpanElement {
  const s = document.createElement("span");
  s.className = "pill" + (cls ? " " + cls : "");
  s.textContent = label;
  return s;
}
export function pillOk(label: string): HTMLSpanElement { return pill(label, "pill-ok"); }
export function pillWarn(label: string): HTMLSpanElement { return pill(label, "pill-warn"); }
export function pillErr(label: string): HTMLSpanElement { return pill(label, "pill-err"); }
export function pillNeutral(label: string): HTMLSpanElement { return pill(label, ""); }
export function pillAccent(label: string): HTMLSpanElement { return pill(label, "pill-accent"); }

// CheckedAt badge in muted style
export function checkedAt(text: string): HTMLElement {
  const span = document.createElement("span");
  span.className = "muted";
  span.textContent = text;
  return span;
}

// Section builder with title + aux
export function section(host: HTMLElement, title: string, aux?: string): HTMLElement {
  const s = document.createElement("section");
  const h = document.createElement("div");
  h.className = "section-h";
  h.appendChild(document.createTextNode(title));
  if (aux !== undefined) {
    const a = document.createElement("span");
    a.className = "aux";
    a.textContent = aux;
    h.appendChild(a);
  }
  s.appendChild(h);
  host.appendChild(s);
  return s;
}

// kvGrid container
export function kvGrid(): HTMLElement {
  const d = document.createElement("div");
  d.className = "kv-grid";
  return d;
}

// kvRow: key/value pair (value may be DOM or string)
export function kvRow(k: string, v: string | HTMLElement): HTMLElement {
  const r = document.createElement("div");
  r.className = "kv-row";
  r.appendChild(document.createTextNode(k));
  const vEl = document.createElement("span");
  vEl.className = "v";
  if (typeof v === "string") {
    vEl.textContent = v;
  } else {
    vEl.appendChild(v);
  }
  r.appendChild(vEl);
  return r;
}

// proxyLine renderer for system/custom/direct modes
export function proxyLine(p: { mode: "system" | "custom"; custom?: string }): string {
  if (p.mode === "system") return "跟随系统代理（未设置自定义代理）";
  if (p.mode === "custom") return "自定义代理：" + (p.custom ?? "（未填写）");
  return "直连（不使用代理）";
}

// Toast: bottom aria-live, auto-remove after 3800ms
export function toast(msg: string, kind: "ok" | "err" = "ok"): void {
  const t = document.createElement("div");
  t.className = "toast" + (kind === "err" ? " toast-err" : " toast-ok");
  t.textContent = msg;
  document.getElementById("toasts")?.appendChild(t);
  setTimeout(() => t.remove(), 3800);
}

// simBusy: spinner+disable button during async flow (1100ms then done callback)
export function simBusy(btn: HTMLButtonElement, busyText: string, done: () => void): void {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = busyText;
  const sp = document.createElement("span");
  sp.className = "spin";
  btn.insertBefore(sp, btn.firstChild);
  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = old;
    done();
  }, 1100);
}

// Skeleton loader for first-time page entry (hero+cards+lines approximation)
export function skeletonFor(hero: boolean, cards: boolean): HTMLElement {
  const w = document.createElement("div");
  w.className = "skel-wrap";
  if (hero) w.appendChild(skeletonItem("hero"));
  if (cards) {
    const row = document.createElement("div");
    row.style.cssText = "display:grid;grid-template-columns:repeat(3,1fr);gap:14px";
    row.appendChild(skeletonItem("card"));
    row.appendChild(skeletonItem("card"));
    row.appendChild(skeletonItem("card"));
    w.appendChild(row);
  }
  w.appendChild(skeletonItem("w60"));
  w.appendChild(skeletonItem("w80"));
  w.appendChild(skeletonItem("w40"));
  return w;
}
function skeletonItem(cls: string): HTMLElement {
  const el = document.createElement("div");
  el.className = "skeleton " + cls;
  return el;
}
