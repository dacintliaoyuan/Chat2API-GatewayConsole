import { logTsEpoch } from "../../src/workbench-log-presentation.ts";

/** Adapt only explicitly captured targets to the aggregate read contract.
 * Missing captures remain unread, never copies of another source's rows. */
export function logOverviewFixture(fixtures, overrides = {}) {
  const registry = fixtures["/api/console/log-sources"]?.body?.sources ?? [];
  const targets = [],
    rows = [],
    sources = [];
  for (const source of registry) {
    const own = [];
    for (const category of source.categories ?? []) {
      const id = `${source.source}/${category.category}`;
      const key = `/api/console/log-entries?source=${source.source}&category=${category.category}`;
      const body = overrides[id] ?? fixtures[key]?.body;
      const readable = body?.status === "ok" && Array.isArray(body.rows);
      const target = {
        source: source.source,
        category: category.category,
        status: readable ? "ok" : (body?.status ?? "unconfigured"),
        rows: readable ? body.rows.length : null,
        truncated: readable && body.truncated === true,
        rows_dropped: 0,
        file_key: body?.file_key ?? `fixture:${id}`,
      };
      targets.push(target);
      own.push(target);
      if (readable)
        rows.push(
          ...body.rows.map((row) => ({
            ...structuredClone(row),
            source: source.source,
            category: category.category,
            file_key: target.file_key,
            ts_norm: logTsEpoch(row.ts),
          }))
        );
    }
    const read = own.filter((t) => t.status === "ok").length;
    sources.push({
      source: source.source,
      display: source.display,
      status: !own.length
        ? "no_targets"
        : !read
          ? "unavailable"
          : read === own.length
            ? "ok"
            : "partial",
      targets_total: own.length,
      targets_read: read,
    });
  }
  const read = targets.filter((t) => t.status === "ok").length;
  return {
    status: !read ? "unavailable" : read === targets.length ? "ok" : "partial",
    rows,
    targets,
    sources,
    next_cursors: {},
    coverage: {
      full_history: false,
      scope: "bounded_tail_window",
      targets_total: targets.length,
      targets_read: read,
      targets_unread: targets.length - read,
      rows_read: rows.length,
      rows_returned: rows.length,
      rows_dropped_by_budget: 0,
      sources_total: sources.length,
      sources_read: sources.filter((s) => s.targets_read).length,
      sources_without_targets: sources
        .filter((s) => !s.targets_total)
        .map((s) => s.source),
      sources,
      note: "离线夹具：只合并明确捕获的目标，缺席目标保持未读。",
    },
  };
}
