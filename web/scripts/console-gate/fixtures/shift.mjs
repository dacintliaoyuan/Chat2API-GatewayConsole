// Date shifting for the console-gate fixtures.
//
// The console computes its 14-day / 24-hour / 7-day windows from "now", so a frozen
// fixture ages out of every window within days. api.json therefore records the
// calendar day it was generated on as "_anchor" (Asia/Shanghai), and every load
// moves all dates forward by (today - _anchor) whole days. Times of day and UTC
// offsets are kept, so hourly buckets, weekday-independent ordering and the
// relative order of usage rows and price-version boundaries are all preserved.
//
// What is shifted (pure, deterministic):
//   - every calendar date (YYYY-MM-DD, years 2020-2099) inside any string value or
//     object key, including the date part of ISO datetimes ("2026-10-05T09:00:00+08:00")
//     and log timestamps ("2026-10-06 08:48:16,691"). A date glued to a preceding
//     word / "-" / "." is not a date: model ids such as "gpt-5-2025-08-07" and
//     compact file names such as "dsh-ledger-20260902.json" stay as they are, and
//     so do sentinel dates before 2020 ("1970-01-01" = the open-ended price segment);
//   - every number under an epoch-like key (`*_at`, `*_epoch`, `ts`, `*_mtime`,
//     camelCase `*At`) that is an epoch in seconds (1.5e9..2.5e9) or milliseconds
//     (1.5e12..2.5e12). Out-of-range values (e.g. a year-9999 expiry) are left alone.
const DATE_RE = /(?<![\w.-])(20[2-9]\d)-(\d{2})-(\d{2})(?!\d)/g;
const EPOCH_KEY = /(?:^|_)(?:at|epoch|ts|mtime)$|[a-z]At$/;
const DAY_S = 86400;

const pad = (n) => String(n).padStart(2, "0");

/** Shift every date inside a string by `days` calendar days. */
export function shiftDateText(text, days) {
  if (!days) return text;
  return text.replace(DATE_RE, (_, y, m, d) => {
    const t = new Date(Date.UTC(Number(y), Number(m) - 1, Number(d) + days));
    return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`;
  });
}

function shiftEpoch(value, days) {
  if (value >= 1.5e9 && value <= 2.5e9) return value + days * DAY_S;
  if (value >= 1.5e12 && value <= 2.5e12) return value + days * DAY_S * 1000;
  return value;
}

/** Return a deep copy of `body` with every date / epoch moved by `days` days. */
export function shiftFixtureBody(body, days, key = "") {
  if (body === null || !days) return body;
  if (Array.isArray(body))
    return body.map((item) => shiftFixtureBody(item, days, key));
  if (typeof body === "string") return shiftDateText(body, days);
  if (typeof body === "number")
    return EPOCH_KEY.test(key) ? shiftEpoch(body, days) : body;
  if (typeof body === "object") {
    const out = {};
    for (const [k, v] of Object.entries(body))
      out[shiftDateText(k, days)] = shiftFixtureBody(v, days, k);
    return out;
  }
  return body;
}

/** Today's calendar date in Asia/Shanghai (the console's own reporting zone). */
export function shanghaiToday(now = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

/** Whole days from `anchor` (YYYY-MM-DD) to `today` (YYYY-MM-DD). */
export function daysBetween(anchor, today) {
  const at = (s) => {
    const [y, m, d] = s.split("-").map(Number);
    return Date.UTC(y, m - 1, d);
  };
  return Math.round((at(today) - at(anchor)) / (DAY_S * 1000));
}

/** Parsed fixtures ready to serve: shifted to `now` when they carry an "_anchor",
 *  returned unchanged otherwise (a raw local capture is already current). */
export function prepareFixtures(raw, now = new Date()) {
  if (!raw || typeof raw._anchor !== "string") return raw;
  const { _anchor, _note, ...rest } = raw;
  return shiftFixtureBody(rest, daysBetween(_anchor, shanghaiToday(now)));
}
