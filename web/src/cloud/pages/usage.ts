import { escapeHtml } from "../../html";
import { fmtNum, fmtTokens } from "../../console/ui";
import { FAILURE_WORD, lastValue } from "../cloud-reads";
import {
  aggregateRows,
  clock,
  dailyTotals,
  failedLine,
  lastDays,
  ledgerRead,
  todayUsage,
  undatedRequests,
  windowSum,
  zeroAggregate,
  type SummaryRow,
} from "../cloud-format";
import type { CloudModel } from "../model";

export function usageKpis(model: CloudModel): string {
  const usageRead = lastValue(model.usage);
  // A stale read is not a current value: today's figures stay unknown until a
  // fresh read answers, and the note names the stale reading instead of
  // rendering the old value as today's usage.
  const usage = usageRead && !usageRead.stale ? usageRead : null;
  const today = usage ? todayUsage(usage.value) : null;
  const ledger = usage ? ledgerRead(usage.value) : null;
  const totals = usage ? dailyTotals(usage.value) : null;
  // A readable ledger that reports 0 requests with no day rows is a measured
  // empty week, not an unknown one; a null request count stays unknown.
  const week =
    usage && !ledger?.unread
      ? totals?.hasDaily ||
        (usage.value.requests === 0 &&
          !totals?.missingKeys &&
          !totals?.missingUnattributed)
        ? lastDays(7).reduce(
            (sum, day) => sum + (totals?.days.get(day)?.requests ?? 0),
            0
          )
        : null
      : null;
  // An unread ledger answers requests 0 without day rows; that is unknown, not
  // a measured zero, so every usage figure falls back to 未知 with the reason.
  // A stale read is the same: 上次读数 is not today's value.
  const measuredToday =
    usage && !usage.stale && !ledger?.unread && today?.available
      ? today
      : null;
  const usageNote = !usageRead
    ? model.usage.state === "loading"
      ? "用量读取中"
      : model.usage.state === "failed"
        ? `用量${FAILURE_WORD[model.usage.kind]}：${model.usage.reason}；今日用量未知，不用 0 代替`
        : "等待用量读数"
    : usageRead.stale
      ? `用量读数已过时（上次 ${clock(usageRead.at)}）：今日用量未知，不用 0 代替`
      : ledger?.unread
        ? `${ledger.reason}；今日用量未知，不用 0 代替`
        : !today?.available
          ? "后端没有按日明细，今日用量未知，不用 0 代替"
          : "";
  // Ticket #8: today's failures come from the backend's own `result=failed`
  // aggregate read (window=24h). A missing, failed or stale read is 未知,
  // never a fabricated 0.
  const failedRead = lastValue(model.usageFailed);
  let failed: number | null = null;
  let failedNote = "";
  if (!failedRead) {
    failedNote =
      model.usageFailed?.state === "failed"
        ? `失败读数${FAILURE_WORD[model.usageFailed.kind]}：${model.usageFailed.reason}`
        : model.usageFailed?.state === "loading"
          ? "失败读数读取中"
          : "等待失败读数";
  } else if (failedRead.stale) {
    failedNote = `失败读数已过时（上次 ${clock(failedRead.at)}）`;
  } else {
    const failedLedger = ledgerRead(failedRead.value);
    const failedToday = todayUsage(failedRead.value);
    if (failedLedger.unread)
      failedNote = `${failedLedger.reason}；今日失败未知，不用 0 代替`;
    else if (!failedToday.available)
      failedNote = "后端没有按日明细，今日失败未知，不用 0 代替";
    else failed = failedToday.requests;
  }
  const requestValue = measuredToday
    ? measuredToday.requests === null
      ? "未知"
      : fmtNum(measuredToday.requests)
    : "未知";
  const tokenValue = measuredToday
    ? measuredToday.tokens === null
      ? "未知"
      : `${measuredToday.tokensComplete ? "" : "≥"}${fmtTokens(measuredToday.tokens)}`
    : "未知";
  const failedValue = failed === null ? "未知" : fmtNum(failed);
  const weekNote = week === null ? "近 7 天未知" : `近 7 天 ${fmtNum(week)}`;
  const tokenNote =
    measuredToday && !measuredToday.tokensComplete
      ? "今日 Token 含未完整记录，≥ 表示下界"
      : "";
  return `${failedLine(model.usage)}
  <section class="cloud-usage-figures" aria-label="今日用量" data-cloud-usage-figures>
    <div class="cloud-usage-figure" data-cloud-usage-figure="requests">
      <span class="cloud-usage-figure-label">今日请求</span>
      <span class="cloud-usage-figure-value mono" data-cloud-usage-value="requests">${requestValue}</span>
    </div>
    <div class="cloud-usage-figure" data-cloud-usage-figure="tokens">
      <span class="cloud-usage-figure-label">今日 Token</span>
      <span class="cloud-usage-figure-value mono" data-cloud-usage-value="tokens">${tokenValue}</span>
    </div>
    <div class="cloud-usage-figure" data-cloud-usage-figure="failed">
      <span class="cloud-usage-figure-label">今日失败</span>
      <span class="cloud-usage-figure-value mono" data-cloud-usage-value="failed">${failedValue}</span>
    </div>
    <p class="cloud-usage-figure-note">${weekNote} · 北京时间日期${tokenNote ? ` · ${tokenNote}` : ""}${totals?.missingKeys ? ` · ${totals.missingKeys} 个 Key 没有按日明细，合计不含它们` : ""}${totals?.missingUnattributed ? " · 未归属桶没有按日明细，合计不含它" : ""}</p>
    ${usageNote ? `<p class="cloud-usage-figure-note" data-tone="warn">${escapeHtml(usageNote)}</p>` : ""}
    ${usageNote ? `<p class="cloud-usage-figure-note" data-tone="warn">${escapeHtml(usageNote)}</p>` : ""}
    ${failedNote ? `<p class="cloud-usage-figure-note" data-tone="warn">${escapeHtml(failedNote)}</p>` : ""}
  </section>`;
}

type RankRow = {
  id: string;
  label: string;
  sub: string;
  value: number | null;
  missing: boolean;
  drill: string;
};

function rankRows(model: CloudModel): {
  rows: RankRow[];
  note: string;
  scope: string;
} {
  const usage = lastValue(model.usage)?.value;
  if (!usage) return { rows: [], note: "", scope: "" };
  const ui = model.overviewUi;
  if (ui.rank === "model") {
    const buckets = new Map<string, SummaryRow[]>();
    let missingKeys = 0;
    for (const key of usage.keys) {
      if (
        key.models.length === 0 &&
        (key.coverage.requests === null || key.coverage.requests > 0)
      )
        missingKeys += 1;
      for (const row of key.models) {
        const bucket = buckets.get(row.identity) ?? [];
        bucket.push(row);
        buckets.set(row.identity, bucket);
      }
    }
    for (const row of usage.unattributed.models) {
      const bucket = buckets.get(row.identity) ?? [];
      bucket.push(row);
      buckets.set(row.identity, bucket);
    }
    const rows = [...buckets]
      .map(([identity, details]) => {
        const aggregate = aggregateRows(details);
        return {
          id: identity,
          label: identity,
          sub: "全部时段",
          value: aggregate[ui.metric],
          missing: false,
          drill: `model:${identity}`,
        };
      })
      .sort((a, b) => (b.value ?? -1) - (a.value ?? -1))
      .slice(0, 6);
    const missingUnattributed =
      usage.unattributed.models.length === 0 &&
      (usage.unattributed.coverage.requests === null ||
        usage.unattributed.coverage.requests > 0);
    return {
      rows,
      note: `${missingKeys ? `${missingKeys} 个 Key 没有按模型明细，排行不含它们。` : ""}${missingUnattributed ? "未归属桶没有按模型明细，排行不含它。" : ""}模型明细只有全部时段，后端未提供模型×日期；不能与左侧 14 天图同窗。`,
      scope: "全部时段",
    };
  }
  const days = Number(ui.range);
  const window = new Set(lastDays(days));
  const totals = dailyTotals(usage);
  const keyRows = lastValue(model.keys)?.value ?? [];
  const rows: RankRow[] = [];
  for (const key of usage.keys) {
    const missing =
      key.days.length === 0 &&
      (key.coverage.requests === null || key.coverage.requests > 0);
    const aggregate = missing ? null : windowSum(key.days, window);
    rows.push({
      id: key.keyId,
      label: key.keyName ?? key.keyId,
      sub: keyRows.find((row) => row.id === key.keyId)?.masked || key.keyId,
      value: aggregate ? aggregate[ui.metric] : null,
      missing,
      drill: `key:${key.keyId}`,
    });
  }
  const unattributed = usage.unattributed;
  const missing =
    unattributed.days.length === 0 &&
    (unattributed.coverage.requests === null ||
      unattributed.coverage.requests > 0);
  const aggregate = missing ? null : windowSum(unattributed.days, window);
  rows.push({
    id: "unattributed",
    label: "未归属（无法对应到 Key）",
    sub: "单列，不摊到任何 Key",
    value: aggregate ? aggregate[ui.metric] : null,
    missing,
    drill: "key:unattributed",
  });
  rows.sort((a, b) => (b.value ?? -1) - (a.value ?? -1));
  return {
    rows: rows.slice(0, 6),
    note: `${totals.missingKeys ? `${totals.missingKeys} 个 Key 没有按日明细，排行不含它们。` : ""}Key 排行与左侧图表同为近 ${days} 天、同一指标。`,
    scope: `近 ${days} 天`,
  };
}

export function rankCard(model: CloudModel): string {
  const usage = lastValue(model.usage);
  const ui = model.overviewUi;
  const rank = rankRows(model);
  const metricWord = ui.metric === "requests" ? "请求" : "Token";
  const value = (row: RankRow) => {
    if (row.missing) return "按日明细未提供";
    const metric = row.value;
    if (metric === null) return "未知";
    return ui.metric === "requests" ? fmtNum(metric) : fmtTokens(metric);
  };
  const head = `<div class="card-head"><h3>${ui.rank === "key" ? "Key 排行" : "模型排行"}</h3><span class="card-meta">${rank.scope} · ${metricWord}</span><span class="card-grow"></span>
    <div class="cloud-seg" role="group" aria-label="排行维度">
      <button type="button" data-cloud-rank="key" aria-pressed="${ui.rank === "key"}">按 Key</button>
      <button type="button" data-cloud-rank="model" aria-pressed="${ui.rank === "model"}">按模型</button>
    </div></div>`;
  if (!usage)
    return `<section class="card cloud-rank" aria-label="排行">${head}${failedLine(model.usage)}</section>`;
  // An absent/unreadable ledger answers requests 0 with no rows; that is not a
  // measured empty ledger, so the card says so instead of "还没有用量记录".
  const ledger = ledgerRead(usage.value);
  if (ledger.unread) {
    return `<section class="card cloud-rank" aria-label="排行">${head}${failedLine(model.usage)}
      <p class="card-foot">账本未读取/不可读（${escapeHtml(ledger.reason)}）：累计请求与排行保持未知，不当作空账本，也不补 0。</p>
    </section>`;
  }
  // Only a measured 0 on every signal is "no usage yet"; a null request
  // count is unknown and is never read as an empty ledger.
  const emptyUsage =
    usage.value.keys.length === 0 &&
    usage.value.unattributed.days.length === 0 &&
    usage.value.unattributed.coverage.requests === 0 &&
    usage.value.requests === 0;
  const unknownUsage =
    usage.value.keys.length === 0 &&
    usage.value.unattributed.days.length === 0 &&
    (usage.value.unattributed.coverage.requests === null ||
      usage.value.requests === null);
  return `<section class="card cloud-rank" aria-label="排行">${head}
    ${emptyUsage ? `<p class="card-foot">还没有用量记录。</p>` : ""}
    ${unknownUsage ? `<p class="card-foot">后端没有返回累计请求数，无法判断是否没有用量；不补 0。</p>` : ""}
    ${
      !emptyUsage && rank.rows.length
        ? `<ol class="cloud-rank-list">${rank.rows
            .map(
              (row, index) => `<li>
      <button type="button" class="cloud-rank-row" data-cloud-drill="${escapeHtml(row.drill)}">
        <span class="cloud-rank-index" aria-hidden="true">${index + 1}</span>
        <span class="cloud-rank-text"><strong>${escapeHtml(row.label)}</strong><small>${escapeHtml(row.sub)}</small></span>
        <span class="cloud-rank-value num">${escapeHtml(value(row))}</span>
      </button></li>`
            )
            .join("")}</ol>`
        : emptyUsage || unknownUsage
          ? ""
          : `<p class="card-foot">后端未提供${ui.rank === "key" ? "按日" : "按模型"}排行明细。</p>`
    }
    <p class="card-foot">${escapeHtml(rank.note)}</p>
  </section>`;
}

/** D18-13: daily usage over the selected 7/14/30 Beijing days. One series,
 *  bars anchored at the baseline, a hover / focus tooltip per day and a table for
 *  assistive tech. A day whose Token total is unknown has no bar and says 未知;
 *  it is never painted as a zero. An all-zero window says so instead of drawing
 *  a silent flat line, and a partial day is marked as a lower bound. */
export function trendCard(model: CloudModel): string {
  const usage = lastValue(model.usage);
  const ui = model.overviewUi;
  const days = Number(ui.range);
  const metricWord = ui.metric === "requests" ? "请求" : "Token";
  const head = `<div class="card-head"><h3>近 ${days} 天${metricWord}</h3><span class="card-meta">每日 · 全部 Key 与未归属</span><span class="card-grow"></span>
    <div class="cloud-seg" role="group" aria-label="图表时段">
      ${(["7", "14", "30"] as const).map((range) => `<button type="button" data-cloud-range="${range}" aria-pressed="${ui.range === range}">近 ${range} 天</button>`).join("")}
    </div>
    <div class="cloud-seg" role="group" aria-label="趋势指标">
      <button type="button" data-cloud-metric="requests" aria-pressed="${ui.metric === "requests"}">请求</button>
      <button type="button" data-cloud-metric="tokens" aria-pressed="${ui.metric === "tokens"}">Token</button>
    </div></div>`;
  if (!usage)
    return `<section class="card cloud-trend" aria-label="近 ${days} 天用量">${head}${failedLine(model.usage)}${model.usage.state === "loading" ? `<p class="card-foot">读取中…</p>` : ""}</section>`;
  // An absent/unreadable ledger answers requests 0 with no day rows; painting
  // those days as a zero curve would claim a measured empty ledger.
  const ledger = ledgerRead(usage.value);
  if (ledger.unread) {
    return `<section class="card cloud-trend" aria-label="近 ${days} 天用量">${head}${failedLine(model.usage)}
      <p class="cloud-chart-empty">账本未读取/不可读（${escapeHtml(ledger.reason)}）：无法绘制每日趋势，不画全 0 曲线，也不当作空账本。</p>
      <p class="card-foot">请求、Token 与每日桶都保持未知，不补 0。</p>
    </section>`;
  }
  const dayList = lastDays(days);
  const totals = dailyTotals(usage.value);
  if (!totals.hasDaily && (usage.value.requests ?? 0) > 0) {
    return `<section class="card cloud-trend" aria-label="近 ${days} 天用量">${head}
      <p class="cloud-chart-empty">后端未提供按日明细，无法绘制每日趋势；不用 0 补一条假曲线。</p>
      <p class="card-foot">已读取到累计 ${fmtNum(usage.value.requests ?? 0)} 次请求，但没有日期维度。</p>
    </section>`;
  }
  const values = dayList.map(
    (day) =>
      totals.days.get(day) ??
      (totals.missingKeys === 0 && !totals.missingUnattributed
        ? zeroAggregate()
        : null)
  );
  const metricValues = values.map((value) => (value ? value[ui.metric] : null));
  const known = metricValues.filter((value): value is number => value !== null);
  if (!known.length) {
    return `<section class="card cloud-trend" aria-label="近 ${days} 天用量">${head}
      <p class="cloud-chart-empty">后端未提供${metricWord}明细，无法绘制每日趋势。</p>
      <p class="card-foot">${totals.missingKeys ? `${totals.missingKeys} 个 Key 没有按日明细；` : ""}不用 0 补一条假曲线。</p>
    </section>`;
  }
  const max = Math.max(...known);
  // An all-zero window is a measured zero, not a broken chart: say so instead
  // of leaving a silent empty baseline.
  const allZero = max <= 0;
  // 「每天的…都是 0」只在每一天都有读数时成立：有 Key 缺按日明细时，
  // 缺失日会标「无数据」，脚注不能宣称每天都是 0。
  const everyDayZero = allZero && known.length === metricValues.length;
  const top = allZero ? 4 : niceCeil(max);
  const W = ui.chartWidth > 0 ? ui.chartWidth : 320,
    H = 150,
    left = 34,
    right = 6,
    plotTop = 10,
    base = 124;
  const step = (W - left - right) / dayList.length;
  const barW = Math.max(2, step - 2);
  const y = (value: number) => base - (value / top) * (base - plotTop);
  const grid = [0, top / 2, top]
    .map(
      (value) =>
        `<line x1="${left}" x2="${W - right}" y1="${y(value).toFixed(1)}" y2="${y(value).toFixed(1)}" class="cloud-grid-line"/>` +
        `<text x="${left - 6}" y="${(y(value) + 4).toFixed(1)}" text-anchor="end" class="cloud-axis">${ui.metric === "requests" ? fmtNum(value) : fmtTokens(value)}</text>`
    )
    .join("");
  const bars = values
    .map((value, index) => {
      const x = left + index * step + (step - barW) / 2;
      const metric = value ? value[ui.metric] : null;
      const h = metric === null ? 0 : base - y(metric);
      const r = Math.min(4, barW / 2, h);
      const total =
        metric === null
          ? "未知"
          : `${fmtNum(metric)} ${ui.metric === "requests" ? "次请求" : "Token"}`;
      const label =
        `${dayList[index]}${index === dayList.length - 1 ? "（今天）" : ""}：` +
        `${value && value.requests !== null ? `${fmtNum(value.requests)} 次请求` : "请求未知"}，` +
        `Token ${value && value.tokens !== null ? `${value.tokensComplete ? "" : "≥"}${fmtTokens(value.tokens)}` : "未知"}` +
        `；当天合计 ${total}` +
        (metric === null ? `；${metricWord}未提供` : "");
      const mark =
        h > 0
          ? `<path class="cloud-bar" d="M${x.toFixed(1)} ${base}V${(base - h + r).toFixed(1)}q0 -${r.toFixed(1)} ${r.toFixed(1)} -${r.toFixed(1)}h${(barW - 2 * r).toFixed(1)}q${r.toFixed(1)} 0 ${r.toFixed(1)} ${r.toFixed(1)}V${base}z"/>`
          : "";
      // D18-13: a day with no bucket gets a short missing mark at the baseline
      // instead of silently looking like a measured 0.
      const missingMark =
        value === null
          ? `<text x="${(left + index * step + step / 2).toFixed(1)}" y="${base + 12}" text-anchor="middle" class="cloud-axis cloud-axis-missing">无数据</text>`
          : "";
      return `<g class="cloud-bar-slot" tabindex="0" role="img" aria-label="${escapeHtml(label)}" data-tip="${escapeHtml(label)}"><rect class="cloud-bar-hit" x="${(left + index * step).toFixed(1)}" y="${plotTop}" width="${step.toFixed(1)}" height="${base - plotTop}"/>${mark}${missingMark}</g>`;
    })
    .join("");
  const tickEvery = dayList.length > 14 ? 5 : dayList.length > 7 ? 2 : 1;
  const ticks = dayList
    .map((day, index) =>
      index % tickEvery === 0 || index === dayList.length - 1
        ? `<text x="${(left + index * step + step / 2).toFixed(1)}" y="${base + 18}" text-anchor="middle" class="cloud-axis">${index === dayList.length - 1 ? "今天" : day.slice(5).replace("-", "/")}</text>`
        : ""
    )
    .join("");
  const undated = undatedRequests(usage.value);
  const undatedText = !undated.reported
    ? ""
    : undated.count === null
      ? "另有请求后端无法确定日期；数量未上报。"
      : undated.count > 0
        ? `另有 ${fmtNum(undated.count)} 次请求后端无法确定日期，计入累计但不在图中。`
        : "";
  const lowerBound =
    values.some((value) => value !== null && value.tokensComplete === false)
      ? `${metricWord}含未完整记录，数值是下界（≥）。`
      : "";
  const allZeroNote = everyDayZero
    ? `所选时段内每天的${metricWord}都是 0：这是已读取到的 0，不是读取失败。`
    : "";
  const table = `<table class="sr-only"><caption>近 ${days} 天每日${metricWord}</caption><thead><tr><th>日期</th><th>请求</th><th>Token</th></tr></thead><tbody>${dayList
    .map((day, index) => {
      const value = values[index];
      return `<tr><td>${day}</td><td>${value ? (value.requests === null ? "未知" : value.requests) : "未知"}</td><td>${value ? (value.tokens === null ? "未知" : value.tokens) : "未知"}</td></tr>`;
    })
    .join("")}</tbody></table>`;
  return `<section class="card cloud-trend" aria-label="近 ${days} 天${metricWord}">${head}${failedLine(model.usage)}
    <svg class="cloud-chart" viewBox="0 0 ${W} ${H}" aria-hidden="false" role="group" aria-label="近 ${days} 天每日${metricWord}柱状图">${grid}${bars}${ticks}</svg>${table}
    <p class="card-foot">按北京时间日期汇总；${totals.missingKeys || totals.missingUnattributed ? `${totals.missingKeys ? `有 ${totals.missingKeys} 个 Key ` : ""}${totals.missingUnattributed ? "未归属桶 " : ""}没有按日明细，图中不补 0。` : "有日期桶但没有记录的日子为 0。"}${undatedText}${lowerBound}${allZeroNote}${usage.stale ? ` 读数时间 ${clock(usage.at)}（已过时）。` : ""}</p>
  </section>`;
}

function niceCeil(value: number): number {
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const n = value / magnitude;
  const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return Math.max(2, nice * magnitude);
}
