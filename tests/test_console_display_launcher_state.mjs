// Display-state mapping for the private noVNC launcher page.
//
// The launcher renders one state segment at a time.  This module is pure so the
// mapping can be checked without a browser or a live 18096 console.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { displayLauncherState } from '../um-cloud-deploy/display/novnc-launcher-state.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const DISPLAY_DIR = join(HERE, '..', 'um-cloud-deploy', 'display');
const LAUNCHER_JS = readFileSync(
  join(DISPLAY_DIR, 'novnc-launcher.js'), 'utf8');
const LAUNCHER_HTML = readFileSync(
  join(DISPLAY_DIR, 'novnc-launcher.html'), 'utf8');

function payload(state, operator = {}) {
  return {
    available: true,
    state,
    label: '',
    detail: null,
    observed_at: 1700000000,
    login_window_deadline_epoch: null,
    operator: {
      owned_by_me: false,
      owner_present: false,
      generation: 0,
      stale: false,
      ...operator,
    },
    display: { available: true, reason: null },
  };
}

test('valid hides the screen and reports success', () => {
  const next = displayLauncherState(payload('valid', {
    owned_by_me: true, owner_present: true, generation: 3,
  }));
  assert.equal(next.state, 'valid');
  assert.equal(next.showScreen, false);
  assert.match(next.label, /登录成功/);
  assert.deepEqual(next.actions, ['reconnect', 'back_to_console']);
});

test('waiting_mfa asks the person to complete MFA and keeps the screen', () => {
  const next = displayLauncherState(payload('waiting_mfa'));
  assert.equal(next.state, 'waiting_mfa');
  assert.equal(next.showScreen, true);
  assert.equal(next.label, '等待 Duo 登录（本人操作）');
  assert.ok(next.actions.includes('cancel_login'));
  assert.ok(next.actions.includes('reconnect'));
  assert.ok(next.actions.includes('back_to_console'));
  assert.ok(!next.actions.includes('takeover'));
});

test('logging_in keeps the screen and offers cancel_login', () => {
  const next = displayLauncherState(payload('logging_in'));
  assert.equal(next.state, 'logging_in');
  assert.equal(next.showScreen, true);
  assert.match(next.label, /登录/);
  assert.ok(next.actions.includes('cancel_login'));
});

test('waiting_duo keeps the screen and reports the Duo push wait', () => {
  const next = displayLauncherState(payload('waiting_duo'));
  assert.equal(next.state, 'waiting_duo');
  assert.equal(next.showScreen, true);
  assert.equal(next.label, '等待本人确认 Duo 推送');
  assert.ok(next.actions.includes('cancel_login'));
  assert.ok(next.actions.includes('reconnect'));
  assert.ok(next.actions.includes('back_to_console'));
});

test('manual takeover on failed keeps the screen', () => {
  const next = displayLauncherState({
    ...payload('failed'),
    label: '需要人工接管',
    detail: 'manual_takeover_required:umpass_mfa_required',
  });
  assert.equal(next.state, 'failed');
  assert.equal(next.showScreen, true);
  assert.equal(next.label, '需要人工接管');
  assert.ok(!next.actions.includes('cancel_login'));
  assert.ok(next.actions.includes('reconnect'));
  assert.ok(next.actions.includes('back_to_console'));
});

test('a manual reason in detail keeps the screen without the label', () => {
  for (const detail of [
    'manual_takeover_required:umpass_prefill_missing',
    'manual_takeover:unrecognized_page',
    'browser_cleanup_failed；原始原因：manual_takeover_required:umpass_autofill_protected',
    'umpass_mfa_required',
  ]) {
    const next = displayLauncherState({
      ...payload('failed'), label: '登录失败', detail,
    });
    assert.equal(next.state, 'failed', detail);
    assert.equal(next.showScreen, true, detail);
  }
});

test('a non-manual failure hides the screen', () => {
  const next = displayLauncherState({
    ...payload('failed'), label: '登录失败', detail: 'browser_cleanup_failed',
  });
  assert.equal(next.state, 'failed');
  assert.equal(next.showScreen, false);
});

test('not_started and valid hide the screen', () => {
  for (const state of ['not_started', 'valid']) {
    const next = displayLauncherState(payload(state));
    assert.equal(next.state, state, state);
    assert.equal(next.showScreen, false, state);
  }
});

test('the shipped launcher keeps a manual takeover screen connected', () => {
  assert.match(LAUNCHER_JS, /waiting_duo/);
  assert.match(LAUNCHER_JS, /需要人工接管/);
  assert.match(LAUNCHER_JS, /next\.state === 'failed' && !next\.showScreen/);
});

test('terminal states show their own message and hide the screen', () => {
  const cases = {
    timed_out: /超时/,
    failed: /失败/,
    cancelled: /取消/,
    display_disconnected: /显示/,
    not_started: /未开始/,
    unknown: /读取/,
  };
  for (const [state, expected] of Object.entries(cases)) {
    const next = displayLauncherState(payload(state));
    assert.equal(next.state, state, state);
    assert.equal(next.showScreen, false, state);
    assert.match(next.label, expected, state);
    assert.ok(!next.actions.includes('cancel_login'), state);
    assert.ok(next.actions.includes('reconnect'), state);
    assert.ok(next.actions.includes('back_to_console'), state);
  }
});

test('takeover is offered only for another page that holds the operator', () => {
  const other = displayLauncherState(payload('logging_in', {
    owner_present: true, owned_by_me: false, generation: 4,
  }));
  assert.ok(other.actions.includes('takeover'));
  const mine = displayLauncherState(payload('logging_in', {
    owner_present: true, owned_by_me: true, generation: 4,
  }));
  assert.ok(!mine.actions.includes('takeover'));
  const free = displayLauncherState(payload('logging_in', {
    owner_present: false, owned_by_me: false, generation: 0,
  }));
  assert.ok(!free.actions.includes('takeover'));
});

test('unknown or missing payloads fail soft instead of throwing', () => {
  for (const value of [null, {}, { state: 'made_up' }, { state: 42 }]) {
    const next = displayLauncherState(value);
    assert.equal(next.state, 'unknown');
    assert.equal(next.showScreen, false);
    assert.ok(Array.isArray(next.actions));
  }
});

test('launcher keeps both the management and display grant exchanges', () => {
  assert.match(LAUNCHER_JS, /#console=/);
  assert.match(LAUNCHER_JS, /\/api\/session/);
  assert.match(LAUNCHER_JS, /location\.replace\('\/#overview'\)/);
  assert.match(LAUNCHER_JS, /#grant=/);
  assert.match(LAUNCHER_JS, /\/display\/grant/);
  assert.match(LAUNCHER_JS, /history\.replaceState/);
});

test('launcher polls display-state every two seconds', () => {
  assert.match(LAUNCHER_JS, /\/api\/console\/share-management\/display-state/);
  assert.match(LAUNCHER_JS, /setInterval\([^;]*,\s*2000\)/s);
  // The shipped classic launcher keeps the pure mapping inline so the SSH
  // bootstrap harness (which only replaces the RFB boundary) can run it.
  assert.match(LAUNCHER_JS, /function displayLauncherState\(/);
});

test('launcher acquires the operator before opening websockify', () => {
  const operator = LAUNCHER_JS.indexOf(
    '/api/console/share-management/display-operator');
  const websockify = LAUNCHER_JS.indexOf('/display/websockify?operator=');
  assert.ok(operator >= 0, 'operator acquire endpoint is missing');
  assert.ok(websockify >= 0, 'operator websockify query is missing');
  assert.ok(operator < websockify,
    'the page must acquire a generation before connecting');
  assert.match(LAUNCHER_JS, /action:\s*'acquire'/);
  assert.match(LAUNCHER_JS, /action:\s*'takeover'/);
  assert.match(LAUNCHER_JS, /display_operator_exists/);
  assert.match(LAUNCHER_JS, /display_session_required/);
});

test('launcher never cancels the login on unload', () => {
  assert.match(LAUNCHER_JS,
    /\/api\/console\/share-management\/backend-login-cancel/);
  assert.doesNotMatch(LAUNCHER_JS, /addEventListener\(\s*'pagehide'/);
  assert.doesNotMatch(LAUNCHER_JS, /addEventListener\(\s*'beforeunload'/);
  assert.doesNotMatch(LAUNCHER_JS, /onpagehide/);
  assert.doesNotMatch(LAUNCHER_JS, /onbeforeunload/);
});

test('launcher hides the screen and disconnects when login becomes valid', () => {
  assert.match(LAUNCHER_JS, /screen\.hidden\s*=\s*!next\.showScreen/);
  assert.match(LAUNCHER_JS, /viewer\.disconnect\(\)/);
});

test('launcher page exposes the state, actions and result regions', () => {
  for (const id of [
    'screen', 'status', 'result', 'result-text', 'actions',
    'action-reconnect', 'action-cancel', 'action-takeover', 'action-back']) {
    assert.match(LAUNCHER_HTML, new RegExp(`id="${id}"`), id);
  }
  assert.match(LAUNCHER_HTML, /\/display\/launcher\.js/);
});
