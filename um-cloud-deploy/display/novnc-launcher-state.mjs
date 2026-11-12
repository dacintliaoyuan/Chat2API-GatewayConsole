// Display-state to launcher UI mapping.
//
// The launcher page polls /api/console/share-management/display-state every two
// seconds and renders this mapping.  Keeping the mapping pure lets the browser and
// the node test runner share one definition: no DOM, no network, no timers.
//
// ``valid`` means the current auth read is valid, so the login window is no
// longer useful: the screen is hidden and the result card takes its place.
// ``logging_in`` and ``waiting_mfa`` keep the screen visible because the person
// may need to type or complete MFA.  Every terminal state (timed out, failed,
// cancelled) and a disconnected relay hides the screen and reports its own copy.
const STATES = new Set([
  'not_started',
  'logging_in',
  'waiting_mfa',
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
  waiting_mfa: '等待 MFA（本人操作）',
  valid: '登录成功，画面已收起。',
  timed_out: '登录等待已超时。',
  failed: '登录失败。',
  cancelled: '本轮登录已取消。',
  display_disconnected: '显示服务已断开。',
  unknown: '正在读取登录状态…',
};

const SCREEN_STATES = new Set(['logging_in', 'waiting_mfa']);

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
  if (state === 'logging_in' || state === 'waiting_mfa') {
    actions.push('cancel_login');
  }
  if (operator.owner_present === true && operator.owned_by_me !== true) {
    actions.push('takeover');
  }
  actions.push('back_to_console');
  return {
    state,
    label: LABELS[state],
    actions,
    showScreen: SCREEN_STATES.has(state),
  };
}
