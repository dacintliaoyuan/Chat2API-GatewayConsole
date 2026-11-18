// Display-state to launcher UI mapping.
//
// The launcher page polls /api/console/share-management/display-state every two
// seconds and renders this mapping.  Keeping the mapping pure lets the browser and
// the node test runner share one definition: no DOM, no network, no timers.
//
// ``valid`` means the current auth read is valid, so the login window is no
// longer useful: the screen is hidden and the result card takes its place.
// ``logging_in``, ``waiting_mfa`` and ``waiting_duo`` keep the screen visible
// because the person may need to type or complete MFA / the Duo push.  A ``failed``
// state keeps the screen only when its reason is a terminal manual takeover; a
// generic failure has no live round left to watch.  Every other terminal state
// (timed out, cancelled) and a disconnected relay hides the screen.
const STATES = new Set([
  'not_started',
  'logging_in',
  'waiting_mfa',
  'waiting_duo',
  'valid',
  'timed_out',
  'failed',
  'cancelled',
  'display_disconnected',
  'unknown',
]);

const LABELS = {
  not_started: '登录窗口未开始。',
  logging_in: '正在登录…',
  waiting_mfa: '等待 Duo 登录（本人操作）',
  waiting_duo: '等待本人确认 Duo 推送',
  valid: '登录成功，画面已收起。',
  timed_out: '登录等待已超时。',
  failed: '登录失败。',
  cancelled: '本轮登录已取消。',
  display_disconnected: '显示服务已断开。',
  unknown: '正在读取登录状态…',
};

// A terminal manual takeover is the display-state contract's ``failed`` state
// whose label says a person must complete the step.  The detail carries the same
// reason for older instances (prefixed or bare), so the mapping does not depend on
// one copy alone.
const MANUAL_TAKEOVER_LABEL = '需要人工接管';
const MANUAL_TAKEOVER_PREFIXES = [
  'manual_takeover_required:',
  'manual_takeover:',
];
const MANUAL_TAKEOVER_REASONS = [
  'umpass_mfa_required',
  'umpass_prefill_missing',
  'umpass_autofill_protected',
  'unrecognized_page',
];
const MANUAL_REASON_PATTERN = new RegExp(
  `(?:^|[；;:\\s])(?:${MANUAL_TAKEOVER_REASONS.join('|')})(?:$|[；;\\s])`);

function isManualTakeover(raw) {
  if (raw.label === MANUAL_TAKEOVER_LABEL) return true;
  const detail = typeof raw.detail === 'string' ? raw.detail : '';
  if (MANUAL_TAKEOVER_PREFIXES.some((prefix) => detail.includes(prefix))) {
    return true;
  }
  return MANUAL_REASON_PATTERN.test(detail);
}

// ``failed_manual`` is not a display-state value; it is the screen marker for
// ``failed`` + a manual reason.  The rendered state stays ``failed``.
const SCREEN_STATES = new Set([
  'logging_in', 'waiting_mfa', 'waiting_duo', 'failed_manual',
]);

function screenState(state, raw) {
  if (state === 'failed' && isManualTakeover(raw)) return 'failed_manual';
  return state;
}

/**
 * Map one display-state payload to the launcher view state.
 *
 * ``actions`` is the launcher's available controls.  ``reconnect`` and
 * ``back_to_console`` are always available; ``cancel_login`` only while a login
 * can still be cancelled, and ``takeover`` only when another page holds the
 * operator and this page does not.
 */
export function displayLauncherState(payload) {
  const raw = payload && typeof payload === 'object' ? payload : {};
  const state = STATES.has(raw.state) ? raw.state : 'unknown';
  const operator = raw.operator && typeof raw.operator === 'object'
    ? raw.operator : {};
  const actions = ['reconnect'];
  if (state === 'logging_in' || state === 'waiting_mfa'
      || state === 'waiting_duo') {
    actions.push('cancel_login');
  }
  if (operator.owner_present === true && operator.owned_by_me !== true) {
    actions.push('takeover');
  }
  actions.push('back_to_console');
  const manual = state === 'failed' && isManualTakeover(raw);
  return {
    state,
    label: manual ? MANUAL_TAKEOVER_LABEL : LABELS[state],
    actions,
    showScreen: SCREEN_STATES.has(screenState(state, raw)),
  };
}
