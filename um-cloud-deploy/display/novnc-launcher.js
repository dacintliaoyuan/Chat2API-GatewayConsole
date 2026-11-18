// Same-origin noVNC launcher for the private cloud login window.
//
// Served at /display/launcher.js next to novnc-launcher.html.  The page CSP is
// script-src 'self', so this module must stay an external same-origin file: an
// inline script is blocked by the browser and the page never connects.
//
// The noVNC core tree is served under /display/, so the relative import below
// resolves to /display/core/rfb.js on the console origin.  VNC authentication is
// completed by the console proxy against x11vnc; the browser never receives the
// VNC password and never prompts for it.
//
// Two fragments are supported, never a query string, so neither is sent to the
// server or written to an access log:
//
// * #console=<code> proves management identity with the SSH desktop entry's
//   single-use code and continues to the console overview;
// * #grant=<token> exchanges a display grant for an HttpOnly, SameSite=Strict
//   display session cookie.
//
// Watching is not the same as operating.  The page asks the console for the
// current operator generation before opening the WebSocket, and the proxy drops
// keyboard/pointer events from any older generation.  Closing the page or returning
// to the console never cancels the background login; only the explicit cancel
// button does.
import RFB from './core/rfb.js';

// Display-state to launcher UI mapping. Kept inline in this classic script
// because the shipped SSH bootstrap harness provides only the RFB boundary; the
// node test loads novnc-launcher-state.mjs, which mirrors this pure mapping.
const LAUNCHER_STATES = new Set([
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
const LAUNCHER_LABELS = {
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
// ``failed_manual`` marks ``failed`` + a terminal manual reason; the rendered
// state stays ``failed``.
const LAUNCHER_MANUAL_LABEL = '需要人工接管';
const LAUNCHER_MANUAL_PREFIXES = [
  'manual_takeover_required:',
  'manual_takeover:',
];
const LAUNCHER_MANUAL_REASONS = [
  'umpass_mfa_required',
  'umpass_prefill_missing',
  'umpass_autofill_protected',
  'unrecognized_page',
];
const LAUNCHER_MANUAL_PATTERN = new RegExp(
  `(?:^|[；;:\\s])(?:${LAUNCHER_MANUAL_REASONS.join('|')})(?:$|[；;\\s])`);
const LAUNCHER_SCREEN_STATES = new Set([
  'logging_in', 'waiting_mfa', 'waiting_duo', 'failed_manual',
]);

function isLauncherManualTakeover(raw) {
  if (raw.label === LAUNCHER_MANUAL_LABEL) return true;
  const detail = typeof raw.detail === 'string' ? raw.detail : '';
  if (LAUNCHER_MANUAL_PREFIXES.some((prefix) => detail.includes(prefix))) {
    return true;
  }
  return LAUNCHER_MANUAL_PATTERN.test(detail);
}

function launcherScreenState(state, raw) {
  if (state === 'failed' && isLauncherManualTakeover(raw)) {
    return 'failed_manual';
  }
  return state;
}

function displayLauncherState(payload) {
  const raw = payload && typeof payload === 'object' ? payload : {};
  const state = LAUNCHER_STATES.has(raw.state) ? raw.state : 'unknown';
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
  const manual = state === 'failed' && isLauncherManualTakeover(raw);
  return {
    state,
    label: manual ? LAUNCHER_MANUAL_LABEL : LAUNCHER_LABELS[state],
    actions,
    showScreen: LAUNCHER_SCREEN_STATES.has(launcherScreenState(state, raw)),
  };
}

const DISPLAY_STATE_URL = '/api/console/share-management/display-state';
const OPERATOR_URL = '/api/console/share-management/display-operator';
const CANCEL_LOGIN_URL =
  '/api/console/share-management/backend-login-cancel';
const WEBSOCKET_PATH = '/display/websockify?operator=';

const status = document.getElementById('status');
const screen = document.getElementById('screen');
const result = document.getElementById('result');
const resultText = document.getElementById('result-text');
const actions = document.getElementById('actions');
const buttons = {
  reconnect: document.getElementById('action-reconnect'),
  cancel: document.getElementById('action-cancel'),
  takeover: document.getElementById('action-takeover'),
  back: document.getElementById('action-back'),
};

let viewer = null;
let shouldConnect = false;
let operatorPending = false;
let polling = false;
let current = displayLauncherState(null);

function setStatus(state, text) {
  status.dataset.state = state;
  status.textContent = text;
}

function grantFromFragment() {
  const match = /^#grant=([A-Za-z0-9_-]{16,256})$/.exec(location.hash);
  return match ? match[1] : null;
}

async function exchangeGrant(grant) {
  const response = await fetch('/display/grant', {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
    },
    body: JSON.stringify({ grant }),
  });
  return response.ok;
}

async function displaySessionAuthorized() {
  try {
    const response = await fetch('/display/session', {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    if (!response.ok) return false;
    const body = await response.json();
    return Boolean(body && body.authorized === true);
  } catch {
    return false;
  }
}

function render(next) {
  status.dataset.state = next.state;
  status.textContent = next.label;
  if (next.showScreen && next.actions.includes('takeover')) {
    // Another page owns the operator while a login is live: say so instead
    // of a generic login label.
    status.dataset.state = 'conflict';
    status.textContent = '已有操作页，可显式接管。';
  }
  screen.hidden = !next.showScreen;
  result.hidden = next.showScreen;
  resultText.textContent = next.label;
  actions.hidden = next.actions.length === 0;
  buttons.reconnect.hidden = !next.actions.includes('reconnect');
  buttons.cancel.hidden = !next.actions.includes('cancel_login');
  buttons.takeover.hidden = !next.actions.includes('takeover');
  buttons.back.hidden = !next.actions.includes('back_to_console');
}

function disconnectViewer() {
  if (viewer) {
    try {
      viewer.disconnect();
    } catch {
      // A viewer that already closed must not break the state transition.
    }
    viewer = null;
  }
}

function connect(generation) {
  disconnectViewer();
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://')
    + location.host + WEBSOCKET_PATH + encodeURIComponent(String(generation));
  const client = new RFB(screen, url, {
    wsProtocols: ['binary'],
  });
  viewer = client;

  client.scaleViewport = true;
  // x11vnc refuses SetDesktopSize (resize prohibited); asking noVNC to resize
  // leaves its canvas at the requested size while the server keeps 1280x900, which
  // can render a black page.  Keep the server geometry and scale it instead.
  client.resizeSession = false;

  client.addEventListener('connect', () => {
    setStatus('connected', '显示已连接，VNC 认证由云端完成。');
  });
  client.addEventListener('disconnect', (event) => {
    if (viewer !== client) return;
    viewer = null;
    // A disconnect only ends this view.  The background login keeps running and
    // the person can reconnect with the button; no cancel is sent here.
    shouldConnect = false;
    setStatus('error', event.detail.clean
      ? '连接已断开。可重新连接，后台登录不会因此取消。'
      : '连接异常断开。请确认云机显示服务仍在运行。');
  });
  client.addEventListener('credentialsrequired', () => {
    setStatus('error', '显示服务要求受控认证，已停止连接。');
    client.disconnect();
  });
}

function applyState(next) {
  current = next;
  render(next);
  if (next.state === 'valid' || next.state === 'display_disconnected'
      || next.state === 'timed_out'
      || (next.state === 'failed' && !next.showScreen)
      || next.state === 'cancelled') {
    // Success收屏: the verified login result replaces the canvas.  A terminal
    // state or a dead relay has no screen left to watch.  A manual takeover
    // failure keeps the screen because the person still has to complete it.
    shouldConnect = false;
    disconnectViewer();
    return;
  }
  if (next.showScreen && shouldConnect && viewer === null
      && !operatorPending) {
    void acquireOperator('acquire');
  }
}

async function pollDisplayState() {
  if (polling) return;
  polling = true;
  try {
    const response = await fetch(DISPLAY_STATE_URL, {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    if (!response.ok) return;
    const payload = await response.json();
    applyState(displayLauncherState(payload));
  } catch {
    // A read failure keeps the last known state; the next poll retries.
  } finally {
    polling = false;
  }
}

async function acquireOperator(action) {
  if (operatorPending) return;
  operatorPending = true;
  try {
    const body = JSON.stringify(action === 'takeover'
      ? { action: 'takeover' }
      : { action: 'acquire' });
    const response = await fetch(OPERATOR_URL, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body,
    });
    if (response.ok) {
      const payload = await response.json();
      const generation = payload ? payload.generation : null;
      if (Number.isInteger(generation) && generation > 0) {
        connect(generation);
      }
      return;
    }
    if (response.status === 409) {
      const payload = await response.json().catch(() => null);
      const error = payload ? payload.error : null;
      if (error === 'display_operator_exists') {
        // Another page owns the operator.  Never reuse its generation; the
        // takeover button asks the server for a new one.
        shouldConnect = false;
        setStatus('conflict', '已有操作页，可显式接管。');
        buttons.takeover.hidden = false;
        return;
      }
      if (error === 'display_session_required') {
        shouldConnect = false;
        setStatus('error', '需要受控授权：请通过云端管理入口打开登录窗口。');
        return;
      }
    }
    setStatus('error', '无法取得显示操作权，请稍后重试。');
  } catch {
    setStatus('error', '无法取得显示操作权，请稍后重试。');
  } finally {
    operatorPending = false;
  }
}

async function cancelLogin() {
  buttons.cancel.disabled = true;
  try {
    const response = await fetch(CANCEL_LOGIN_URL, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
    if (response.ok) {
      shouldConnect = false;
      disconnectViewer();
      setStatus('cancelled', '本轮登录已取消。');
      return;
    }
  } catch {
    // The explicit cancel button stays available for another attempt.
  }
  buttons.cancel.disabled = false;
  setStatus('error', '取消登录失败，请稍后重试。');
}

function reconnectDisplay() {
  shouldConnect = true;
  if (!current.showScreen) {
    // Nothing is on screen; refresh the state and connect when a login starts.
    void pollDisplayState();
    return;
  }
  disconnectViewer();
  void acquireOperator('acquire');
}

function takeoverDisplay() {
  shouldConnect = true;
  disconnectViewer();
  void acquireOperator('takeover');
}

function backToConsole() {
  // Leaving the viewer only ends this view; it never cancels the login.
  location.assign('/#overview');
}

const buttonListeners = {
  reconnect: reconnectDisplay,
  cancel: () => { void cancelLogin(); },
  takeover: takeoverDisplay,
  back: backToConsole,
};
for (const [name, listener] of Object.entries(buttonListeners)) {
  const button = buttons[name];
  if (button && typeof button.addEventListener === 'function') {
    button.addEventListener('click', listener);
  }
}

async function start() {
  // The SSH desktop entry proves management identity with a single-use code.
  // Clear it before network I/O; the long-lived control token stays on the ECS.
  if (location.hash.startsWith('#console=')) {
    const match = /^#console=([A-Za-z0-9_-]{16,256})$/.exec(location.hash);
    history.replaceState(null, '', location.pathname + location.search);
    if (!match) {
      setStatus('error', '管理授权格式无效，请重新打开桌面入口。');
      return;
    }
    setStatus('connecting', '正在建立云端管理会话…');
    try {
      const response = await fetch('/api/session', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify({ code: match[1] }),
      });
      if (response.ok) {
        location.replace('/#overview');
        return;
      }
    } catch {
      // Transport exceptions must never echo the bootstrap code or a URL.
    }
    setStatus('error', '管理授权失败或已过期，请重新打开桌面入口。');
    return;
  }
  const grant = grantFromFragment();
  if (grant) {
    history.replaceState(null, '', location.pathname + location.search);
    if (!await exchangeGrant(grant)) {
      setStatus('error', '受控授权无效或已过期，请重新获取登录窗口。');
      return;
    }
  }
  if (!await displaySessionAuthorized()) {
    setStatus('error', '需要受控授权：请通过云端管理入口打开登录窗口。');
    return;
  }
  render(current);
  shouldConnect = true;
  void pollDisplayState();
  if (typeof setInterval === 'function') {
    setInterval(pollDisplayState, 2000);
  }
}

start();
