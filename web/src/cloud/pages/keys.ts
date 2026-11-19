import { escapeHtml } from "../../html";
import { fmtNum } from "../../console/ui";
import type { ShareKeyRow } from "../../console/share-model";
import {
  lastValue,
  readShareKeySecret,
  type KeySecretOutcome,
} from "../cloud-reads";
import { shareKeyWrite, type KeyWriteOutcome } from "../cloud-writes";
import {
  TEST_REASON,
  isRecord,
  newIntentKey,
  plainNotice,
  reasonWord,
  revokeKey,
  submitCreate,
  submitConcurrency,
} from "../operation-intent";
import { clock, failedLine, svgIcon } from "../cloud-format";
import type {
  CloudModel,
  ConfirmCopy,
  Feedback,
  KeyNotice,
  KeyTest,
  KeyUi,
} from "../model";

/** Ticket #10: an inline test result stays visible for a few seconds, then the
 *  row's button returns to 测试. The result itself is never persisted. */
const KEY_TEST_RESULT_MS = 5_000;

function createKeyButton(
  model: CloudModel,
  label: string,
  cls = "btn btn-primary btn-sm"
): string {
  const writable = model.writeReady();
  return `<button type="button" class="${cls}" data-cloud-key-create-open${writable.ok ? "" : ` disabled title="${escapeHtml(writable.reason)}"`}>${svgIcon("key")}${escapeHtml(label)}</button>`;
}

/** Ticket #10: the test result stays in the row's own button for a few
 *  seconds: 测试 → 测试中… → 通过 · 耗时 / 失败 · 原因 → 测试. The
 *  elapsed time is the backend's own measurement; a missing one is 未知. */
function testElapsedText(ms: number | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0)
    return "耗时未知";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}
function keyTestLabel(test: KeyTest | undefined): string {
  if (!test) return "测试";
  if (test.state === "busy") return "测试中…";
  if (test.state === "ok") return `通过 · ${testElapsedText(test.elapsedMs)}`;
  return `失败 · ${test.reason || "后端未给出原因"}`;
}
function keyTestTone(test: KeyTest | undefined): string {
  if (!test) return "neutral";
  if (test.state === "busy") return "warn";
  return test.state === "ok" ? "ok" : "err";
}

/** D18-7: one Key row. The name sits above the mask, the mask is the main
 *  visual, the concurrency number is edited in place and 撤销 is the row's only
 *  destructive action. Clicking the row swaps the mask for the full plaintext and
 *  adds 复制; clicking it again collapses. */
function keyRowHtml(model: CloudModel, key: ShareKeyRow): string {
  const id = String(key.id);
  const secret = model.keySecret(id);
  const revealed = secret?.revealed === true;
  const notice = model.keyNotice(id);
  const writable = model.writeReady();
  const capValue =
    model.keyCapValue(id) ??
    (typeof key.max_concurrency === "number"
      ? String(key.max_concurrency)
      : "");
  const test = model.keyTest(id);
  const disabled = !writable.ok
    ? ` disabled title="${escapeHtml(writable.reason)}"`
    : model.keyBusy(id)
      ? ` disabled title="同一操作正在进行"`
      : test?.state === "busy"
        ? ` disabled title="同一 Key 正在测试"`
        : "";
  const testReason =
    key.enabled !== true
      ? "Key 已停用，无法测试"
      : typeof key.row_version !== "number" || key.row_version < 1
        ? "缺少行版本，请刷新列表"
        : test?.state === "busy"
          ? "同一 Key 已有测试在进行"
          : "";
  const testDisabled = disabled
    ? disabled
    : testReason
      ? ` disabled title="${escapeHtml(testReason)}"`
      : "";
  return `<li class="cloud-key-row" data-cloud-key-row="${escapeHtml(id)}">
    <button type="button" class="cloud-key-main" data-cloud-key-action="reveal:${escapeHtml(id)}" aria-expanded="${revealed}">
      <span class="cloud-key-name">${escapeHtml(key.name || "（未命名）")}</span>
      ${
        revealed
          ? `<code class="cloud-key-plain mono">${escapeHtml(secret?.secret ?? "")}</code>`
          : `<span class="cloud-key-mask mono">${escapeHtml(key.masked || "—")}</span>`
      }
    </button>
    <div class="cloud-key-row-actions">
      ${
        revealed
          ? `<button type="button" class="cloud-key-copy" data-cloud-key-action="copy:${escapeHtml(id)}">复制</button>`
          : ""
      }
      <input class="cloud-key-cap mono" type="number" min="1" max="48" step="1" inputmode="numeric" value="${escapeHtml(capValue)}" data-cloud-key-cap-input="${escapeHtml(id)}" aria-label="Key ${escapeHtml(key.name || id)} 的并发上限"${disabled}>
      <button type="button" class="cloud-key-rename" data-cloud-key-action="rename:${escapeHtml(id)}"${disabled}>改名</button>
      <button type="button" class="cloud-key-test-btn" data-cloud-key-action="test:${escapeHtml(id)}" data-tone="${keyTestTone(test)}"${testDisabled}>${escapeHtml(keyTestLabel(test))}</button>
      <button type="button" class="cloud-key-revoke" data-cloud-key-action="revoke:${escapeHtml(id)}"${disabled}>撤销</button>
    </div>
    ${notice ? `<p class="cloud-key-notice" data-tone="${notice.tone}">${escapeHtml(notice.text)}</p>` : ""}
  </li>`;
}

export function keysCard(model: CloudModel): string {
  const keys = lastValue(model.keys);
  const statusRead = lastValue(model.status);
  // The Key list header owns the one read-only global concurrency line. A stale
  // status read cannot state a current cap or in-flight count, so both stay
  // unknown and the line says why.
  const status = statusRead && !statusRead.stale ? statusRead.value : null;
  const cap =
    typeof status?.share_max_concurrency === "number"
      ? status.share_max_concurrency
      : null;
  const inFlight =
    typeof status?.capacity?.in_flight === "number"
      ? status.capacity.in_flight
      : null;
  const concurrencyTitle = statusRead?.stale
    ? `状态读数已过时（上次 ${clock(statusRead.at)}），当前并发未知`
    : "当前在途 / 全局配置上限（只读）";
  const concurrency = `并发 ${inFlight === null ? "未上报" : fmtNum(inFlight)} / ${cap === null ? "未上报" : fmtNum(cap)}`;
  const keysStale = keys?.stale === true;
  const keysStaleNote = keysStale
    ? `<p class="cloud-read-note" data-tone="warn">Key 读数已过时（上次 ${clock(keys.at)}）：列表与已启用 Key 数当前未知</p>`
    : "";
  const head = `<div class="card-head"><h3>API Key</h3><span class="card-meta">${keys ? `共 ${keys.value.length} 个${keysStale ? "（读数已过时）" : ""}` : ""}</span><span class="cloud-key-concurrency mono" title="${escapeHtml(concurrencyTitle)}">${escapeHtml(concurrency)}</span><span class="card-grow"></span>
    ${keys && keys.value.length === 0 ? "" : createKeyButton(model, "新建 Key")}</div>`;
  if (!keys) {
    return `<section class="card cloud-keys" aria-label="API Key">${head}${failedLine(model.keys)}${model.keys.state === "loading" ? `<p class="card-foot">读取中…</p>` : ""}</section>`;
  }
  const empty = keys.value.length
    ? ""
    : `<div class="cloud-key-empty"><p>还没有 Key。新建一个长期 Key，只填名字与并发。</p>${createKeyButton(model, "新建 Key")}</div>`;
  return `<section class="card cloud-keys" aria-label="API Key">${head}${failedLine(model.keys)}${keysStaleNote}
    ${keys.value.length ? `<ul class="cloud-key-list">${keys.value.map((key) => keyRowHtml(model, key)).join("")}</ul>` : ""}
    ${empty}
    <p class="card-foot">Key 长期有效。点开一行看完整明文并复制，再点收起；明文只留在当前页面内存，不进入地址栏、浏览器存储或日志。并发为单 Key 配置上限（1–48），改完立即生效。</p>
  </section>`;
}

export type KeysPageContext = {
  model: CloudModel;
  keyUi: KeyUi;
  content: HTMLElement;
  createDialog: HTMLDialogElement;
  createForm: HTMLFormElement;
  createSubmit: HTMLButtonElement;
  createErrorBox: HTMLElement;
  plainDialog: HTMLDialogElement;
  plainTitle: HTMLElement;
  plainValue: HTMLElement;
  plainNote: HTMLElement;
  plainToggle: HTMLButtonElement;
  plainCopy: HTMLButtonElement;
  render: () => void;
  refreshKeys: () => Promise<void>;
  refreshUsage: () => Promise<void>;
  loadRequestsPage: (page: number, reset?: boolean) => Promise<void>;
  addFeedback: (entry: Omit<Feedback, "key" | "at" | "done">) => Feedback;
  settle: (row: Feedback, tone: Feedback["tone"], text: string) => void;
  keyRow: (id: string) => ShareKeyRow | null;
  keyLabel: (row: ShareKeyRow | null, id: string) => string;
  confirmWith: (
    copy: ConfirmCopy,
    run: () => void,
    force?: { label: string; run: () => void }
  ) => void;
};

export function createKeysPage(ctx: KeysPageContext) {
  const {
    model,
    keyUi,
    content,
    createDialog,
    createForm,
    createSubmit,
    createErrorBox,
    plainDialog,
    plainTitle,
    plainValue,
    plainNote,
    plainToggle,
    plainCopy,
    render,
    refreshKeys,
    refreshUsage,
    loadRequestsPage,
    addFeedback,
    settle,
    keyRow,
    keyLabel,
    confirmWith,
  } = ctx;

  function setNotice(id: string, tone: KeyNotice["tone"], text: string): void {
    keyUi.notices.set(id, { tone, text, at: Date.now() });
    render();
  }
  function clearNotice(id: string): void {
    keyUi.notices.delete(id);
    render();
  }

  async function copyText(text: string): Promise<boolean> {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }

  function setCreateError(text: string): void {
    keyUi.createError = text || null;
    createErrorBox.textContent = text;
  }

  function openCreate(): void {
    const writable = model.writeReady();
    if (!writable.ok) {
      const text = `未打开新建：${writable.reason}`;
      settle(
        addFeedback({
          action: "share.key_create",
          target: "share",
          tone: "warn",
          text,
          opId: null,
          intentKey: null,
        }),
        "warn",
        text
      );
      return;
    }
    if (!keyUi.createIdem) keyUi.createIdem = newIntentKey();
    createForm.reset();
    const capInput = createForm.querySelector<HTMLInputElement>(
      'input[name="max_concurrency"]'
    );
    if (capInput) capInput.value = "16";
    setCreateError("");
    createDialog.showModal();
    createForm.querySelector<HTMLInputElement>('input[name="name"]')?.focus();
  }

  async function createKey(): Promise<void> {
    if (keyUi.createBusy) return;
    const ready = model.writeReady();
    if (!ready.ok) {
      setCreateError(ready.reason);
      return;
    }
    const nameInput =
      createForm.querySelector<HTMLInputElement>('input[name="name"]');
    const capInput = createForm.querySelector<HTMLInputElement>(
      'input[name="max_concurrency"]'
    );
    if (!nameInput || !capInput) return;
    const submission = submitCreate(nameInput.value, capInput.value);
    if (submission.kind === "invalid") {
      setCreateError(submission.reason);
      (submission.field === "name" ? nameInput : capInput).focus();
      return;
    }
    const name = submission.body.name;
    keyUi.createBusy = true;
    createSubmit.disabled = true;
    createSubmit.textContent = "创建中…";
    setCreateError("");
    const body: Record<string, unknown> = {
      name,
      max_concurrency: submission.body.max_concurrency,
      idempotency_key: keyUi.createIdem,
    };
    const outcome = await shareKeyWrite(
      "POST",
      "/api/console/share-management/keys",
      body
    );
    keyUi.createBusy = false;
    createSubmit.disabled = false;
    createSubmit.textContent = "创建";
    if (!outcome.ok) {
      setCreateError(
        outcome.kind === "no-answer"
          ? `结果未确认：${outcome.reason}；同一幂等键已保留，可安全重试`
          : `创建失败：${outcome.reason}`
      );
      if (outcome.kind === "no-answer") void refreshKeys();
      else keyUi.createIdem = newIntentKey();
      return;
    }
    const key = isRecord(outcome.data.key) ? outcome.data.key : null;
    const plaintext =
      typeof outcome.data.plaintext === "string" ? outcome.data.plaintext : "";
    const createdName =
      typeof key?.name === "string" && key.name ? key.name : name;
    const viewable =
      typeof key?.plaintext_viewable === "boolean"
        ? key.plaintext_viewable
        : null;
    keyUi.createIdem = newIntentKey();
    createDialog.close("created");
    if (plaintext) openPlain("created", createdName, plaintext, viewable);
    else {
      const text =
        "已创建，但响应缺少明文；请再点开该行尝试查看明文（后端允许时），否则轮换生成";
      settle(
        addFeedback({
          action: "share.key_create",
          target: "share",
          tone: "warn",
          text,
          opId: null,
          intentKey: null,
        }),
        "warn",
        text
      );
    }
    void refreshKeys();
    void refreshUsage();
  }

  function openPlain(
    kind: "created" | "rotated",
    name: string,
    value: string,
    viewable: boolean | null
  ): void {
    // D18-4: a freshly created Key is shown in full once so the operator can
    // copy it before closing. A rotated Key keeps the old R4 masked-until-asked
    // behaviour; the value stays in this page's memory only.
    keyUi.plain = {
      kind,
      name,
      value,
      at: Date.now(),
      hidden: kind !== "created",
      viewable,
    };
    renderPlain();
    plainDialog.showModal();
    (keyUi.plain.hidden ? plainToggle : plainCopy).focus();
  }

  function renderPlain(): void {
    const plain = keyUi.plain;
    if (!plain) return;
    plainTitle.textContent = `${plain.kind === "created" ? "已创建" : "已轮换"} API Key：${plain.name || "（未命名）"}`;
    plainValue.textContent = plain.hidden ? "••••••••••••••••" : plain.value;
    plainToggle.hidden = plain.kind === "created";
    plainToggle.textContent = plain.hidden ? "显示明文" : "隐藏明文";
    plainToggle.setAttribute("aria-pressed", String(!plain.hidden));
    plainNote.textContent = plainNotice(plain.kind, plain.viewable);
  }

  async function revealSecret(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    if (row.plaintext_viewable === false) {
      setNotice(id, "warn", "后端未保存可回查的密文；只能轮换生成新 Key");
      return;
    }
    if (row.plaintext_viewable !== true) {
      setNotice(
        id,
        "warn",
        "明文状态未上报：不假设可查看，也不自动轮换；请刷新列表核对"
      );
      return;
    }
    const rowVersion = row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    const busyKey = `secret:${id}`;
    if (keyUi.busy.has(busyKey) || keyUi.secrets.has(id)) return;
    keyUi.busy.add(busyKey);
    render();
    const outcome = await readShareKeySecret(id, rowVersion);
    keyUi.busy.delete(busyKey);
    if (!outcome.ok) {
      setNotice(
        id,
        outcome.kind === "no-answer" ? "warn" : "err",
        secretFailure(outcome)
      );
      return;
    }
    keyUi.secrets.set(id, {
      secret: outcome.secret,
      rowVersion,
      at: Date.now(),
      revealed: true,
    });
    keyUi.notices.delete(id);
    render();
  }

  /** D18-7: clicking the row toggles the plaintext in place. The secret stays
   *  revealed until the operator clicks the row again; there is no auto-remask. */
  function toggleReveal(id: string): void {
    if (keyUi.secrets.has(id)) {
      keyUi.secrets.delete(id);
      render();
      return;
    }
    void revealSecret(id);
  }
  function secretFailure(
    outcome: Extract<KeySecretOutcome, { ok: false }>
  ): string {
    const reason = outcome.reason;
    if (reason === "legacy") return "旧密钥不可恢复，手动轮换后可见";
    if (reason === "undecryptable")
      return "密文不可解密（换身份或迁机后不可恢复），手动轮换后可见";
    if (reason === "version_conflict") return "列表版本已变化，刷新后重看";
    if (reason === "key_deleted") return "Key 已删除";
    if (reason === "key_not_found") return "Key 不存在，请刷新列表";
    return outcome.kind === "no-answer"
      ? `结果未确认：${reason}`
      : `明文读取失败：${reason}`;
  }

  function hideSecret(id: string): void {
    keyUi.secrets.delete(id);
    render();
  }
  function toggleSecret(id: string): void {
    const entry = keyUi.secrets.get(id);
    if (!entry) return;
    entry.revealed = !entry.revealed;
    render();
  }
  async function copySecret(id: string): Promise<void> {
    const entry = keyUi.secrets.get(id);
    if (!entry) return;
    const ok = await copyText(entry.secret);
    setNotice(
      id,
      ok ? "ok" : "warn",
      ok ? "已复制该 Key 明文" : "复制失败：浏览器拒绝写入剪贴板"
    );
  }

  async function saveCap(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const input = content.querySelector<HTMLInputElement>(
      `[data-cloud-key-cap-input="${CSS.escape(id)}"]`
    );
    const submission = submitConcurrency(
      input?.value ?? "",
      row.max_concurrency
    );
    if (submission.kind === "invalid") {
      setNotice(id, "warn", submission.reason);
      input?.focus();
      return;
    }
    if (submission.unchanged) {
      setNotice(id, "neutral", "并发上限没有变化");
      return;
    }
    await patchKey(
      id,
      { max_concurrency: submission.value },
      `并发上限已保存为 ${submission.value}`
    );
  }

  /** Rename one Key. The PATCH carries the row version; a stale version is
   *  refused instead of overwriting a concurrent change. */
  async function renameKey(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const current = typeof row.name === "string" ? row.name : "";
    const next = window.prompt("新的 Key 名称（1–64 个字符）", current);
    if (next === null) return;
    const name = next.trim();
    if (!name || Array.from(name).length > 64) {
      setNotice(id, "warn", "名称需为 1–64 个字符");
      return;
    }
    if (name === current) return;
    // The prompt-time version is the PATCH precondition: a concurrent rename or
    // concurrency change while the prompt is open must be refused, not overwritten.
    await patchKey(id, { name }, "已改名", row.row_version);
  }

  async function patchKey(
    id: string,
    patch: Record<string, unknown>,
    successText: string,
    expectedVersion?: number
  ): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const ready = model.writeReady();
    if (!ready.ok) {
      setNotice(id, "warn", ready.reason);
      return;
    }
    const rowVersion =
      typeof expectedVersion === "number" ? expectedVersion : row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    const busyKey = `patch:${id}`;
    if (keyUi.busy.has(busyKey)) return;
    keyUi.busy.add(busyKey);
    render();
    const outcome = await shareKeyWrite(
      "PATCH",
      `/api/console/share-management/keys/${encodeURIComponent(id)}`,
      {
        ...patch,
        expected_row_version: rowVersion,
      }
    );
    keyUi.busy.delete(busyKey);
    if (!outcome.ok) {
      keyFailure(id, outcome);
      return;
    }
    // A concurrency-only PATCH bumps row_version. The revealed plaintext is
    // still valid for this Key, so adopt the new version before the refresh
    // reconciles; otherwise reconcileSecrets would clear it and re-mask the row.
    const updatedKey = isRecord(outcome.data.key) ? outcome.data.key : null;
    const nextVersion =
      typeof updatedKey?.row_version === "number"
        ? updatedKey.row_version
        : null;
    const revealed = keyUi.secrets.get(id);
    if (revealed && nextVersion !== null) revealed.rowVersion = nextVersion;
    setNotice(id, "ok", successText);
    keyUi.caps.delete(id);
    void refreshKeys();
    void refreshUsage();
  }

  function keyFailure(
    id: string,
    outcome: Extract<KeyWriteOutcome, { ok: false }>
  ): void {
    // A failed write did not take effect: drop the optimistic input value so
    // the row shows the Key's actual concurrency again.
    keyUi.caps.delete(id);
    const text =
      outcome.kind === "conflict"
        ? `版本冲突：${outcome.reason}；列表已刷新，请按新版本重试`
        : outcome.kind === "no-answer"
          ? `结果未确认：${outcome.reason}；本页不会自动重发，请刷新列表核对后再操作`
          : `未完成：${reasonWord(outcome.reason)}`;
    keyUi.notices.set(id, {
      tone:
        outcome.kind === "no-answer" || outcome.kind === "conflict"
          ? "warn"
          : "err",
      text,
      at: Date.now(),
    });
    if (outcome.kind === "conflict" || outcome.kind === "no-answer")
      void refreshKeys();
    render();
  }

  async function rotateKey(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const rowVersion = row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    confirmWith(
      {
        title: "轮换这个 API Key？",
        danger: true,
        confirm: "轮换 Key",
        body: "轮换后旧 Key 立即失效；新明文在本次响应中显示并默认遮蔽。后端保存可回查密文时可在详情再次查看，否则关闭后只能再次轮换。需要先确保使用方可以更换 Key。",
        detail: `Key ${keyLabel(row, id)} · 行版本 ${rowVersion}`,
      },
      () =>
        void (async () => {
          const ready = model.writeReady();
          if (!ready.ok) {
            setNotice(id, "warn", ready.reason);
            return;
          }
          const busyKey = `rotate:${id}`;
          if (keyUi.busy.has(busyKey)) return;
          keyUi.busy.add(busyKey);
          render();
          const outcome = await shareKeyWrite(
            "POST",
            `/api/console/share-management/keys/${encodeURIComponent(id)}/rotate`,
            { expected_row_version: rowVersion }
          );
          keyUi.busy.delete(busyKey);
          if (!outcome.ok) {
            keyFailure(id, outcome);
            return;
          }
          keyUi.secrets.delete(id);
          keyUi.notices.delete(id);
          const plaintext =
            typeof outcome.data.plaintext === "string"
              ? outcome.data.plaintext
              : "";
          const key = isRecord(outcome.data.key) ? outcome.data.key : null;
          const name =
            typeof key?.name === "string" && key.name
              ? key.name
              : keyLabel(row, id);
          const viewable =
            typeof key?.plaintext_viewable === "boolean"
              ? key.plaintext_viewable
              : null;
          void refreshKeys();
          if (plaintext) openPlain("rotated", name, plaintext, viewable);
          else
            setNotice(
              id,
              "warn",
              "已轮换，但响应缺少明文；请在该 Key 详情尝试查看明文（后端允许时），否则再次轮换"
            );
        })()
    );
  }

  async function revokeKeyAction(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const request = revokeKey(id, row.row_version);
    if (request.kind === "invalid") {
      setNotice(id, "warn", request.reason);
      return;
    }
    confirmWith(
      {
        title: "撤销这个 API Key？",
        danger: true,
        confirm: "撤销",
        body: "撤销后不可恢复，正在使用该 Key 的客户端会立即失败；该 Key 会从列表消失，历史用量仍保留。确定撤销？",
        detail: `Key ${keyLabel(row, id)}`,
      },
      () =>
        void (async () => {
          const ready = model.writeReady();
          if (!ready.ok) {
            setNotice(id, "warn", ready.reason);
            return;
          }
          const busyKey = `revoke:${id}`;
          if (keyUi.busy.has(busyKey)) return;
          keyUi.busy.add(busyKey);
          render();
          const outcome = await shareKeyWrite(
            "POST",
            request.path,
            request.body
          );
          keyUi.busy.delete(busyKey);
          if (!outcome.ok) {
            keyFailure(id, outcome);
            return;
          }
          // D18-3: the revoke answer separates what the management side
          // released (PENDING/GRANTED), what it aborted (COMMITTED)
          // and what 8100 closed upstream. A missing count stays 未知, never 0.
          const count = (name: string) => {
            const value = outcome.data[name];
            return typeof value === "number" && Number.isFinite(value)
              ? fmtNum(value)
              : "未上报";
          };
          const note =
            typeof outcome.data.note === "string" && outcome.data.note
              ? `；${outcome.data.note}`
              : "";
          keyUi.secrets.delete(id);
          // 撤销成功后这一行会从列表消失，所以结果写页级反馈，
          // 不写在即将被移除的行上。
          addFeedback({
            action: "share.key_revoke",
            target: id,
            tone: outcome.data.terminated === true ? "ok" : "warn",
            text: `已撤销：终止 ${count("terminated")} · 释放 ${count("released")} · 中止 ${count("aborted")} · 关闭上游 ${count("upstream_closed")}${note}`,
            opId: null,
            intentKey: null,
          });
          void refreshKeys();
        })()
    );
  }

  async function deleteKey(id: string): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const rowVersion = row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    confirmWith(
      {
        title: "删除这个 API Key？",
        danger: true,
        confirm: "删除 Key",
        body: "删除后该 Key 从列表移除，不能恢复；历史用量仍保留在分享账本中。需要终止在途请求时请先用撤销。",
        detail: `Key ${keyLabel(row, id)} · 行版本 ${rowVersion}`,
      },
      () =>
        void (async () => {
          const ready = model.writeReady();
          if (!ready.ok) {
            setNotice(id, "warn", ready.reason);
            return;
          }
          const busyKey = `delete:${id}`;
          if (keyUi.busy.has(busyKey)) return;
          keyUi.busy.add(busyKey);
          render();
          const outcome = await shareKeyWrite(
            "DELETE",
            `/api/console/share-management/keys/${encodeURIComponent(id)}`,
            { expected_row_version: rowVersion }
          );
          keyUi.busy.delete(busyKey);
          if (!outcome.ok) {
            keyFailure(id, outcome);
            return;
          }
          keyUi.secrets.delete(id);
          keyUi.notices.delete(id);
          void refreshKeys();
          const text = `已删除 Key ${keyLabel(row, id)}；历史用量仍在账本中`;
          settle(
            addFeedback({
              action: "share.key_delete",
              target: "share",
              tone: "ok",
              text,
              opId: null,
              intentKey: null,
            }),
            "ok",
            text
          );
          render();
        })()
    );
  }

  /** Ticket #10: a finished test keeps its word and elapsed time in the row's
   *  own button for a few seconds. A newer test replaces the entry, so the old
   *  timer cannot clear the new result. */
  function setKeyTest(id: string, test: KeyTest): void {
    keyUi.tests.set(id, test);
    render();
    if (test.state === "busy") return;
    window.setTimeout(() => {
      if (keyUi.tests.get(id) !== test) return;
      keyUi.tests.delete(id);
      render();
    }, KEY_TEST_RESULT_MS);
  }

  async function testKey(id: string, privateTest = false): Promise<void> {
    const row = keyRow(id);
    if (!row) {
      setNotice(id, "warn", "Key 列表已变化，找不到该 Key");
      return;
    }
    const ready = model.writeReady();
    if (!ready.ok) {
      setNotice(id, "warn", ready.reason);
      return;
    }
    const rowVersion = row.row_version;
    if (
      typeof rowVersion !== "number" ||
      !Number.isInteger(rowVersion) ||
      rowVersion < 1
    ) {
      setNotice(id, "warn", "缺少行版本，请刷新列表后重试");
      return;
    }
    if (row.enabled !== true) {
      setNotice(id, "warn", "Key 已停用，启用后才能测试");
      return;
    }
    if (keyUi.tests.get(id)?.state === "busy") return;
    const testedFrom = privateTest ? "private" : "public";
    setKeyTest(id, { state: "busy", rowVersion, from: testedFrom });
    const path = `/api/console/share-management/keys/${encodeURIComponent(id)}/test${privateTest ? "-private" : ""}`;
    const outcome = await shareKeyWrite(
      "POST",
      path,
      { expected_row_version: rowVersion },
      100_000
    );
    // Ticket #10: the test is one real inference through the share gateway, so
    // it is metered like any other request. Re-read the ledger and the request
    // detail instead of inventing a row from the probe answer.
    void refreshUsage();
    if (model.usageRequests !== null && !model.usageRequestsUi.loading)
      void loadRequestsPage(model.usageRequestsUi.page);
    if (!outcome.ok) {
      setKeyTest(id, {
        state: "error",
        rowVersion,
        from: testedFrom,
        at: Date.now(),
        reason:
          outcome.kind === "no-answer"
            ? `结果未确认：${outcome.reason}`
            : outcome.reason,
      });
      return;
    }
    const data = outcome.data;
    const connected = data.connected === true;
    const expectedStage = privateTest ? "models" : "inference";
    const contractOk =
      data.key_id === id &&
      data.row_version === rowVersion &&
      typeof data.connected === "boolean" &&
      data.tested_from === testedFrom &&
      (connected
        ? data.stage === expectedStage &&
          data.http_status === 200 &&
          typeof data.model === "string" &&
          data.model.length > 0 &&
          (privateTest ||
            (typeof data.public_ip === "string" && data.public_ip.length > 0))
        : typeof data.reason === "string");
    if (!contractOk) {
      setKeyTest(id, {
        state: "error",
        rowVersion,
        from: testedFrom,
        reason: "探测结果形状不符",
        at: Date.now(),
      });
      return;
    }
    const reason = typeof data.reason === "string" ? data.reason : "";
    setKeyTest(id, {
      state: connected ? "ok" : "error",
      rowVersion,
      reason: connected
        ? ""
        : (TEST_REASON[reason] ?? reason) || "后端未给出原因",
      stage: typeof data.stage === "string" ? data.stage : undefined,
      httpStatus:
        typeof data.http_status === "number" ? data.http_status : undefined,
      model: typeof data.model === "string" ? data.model : undefined,
      elapsedMs:
        typeof data.elapsed_ms === "number" ? data.elapsed_ms : undefined,
      publicIp: typeof data.public_ip === "string" ? data.public_ip : undefined,
      from: testedFrom,
      at:
        typeof data.checked_at === "number"
          ? data.checked_at * 1000
          : Date.now(),
    });
  }

  function reconcileSecrets(keys = lastValue(model.keys)?.value): void {
    if (!keys) return;
    const byId = new Map(keys.map((row) => [row.id, row]));
    for (const [id, entry] of keyUi.secrets) {
      const row = byId.get(id);
      if (!row) {
        // The Key left the list (revoked or deleted): its plaintext must not
        // stay on screen.
        keyUi.secrets.delete(id);
        keyUi.tests.delete(id);
        continue;
      }
      // Ticket #5 / spec 180: 明文不会自动遮回 —— 改行内并发会让
      // row_version 前移，但密文本身没变，明文要留在屏幕上直到再点该行。
      // 存的版本跟着行前移，后续读取用当前版本比较。
      if (
        entry.rowVersion !== null &&
        typeof row.row_version === "number" &&
        row.row_version !== entry.rowVersion
      )
        entry.rowVersion = row.row_version;
    }
  }

  async function keyActionFor(raw: string): Promise<void> {
    const [action, ...rest] = raw.split(":");
    const id = rest.join(":");
    if (!id) return;
    if (action === "cap") await saveCap(id);
    else if (action === "reveal") toggleReveal(id);
    else if (action === "show") toggleSecret(id);
    else if (action === "hide") hideSecret(id);
    else if (action === "copy") await copySecret(id);
    else if (action === "rename") await renameKey(id);
    else if (action === "test") await testKey(id);
    else if (action === "test-private") await testKey(id, true);
    else if (action === "rotate") await rotateKey(id);
    else if (action === "revoke") await revokeKeyAction(id);
    else if (action === "delete") await deleteKey(id);
  }

  // The create dialog lives outside the repainted content: its fields are not
  // rebuilt by the 30-second refresh, and its idempotency key survives a retry.
  function init(): void {
    createDialog
      .querySelector("[data-cloud-key-create-close]")!
      .addEventListener("click", () => createDialog.close("cancel"));
    createForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void createKey();
    });
    createDialog.addEventListener("close", () => {
      keyUi.createError = null;
      createErrorBox.textContent = "";
    });
    plainDialog
      .querySelector("[data-cloud-plain-close]")!
      .addEventListener("click", () => plainDialog.close("done"));
    plainDialog.addEventListener("close", () => {
      keyUi.plain = null;
      keyUi.secrets.clear();
      plainValue.textContent = "";
      render();
    });
    plainDialog
      .querySelector("[data-cloud-plain-toggle]")!
      .addEventListener("click", () => {
        if (!keyUi.plain) return;
        keyUi.plain.hidden = !keyUi.plain.hidden;
        renderPlain();
      });
    plainDialog
      .querySelector("[data-cloud-plain-copy]")!
      .addEventListener("click", () => {
        const plain = keyUi.plain;
        if (!plain) return;
        void copyText(plain.value).then((ok) => {
          plainNote.textContent = ok
            ? "已复制明文"
            : "复制失败：浏览器拒绝写入剪贴板";
        });
      });
    // D18-5: an inline concurrency edit saves on change, so no 保存 button.
    content.addEventListener("change", (event) => {
      const target = event.target as Element;
      const cap = target.closest<HTMLInputElement>(
        "[data-cloud-key-cap-input]"
      );
      if (cap) {
        void saveCap(cap.dataset.cloudKeyCapInput ?? "");
        return;
      }
    });
    content.addEventListener("input", (event) => {
      const target = event.target as Element;
      const cap = target.closest<HTMLInputElement>(
        "[data-cloud-key-cap-input]"
      );
      if (cap) keyUi.caps.set(cap.dataset.cloudKeyCapInput ?? "", cap.value);
    });
  }

  return { init, openCreate, keyActionFor, reconcileSecrets };
}
