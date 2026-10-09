"""share_management.py — command layer behind the management API and CLI.

This module keeps HTTP routing out of the store/broker logic. A command layer
method opens a short-lived ShareStore transaction, mutates the authoritative
store, then asks the active broker to publish the resulting projection. The
share-side confirmation is recorded with ``commit_applied``; until then the
management view reports ``in_sync=False`` and never claims a revocation has
already taken effect online.

Revoking mutations (disable / rotate / delete) FAIL CLOSED: if the broker does
not confirm the new applied generation, the call raises BrokerError (503) and
records a queryable failure instead of returning a fake 200. The default broker
is NoopBroker, which therefore also refuses revoking operations offline.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from share_store import (Conflict, Disabled, KEY_MAX_CONCURRENCY_MAX,
                         KEY_MAX_CONCURRENCY_MIN, NotFound, ShareStore,
                         StoreError, VersionMismatch)

HERE = Path(__file__).resolve().parent
DEFAULT_STORE_FILE = Path(
    os.environ.get("SHARE_STORE_FILE", HERE / "data" / "share-store.db"))


class BrokerError(Exception):
    pass


class NoopBroker:
    """Offline placeholder: no high-privilege broker is installed or authorized."""

    name = "noop"

    def publish(self, projection, terminate_keys=None):
        # R8-A7: terminate_keys accepted for signature parity; a noop broker
        # publishes nothing and interrupts nothing (unchanged semantics).
        return None

    def snapshot(self):
        return {"name": self.name, "state": "offline",
                "note": "未安装/未授权高权限 broker；变更已保存但未在线发布"}

    def start(self):
        raise BrokerError("no broker installed")

    def graceful_stop(self):
        raise BrokerError("no broker installed")

    def emergency_stop(self):
        raise BrokerError("no broker installed")

    def host_verify(self, check):
        raise BrokerError("no broker installed")

    def host_stop(self):
        raise BrokerError("no broker installed")

    # Rev9 console control surface: the offline placeholder refuses the six
    # control verbs like every other host action (no broker installed).
    def maintenance_begin(self, component, lease_s):
        raise BrokerError("no broker installed")

    def maintenance_end(self, component, lease_id):
        raise BrokerError("no broker installed")

    def maintenance_status(self):
        raise BrokerError("no broker installed")

    def gateway_restart(self, component, budget_s):
        raise BrokerError("no broker installed")

    def host_evidence(self, check):
        raise BrokerError("no broker installed")

    def ts_service_control(self, action):
        raise BrokerError("no broker installed")


def _name_valid(name):
    return isinstance(name, str) and 1 <= len(name) <= 64


def _reply_terminated(reply):
    """R8-A7: extract the instance ACK's one-shot ``terminated`` count from
    either reply shape (adapter dict / BrokerResult); None when absent —
    a missing count is never read as zero."""
    if isinstance(reply, dict):
        value = reply.get("terminated")
    else:
        result = getattr(reply, "result", None)
        value = result.get("terminated") if isinstance(result, dict) else None
    return value if isinstance(value, int) else None


def _fingerprint(projection):
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True).encode("utf-8")).hexdigest()


class BrokerClientAdapter:
    """Adapt the real broker client (D1) to the ShareManager broker surface.

    The publication ACK must bind generation, fingerprint AND the acking
    instance identity (A4); any mismatch is a FAILED publication, never a
    success. Host actions are mapped onto fixed broker actions — an error
    result raises BrokerError carrying the structured error code, and an
    emergency stop reports its confirmation through the same
    ``stopped: True`` contract the fail-closed path already consumes.
    """

    name = "broker-client"

    def __init__(self, client):
        self.client = client

    def snapshot(self):
        status = self.client.status()
        if not status.ok:
            return {"name": self.name, "state": "unavailable",
                    "error_code": status.error_code}
        return {"name": self.name, "state": "ok",
                "broker": status.result}

    def publish(self, projection, terminate_keys=None):
        result = self.client.publish(projection,
                                     terminate_keys=terminate_keys)
        if not result.ok:
            raise BrokerError(f"publish rejected: {result.error_code}")
        ack = result.result
        generation = projection.get("config_generation")
        fingerprint = _fingerprint(projection)
        if ack.get("generation") != generation:
            raise BrokerError(
                f"ack generation {ack.get('generation')!r} does not match "
                f"target {generation}")
        if ack.get("fingerprint") != fingerprint:
            raise BrokerError("ack fingerprint does not match publication")
        if not isinstance(ack.get("instance_id"), str) \
                or not ack.get("instance_id"):
            raise BrokerError("ack does not identify the acking instance")
        out = {"applied_generation": generation,
               "instance_id": ack["instance_id"],
               "fingerprint": fingerprint}
        if "terminated" in ack:
            # R8-A7: one-shot closed-connection count from the instance ACK
            # (None when the running instance predates the field — never
            # fabricated).
            out["terminated"] = ack.get("terminated")
        return out

    def start(self):
        # R29-2: the broker must EXPLICITLY confirm `started: True`; an ok
        # with an empty/contradictory result is a failed start, never a
        # success, and no state string is fabricated to cover it.
        result = self.client.start()
        if not result.ok:
            raise BrokerError(f"start rejected: {result.error_code}")
        if result.result.get("started") is not True:
            raise BrokerError(
                "start result did not explicitly confirm started=True")
        return {"state": "ok", "started": True,
                "broker": result.result}

    def graceful_stop(self):
        # R31-1: drained/timed_out only describe the DRAIN PHASE - EVERY
        # close outcome requires an explicit final-stop confirmation
        # (stopped=True). A normal drain whose host stop failed is a FAILED
        # close, never a success, and never wrapped in a state string.
        result = self.client.drain()
        if not result.ok:
            raise BrokerError(f"drain rejected: {result.error_code}")
        drained = result.result.get("drained") is True
        timed_out = result.result.get("timed_out") is True
        stopped = result.result.get("stopped") is True
        if not (drained or timed_out):
            raise BrokerError(
                "drain result did not confirm drained or timed_out")
        if not stopped:
            raise BrokerError(
                "drain completed but the final stop was not confirmed")
        return dict(result.result, state="stopped")

    def emergency_stop(self):
        # R29-2: confirmation comes ONLY from the explicit stopped flag; a
        # False/missing flag stays unconfirmed and is passed through for the
        # fail-closed consumer — never covered by a state string.
        result = self.client.emergency_stop()
        if not result.ok:
            raise BrokerError(f"emergency stop rejected: {result.error_code}")
        if "stopped" not in result.result:
            raise BrokerError(
                "emergency stop result did not include a stop confirmation")
        return {"stopped": result.result.get("stopped") is True,
                "broker": result.result}

    def host_verify(self, check):
        """SYSTEM-side host evidence verdict, passed through verbatim.

        Returns True/False for a resolved verdict; None means the broker
        could NOT resolve the evidence (explicit unconfirmed - the fail-closed
        consumer treats it like any other None, never a pretend success). A
        rejected verb or a result without the verdict field is a BrokerError.
        """
        if check not in ("running", "closed"):
            raise BrokerError(f"unknown host verify check: {check!r}")
        result = self.client.host_verify(check)
        if not result.ok:
            raise BrokerError(f"host verify rejected: {result.error_code}")
        key = "host_running" if check == "running" else "host_closed"
        if key not in result.result:
            raise BrokerError(
                "host verify result did not include the verdict field")
        return result.result.get(key)

    def host_stop(self):
        result = self.client.host_stop()
        if not result.ok:
            raise BrokerError(f"host stop rejected: {result.error_code}")
        return result.result.get("host_stopped") is True

    # ------------------------------------------------------------------
    # Rev9 console control surface (A9-1..A9-4)
    #
    # The six verbs below serve the console orchestration layer
    # (console_share_production.py) and are THIN wrappers over the fixed
    # BrokerClient methods (share_broker_client.py). Unlike the
    # ShareManager-facing methods above, they return the broker's
    # BrokerResult UNCHANGED — including the structured ``error_code`` —
    # because the console layer maps those codes to its frozen console codes
    # (bad_request -> share_broker_extension_missing, capability_missing ->
    # share_capability_missing, blocked/unconfirmed ->
    # share_maintenance_refused / share_gateway_restart_failed; for
    # ts_service_control: not_installed -> share_broker_unwired,
    # bad_request -> share_broker_extension_missing, unconfirmed with the
    # re-confirmation detail -> share_ts_pending_confirmation). Folding a
    # rejection into a BrokerError message here would destroy that mapping.
    #
    # The exchange deadline stays BOUNDED: the client's own configured
    # deadline for the read-only verbs, and the frozen ``min(budget_s + 15,
    # 180)`` window for gateway_restart.

    def maintenance_begin(self, component, lease_s):
        """Begin ONE bounded maintenance lease (pause acceptance)."""
        return self.client.maintenance_begin(component, lease_s)

    def maintenance_end(self, component, lease_id):
        """End the CURRENT maintenance lease (resume acceptance)."""
        return self.client.maintenance_end(component, lease_id)

    def maintenance_status(self):
        """Read-only maintenance view (lease / instance / last_outcome)."""
        return self.client.maintenance_status()

    def gateway_restart(self, component, budget_s):
        """8100 single-layer restart through the restricted broker host path;
        the blocking exchange is bounded by the client's ``min(budget_s + 15,
        180)`` deadline."""
        return self.client.gateway_restart(component, budget_s)

    def host_evidence(self, check):
        """Read-only SYSTEM-side host evidence (limits / faults); the check
        enum is validated by the broker-side protocol whitelist."""
        return self.client.host_evidence(check)

    def ts_service_control(self, action):
        """TS service control ("start"/"stop") through the restricted
        ``ts-service-control`` broker verb; the action enum is validated by the
        broker-side protocol whitelist (the service name stays broker-side)."""
        return self.client.ts_service_control(action)


def _acked_generation(reply):
    """Extract the generation a broker reply confirms, or None."""
    if not isinstance(reply, dict):
        return None
    if reply.get("ack") and reply.get("generation") is not None:
        return reply["generation"]
    applied = reply.get("applied_generation")
    if isinstance(applied, bool) or not isinstance(applied, int):
        return None
    return applied


def _action_confirmed(kind, result):
    """The broker must EXPLICITLY confirm a host action; an arbitrary dict
    return value never counts as completion (R6-a)."""
    if not isinstance(result, dict):
        return False
    state = str(result.get("state", "")).lower()
    if kind == "start":
        return result.get("started") is True or state in ("ok", "running")
    if kind == "graceful_stop":
        # R31-1: drained/timed_out describe the drain phase ONLY; completion
        # requires the explicit final-stop confirmation.
        return (result.get("stopped") is True
                and (result.get("drained") is True
                     or result.get("timed_out") is True
                     or state in ("stopped", "drained")))
    if kind == "emergency_stop":
        # Explicit stopped flag only - no generic state string can cover a
        # missing/False confirmation (R31-1).
        return result.get("stopped") is True
    return False


class ShareManager:
    # Restart reconciliation runs ONCE per store path per process: every
    # instance construction re-scanning the ops table would let a concurrent
    # reader's fresh instance race a live executor and stomp its pending
    # operation with a "restarted" failure (R6-a).
    _recover_lock = threading.Lock()
    _recovered_paths = set()

    def __init__(self, store_path=None, broker=None):
        if store_path is None:
            store_path = os.environ.get("SHARE_STORE_FILE")
            if not store_path:
                store_path = HERE / "data" / "share-store.db"
        self.store_path = Path(store_path)
        self.broker = broker or NoopBroker()
        self._failure_lock = threading.Lock()
        self._last_failure = None
        # One publication at a time; _last_published pins the newest
        # generation+content confirmed out to the share (R5-b).
        self._publish_lock = threading.Lock()
        self._last_published = None
        self._last_terminated = None  # R8-A7: one-shot per publish
        # D2 lifecycle coordination: a monotonic epoch and the desired share
        # state. start/drain/emergency-stop are coordinated against the
        # epoch so an emergency stop can never be reopened by a start that
        # was still pending when the stop completed (A6). State authority:
        # this manager is the lifecycle owner; the broker's protected work
        # log stays a host-action record, never a second Key store.
        self._lifecycle_lock = threading.Lock()
        self._epoch = 0
        self._share_state = "stopped"
        self._recover_interrupted_operations()

    # ------------------------------------------------------------- lifecycle
    def _lifecycle_snapshot(self):
        with self._lifecycle_lock:
            return self._epoch, self._share_state

    def _lifecycle_advance(self, state):
        with self._lifecycle_lock:
            self._share_state = state
            return self._epoch

    def _lifecycle_bump_epoch(self, state):
        with self._lifecycle_lock:
            self._epoch += 1
            self._share_state = state
            return self._epoch

    # ------------------------------------------------------------------ store
    def _open(self):
        return ShareStore(self.store_path)

    def _note_failure(self, kind, message):
        with self._failure_lock:
            self._last_failure = {"kind": kind, "error": message,
                                  "at": _now_text()}

    def _attempt_emergency_stop(self):
        """Drive the restricted emergency-stop boundary once and report
        whether closure was CONFIRMED (result with explicit stopped=True).
        Never raises: an unconfirmed stop is reported, not swallowed."""
        try:
            result = self.broker.emergency_stop()
        except Exception as exc:  # noqa: BLE001
            return {"attempted": True, "confirmed": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        confirmed = isinstance(result, dict) and result.get("stopped") is True
        return {"attempted": True, "confirmed": confirmed,
                "result": result if isinstance(result, dict) else None}

    def _recover_interrupted_operations(self):
        """Restart reconciliation (R4-a.1/R6-a.1). Runs ONCE per store path
        per process; EVERY non-terminal operation belongs to the previous
        process at that point (this instance has not started any executor
        yet), so none may be left dangling:

        * publish intents with a REVOKING or UNKNOWN change: fail closed —
          drive the restricted emergency stop, verify the result and mark
          the publication blocked while closure is unconfirmed; a crash
          between the key change and the publication is exactly this case.
        * non-revoking publish intents (create/rename/enable): mark failed —
          the change is saved but not online, a safe direction.
        * host actions in pending/running: mark failed (running included —
          an executor that died mid-action never returns).
        * intent GAP (desired > applied with no open publish op): the intent
          was never recorded — NEVER infer safety from its absence; a
          fail-closed blocked record is synthesized and the stop driven.
        """
        with ShareManager._recover_lock:
            try:
                key = str(self.store_path.resolve())
            except OSError:
                key = str(self.store_path)
            if key in ShareManager._recovered_paths:
                return
            ShareManager._recovered_paths.add(key)
        try:
            store = self._open()
        except StoreError:
            return
        try:
            # 恢复判定顺序（§14/§16）：① 已应用代次覆盖的意图先收尾（不关
            # 停）；② 仍未应用且危险的意图驱动关停并持久化真实结果；③ 真
            # 正缺失意图的代次缺口合成 fail-closed 记录。未知或不合法代次
            # 不据此跳过安全处理。
            gen = store.generation()
            applied = gen["applied_generation"]

            def _target_covered(payload):
                """True 仅当 target_generation 是合法 int 且 <= applied。"""
                target = payload.get("target_generation")
                return (isinstance(target, int) and not isinstance(target, bool)
                        and target <= applied)

            def _dangerous_publish(payload):
                """跨状态转换稳定的危险分类（§16-A）：revoking=False 是明确
                非撤销（安全方向）；revoking=True 或缺失（未知/旧格式）都
                按危险处理——分类不因 pending→blocked 的状态转换丢失，下次
                启动仍保留恢复资格。"""
                return payload.get("revoking") is not False

            # 单次遍历分诊（§16-B）：每个意图在本启动内至多一次关停尝试，
            # 不再分两轮遍历导致同一意图被刚写入的 blocked 二次读取。
            for op in (store.operations(status="pending")
                       + store.operations(status="running")):
                if op["kind"] != "publish":
                    store.update_operation(op["id"], "failed", {
                        "recovered": True, "recovered_from": op["status"],
                        "error": "management process restarted before this "
                                 "host action completed"})
            for op in store.operations(kind="publish"):
                status = op["status"]
                if status in ("applied",):
                    continue
                payload = op["payload"]
                if _target_covered(payload):
                    # ① 已被已应用代次覆盖：意图内容已在线生效——明确收尾，
                    # 绝不无故关停已正确应用撤销的分享（R4-a.1 路径①②）。
                    store.update_operation(op["id"], "applied", {
                        "generation": applied,
                        "recovered": "superseded_by_applied"})
                    continue
                if not _dangerous_publish(payload):
                    # 明确非撤销（安全方向）：非终态标 failed；blocked/failed
                    # 不重试、不改类。
                    if status in ("pending", "running"):
                        store.update_operation(op["id"], "failed", {
                            "recovered": True,
                            "recovered_from": status,
                            "error": "management process restarted before "
                                     "the publication was confirmed"})
                    continue
                if (payload.get("emergency_stop") or {}).get("confirmed") is True:
                    continue  # 已确认关闭：不重复无意义关停
                # ② 危险且未被覆盖、关停未确认（pending/running/blocked/
                # failed）：本启动一次有界尝试并持久化真实结果；下次启动
                # 才允许重试（危险分类跨启动保留）。
                stop = self._attempt_emergency_stop()
                store.update_operation(op["id"], "blocked", {
                    "blocked": not stop["confirmed"],
                    "recovered": ("restart_recheck"
                                  if status in ("blocked", "failed") else True),
                    "recovered_from": status,
                    "emergency_stop": stop})
            # 真正代次缺口（§18）：未应用变更区间（applied+1 .. desired）内
            # 每个代次都必须有相应代次的有效意图解释其性质——已应用收尾、
            # 已按危险处理的 blocked、明确非撤销或已确认关闭的 failed 都解
            # 释自己的代次；一条旧记录或较新非撤销记录不能替缺失的另一代撤
            # 销证明安全。没有任何有效解释的代次按未知危险缺口处理：合成可
            # 追溯记录并驱动受限关停，未确认时保持 blocked。初始新库
            # （applied=0/desired=1、无 keys）不是变更缺口。
            gen = store.generation()
            explained = set()
            for op in store.operations(kind="publish"):
                payload = op["payload"]
                target = payload.get("target_generation")
                # §20：合成 gap 记录负责的缺失代次列表也计入解释——既有
                # gap 工作及其停止确认在后续恢复中被复用（记录不增殖、不
                # 重复关停）；记录之外的新缺失代次仍会被识别，不被掩盖。
                # 判据是字段本身（unexplained_generations 只在合成 gap 时
                # 写入），不依赖 recovered 标记值——重试会把它覆盖为
                # restart_recheck，但缺口列表必须继续被读取。
                unexplained_list = payload.get("unexplained_generations")
                if isinstance(unexplained_list, list):
                    for g in unexplained_list:
                        if isinstance(g, int) and not isinstance(g, bool):
                            explained.add(g)
                if not isinstance(target, int) or isinstance(target, bool):
                    continue  # 未知/非法代次本身按危险处理，不解释区间代次
                status = op["status"]
                if status == "applied":
                    explained.add(target)
                elif status == "blocked":
                    explained.add(target)   # 该代次已按危险处理（含关停尝试）
                elif status == "failed" and (
                        payload.get("revoking") is False
                        or (payload.get("emergency_stop") or {}).get("confirmed")
                        is True):
                    explained.add(target)   # 明确非撤销 / 已确认关闭
            unexplained = [g for g in range(max(gen["applied_generation"], 1) + 1,
                                            gen["config_generation"] + 1)
                           if g not in explained]
            # 代次 1 是库创建时的初始代次（无变更、无需意图解释）；未应用
            # 变更区间从 max(applied, 1)+1 起。仅真正的初始库（applied=0、
            # desired=1、无任何 publish 记录）不是变更缺口。
            is_initial_store = (gen["applied_generation"] == 0
                                and gen["config_generation"] == 1
                                and not store.operations(kind="publish"))
            if unexplained and not is_initial_store:
                op_id, _ = store.record_operation("publish", "pending", {
                    "target_generation": gen["config_generation"],
                    "revoking": True, "recovered": "intent_gap",
                    "unexplained_generations": unexplained})
                stop = self._attempt_emergency_stop()
                store.update_operation(op_id, "blocked", {
                    "blocked": not stop["confirmed"], "recovered": "intent_gap",
                    "emergency_stop": stop})
            # R31-2: 重启对账对持久生命周期阻塞做一次有界重新确认——驱动
            # 受限关停并核验；确认关闭解除阻塞，未确认保持 blocked。
            for block_op in store.operations(kind="lifecycle_block",
                                             status="blocked"):
                self._resolve_lifecycle_block_on(store, block_op)
        except StoreError:
            pass
        finally:
            store.close()

    def _publish(self, store, terminate_keys=None):
        """Publish the CURRENT projection under the publish lock.

        ``terminate_keys`` (R8-A7 revoke-and-terminate) is a one-shot list
        forwarded to the broker publication and the instance load; it never
        persists in the projection content. The audit intent record carries
        it (key ids only — no secrets).

        Returns (published, applied_generation_or_None, error_or_None,
        operation_id). The projection content and its generation come from
        one store snapshot; the broker ack must confirm EXACTLY this target
        generation; an older generation may never overwrite a newer published
        one and a same-generation publish must carry identical content
        (R4-a/R5-b). Every attempt is persisted in the ops table.
        """
        projection = store.render_projection()
        generation = projection["config_generation"]
        fingerprint = _fingerprint(projection)
        # R4-a.1: the mutation transaction already recorded a pending publish
        # intent for this generation — REUSE it so a crash between the key
        # change and this call can never leave the intent orphaned; only
        # synthesize a new record when no intent exists (e.g. direct calls).
        intent = store.find_pending_publish_intent(generation)
        if intent is not None:
            op_id = intent["id"]
        else:
            op_id, _created = store.record_operation(
                "publish", "pending", {"target_generation": generation})
        with self._publish_lock:
            last = self._last_published
            if last is not None and generation < last["generation"]:
                error = (f"publication generation regression refused "
                         f"(already published generation {last['generation']} "
                         f"> target {generation})")
                store.update_operation(op_id, "failed",
                                       {"error": error, "refused": "regression"})
                self._note_failure("publish", error)
                return False, None, error, op_id
            if (last is not None and generation == last["generation"]
                    and fingerprint != last["fingerprint"]):
                error = "same-generation projection content mismatch refused"
                store.update_operation(op_id, "failed",
                                       {"error": error,
                                        "refused": "content_mismatch"})
                self._note_failure("publish", error)
                return False, None, error, op_id
            try:
                reply = self.broker.publish(projection,
                                        terminate_keys=terminate_keys)
            except Exception as exc:  # noqa: BLE001
                error = f"projection publication failed: {type(exc).__name__}: {exc}"
                store.update_operation(op_id, "failed", {"error": error})
                self._note_failure("publish", error)
                return False, None, error, op_id
            acked = _acked_generation(reply)
            if acked != generation:
                error = (f"broker ack does not confirm target generation "
                         f"{generation} (ack {acked!r}); the share may still "
                         f"run an older projection")
                store.update_operation(op_id, "failed",
                                       {"error": error, "ack": acked})
                self._note_failure("publish", error)
                return False, None, error, op_id
            store.commit_applied(generation)
            self._last_published = {"generation": generation,
                                    "fingerprint": fingerprint}
            store.update_operation(op_id, "applied", {"generation": generation})
            # R8-A7: the instance ACK reports how many live connections the
            # terminate list actually closed (best-effort count for the
            # caller; a missing field stays None, never a fabricated 0).
            # Reply shapes: plain dict (BrokerClientAdapter ack dict, count
            # at top level) or BrokerResult (raw client, count under
            # .result) — read both, never assume one.
            self._last_terminated = _reply_terminated(reply)
            return True, generation, None, op_id

    def _response(self, store, payload):
        gen = store.generation()
        out = dict(payload)
        out["desired_generation"] = gen["config_generation"]
        out["applied_generation"] = gen["applied_generation"]
        out["in_sync"] = gen["in_sync"]
        return out

    def _fail_closed(self, store, op_id, publish_error):
        """A revoking mutation failed to publish: drive the restricted
        emergency-stop boundary, persist whether closure was confirmed and
        keep the publication marked blocked while it is not (R4-a)."""
        try:
            result = self.broker.emergency_stop()
        except Exception as exc:  # noqa: BLE001
            stop = {"attempted": True, "confirmed": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        else:
            confirmed = isinstance(result, dict) and result.get("stopped") is True
            stop = {"attempted": True, "confirmed": confirmed,
                    "result": result if isinstance(result, dict) else None}
        store.update_operation(op_id, "failed", {
            "error": publish_error,
            "blocked": not stop["confirmed"],
            "emergency_stop": stop})
        return stop

    def _require_applied(self, store, payload, terminate_keys=None):
        """Fail a revoking mutation unless its projection was confirmed by an
        ack for EXACTLY the target generation; otherwise fail closed through
        the restricted emergency-stop boundary (R4-a).

        ``terminate_keys`` (R8-A7) rides the fail-closed publication so a
        revoke-and-terminate disables AND interrupts in one confirmed step."""
        published, applied, error, op_id = self._publish(
            store, terminate_keys=terminate_keys)
        payload["published"] = published
        payload["publication_error"] = error
        if not published:
            stop = self._fail_closed(store, op_id, error)
            state = "confirmed" if stop["confirmed"] else "NOT confirmed (blocked)"
            raise BrokerError(
                f"revoking operation not applied online (desired="
                f"{store.generation()['config_generation']}); "
                f"existing projection may still authorize the old key; "
                f"emergency stop {state}")
        return self._response(store, payload)

    # ------------------------------------------------------------- read paths
    def status(self):
        store = self._open()
        try:
            rows = store.list_keys()
            gen = store.generation()
            broker = self.broker.snapshot() if hasattr(self.broker, "snapshot") else {}
            with self._failure_lock:
                failure = self._last_failure
            blocked = self._publication_blockers(store)
            return {
                "schema_version": gen["schema_version"],
                "share_max_concurrency": store.get_share_max_concurrency(),
                "desired_generation": gen["config_generation"],
                "applied_generation": gen["applied_generation"],
                "in_sync": gen["in_sync"],
                "pending_revocation": gen["config_generation"] > gen["applied_generation"],
                "active_keys": len([r for r in rows if r["enabled"]]),
                "total_keys": len(rows),
                "broker": broker,
                "last_publication_failure": failure,
                "blocked_publications": blocked,
                "blocked": any(b["blocked"] for b in blocked),
            }
        finally:
            store.close()

    def _publication_blockers(self, store):
        """Revocation publications whose target generation was never confirmed
        AND whose restricted stop could not confirm the share closed (R4-a).
        Superseded by a later confirmed generation they clear."""
        applied = store.generation()["applied_generation"]
        blockers = []
        for op in store.operations(kind="publish"):
            if op["status"] not in ("pending", "blocked", "failed"):
                continue
            payload = op["payload"]
            target = payload.get("target_generation")
            if isinstance(target, bool) or not isinstance(target, int):
                continue
            if target <= applied:
                continue
            stop = payload.get("emergency_stop") or {}
            confirmed = stop.get("confirmed") is True and op["status"] != "pending"
            blockers.append({
                "operation_id": op["id"],
                "status": op["status"],
                "target_generation": target,
                "stop_confirmed": stop.get("confirmed") is True,
                "blocked": payload.get("blocked") is True and not confirmed,
                "recovered": payload.get("recovered"),
                "created_at": op["created_at"],
            })
        return blockers

    def list_keys(self, include_deleted=False):
        """List key metadata; ``include_deleted`` keeps deleted identities.

        Rev8 B8-6: the per-key cost read face projects the key's name and
        deletion state, so a deleted or rotated key keeps its readable name
        under its stable key_id. ``include_deleted`` only widens which
        identities are listed — the row shape and the secret exclusion
        (``_public_row``) are unchanged.
        """
        store = self._open()
        try:
            return {"keys": store.list_keys(include_deleted=include_deleted)}
        finally:
            store.close()

    def get_key(self, key_id):
        store = self._open()
        try:
            return store.get_key(key_id)
        finally:
            store.close()

    def get_operation(self, operation_id):
        store = self._open()
        try:
            return store.get_operation(operation_id)
        finally:
            store.close()

    # ------------------------------------------------------------- mutations
    def create_key(self, name, max_concurrency=8, idempotency_key=None):
        """Create a share key; the plaintext is returned EXACTLY ONCE here.

        New keys default to a per-key concurrency limit of 8 (R6-A2 product
        default, adjustable 1..64); idempotency replay semantics are
        unchanged — a replayed create never replays the plaintext.
        """
        if not _name_valid(name):
            raise StoreError("invalid name")
        if max_concurrency is None:
            max_concurrency = 8
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) \
                or not (KEY_MAX_CONCURRENCY_MIN <= max_concurrency
                        <= KEY_MAX_CONCURRENCY_MAX):
            raise StoreError(
                f"max_concurrency must be an integer in "
                f"{KEY_MAX_CONCURRENCY_MIN}..{KEY_MAX_CONCURRENCY_MAX}")
        store = self._open()
        try:
            result = store.create_key(name, idempotency_key=idempotency_key,
                                      max_concurrency=max_concurrency)
            published, _, pub_error, _ = self._publish(store)
            result["published"] = published
            result["publication_error"] = pub_error
            return self._response(store, result)
        finally:
            store.close()

    def reveal_key_secret(self, key_id, expected_row_version=None):
        """Narrow per-key plaintext readback (Plan-Revision 11 contract).

        Read-only delegation to the store's reveal — no publish, no
        operation record, no ledger row. The store answers with distinct
        failures (legacy/undecryptable/deleted/version-conflict/not-found);
        the plaintext rides ONLY this immediate response and is never
        logged."""
        store = self._open()
        try:
            return store.reveal_key_secret(
                key_id, expected_row_version=expected_row_version)
        finally:
            store.close()

    def update_key(self, key_id, name=None, enabled=None,
                   max_concurrency=None, expected_row_version=None,
                   idempotency_key=None):
        """Atomic PATCH. Lowering ``max_concurrency`` is a REVOCATION-direction
        change and takes the SAME fail-closed path as ``enabled=False``
        (``_require_applied``): the projection must be confirmed online or
        the call raises BrokerError (503). Raising the limit is the safe
        direction and publishes best-effort. The direction is decided from
        the publish intent recorded INSIDE the mutation transaction (a
        generation bump plus a revoking intent), not from a pre-read —
        no race window between read and write."""
        store = self._open()
        try:
            gen_before = store.generation()["config_generation"]
            result = store.update_key(key_id, new_name=name, enabled=enabled,
                                      max_concurrency=max_concurrency,
                                      expected_row_version=expected_row_version)
            gen_after = store.generation()["config_generation"]
            revoking = False
            if gen_after > gen_before:
                intent = store.find_pending_publish_intent(gen_after)
                revoking = bool(intent and intent["payload"].get("revoking"))
            if revoking:
                return self._require_applied(store, result)
            published, _, pub_error, _ = self._publish(store)
            result["published"] = published
            result["publication_error"] = pub_error
            return self._response(store, result)
        finally:
            store.close()

    def set_share_limits(self, share_max_concurrency, expected_share_max=None,
                         idempotency_key=None):
        """Adjust the global share concurrency limit (1..64).

        * ``expected_share_max`` is an optimistic gate on the CURRENT value;
          a mismatch raises VersionMismatch, which the existing
          ``status_code_for`` mapping already renders as 409. Choice frozen
          in the contract notes: REUSE the existing conflict mechanism
          (VersionMismatch) instead of introducing a new exception type —
          the two conflicts have the same semantics (concurrent
          modification) and the same HTTP answer.
        * LOWERING the limit is a revocation-direction change (fail-closed
          via ``_require_applied``); raising publishes best-effort. Neither
          direction kills in-flight requests: the share enforces limits at
          the accept boundary only.
        """
        if isinstance(share_max_concurrency, bool) \
                or not isinstance(share_max_concurrency, int) \
                or not (KEY_MAX_CONCURRENCY_MIN <= share_max_concurrency
                        <= KEY_MAX_CONCURRENCY_MAX):
            raise StoreError(
                f"share_max_concurrency must be an integer in "
                f"{KEY_MAX_CONCURRENCY_MIN}..{KEY_MAX_CONCURRENCY_MAX}")
        if expected_share_max is not None:
            if isinstance(expected_share_max, bool) \
                    or not isinstance(expected_share_max, int):
                raise StoreError("expected_share_max must be an integer")
        store = self._open()
        try:
            result = store.set_share_max_concurrency(
                share_max_concurrency, expected=expected_share_max,
                idempotency_key=idempotency_key)
            if result.get("changed") and \
                    share_max_concurrency < result["previous"]:
                return self._require_applied(store, result)
            published, _, pub_error, _ = self._publish(store)
            result["published"] = published
            result["publication_error"] = pub_error
            return self._response(store, result)
        finally:
            store.close()

    def rotate_key(self, key_id, idempotency_key=None, expected_row_version=None):
        store = self._open()
        try:
            result = store.rotate_key(key_id, idempotency_key=idempotency_key,
                                      expected_row_version=expected_row_version)
            return self._require_applied(store, result)
        finally:
            store.close()

    def delete_key(self, key_id, expected_row_version=None):
        store = self._open()
        try:
            result = store.delete_key(key_id, expected_row_version=expected_row_version)
            return self._require_applied(store, result)
        finally:
            store.close()

    def revoke_and_terminate_key(self, key_id, expected_row_version=None,
                                 idempotency_key=None):
        """R8-A7 revoke-and-terminate: disable the key (立即拒新, fail-closed
        direction) and PUBLISH with a one-shot ``terminate_keys`` list so the
        share gateway interrupts exactly THIS key's in-flight requests. Other
        keys' requests and the local machine are never touched. The
        terminated count comes from the instance ACK; a publish that cannot
        reach the instance still leaves the key disabled (refusal already
        live) and the termination pending — reported honestly, never faked.
        """
        store = self._open()
        try:
            # The store PATCH has no idempotency parameter (idempotency is
            # enforced by the usagesrv ops layer / _reject replay at the
            # route); the store mutation itself stays single-step.
            result = store.update_key(
                key_id, enabled=False,
                expected_row_version=expected_row_version)
            disabled = self._require_applied(store, result,
                                             terminate_keys=[key_id])
            # _last_terminated is the instance ACK's closed-connection count
            # from the publication this call drove (one-shot; None when the
            # ack shape carries no count — never fabricated).
            return dict(disabled, terminated=self._last_terminated)
        finally:
            store.close()

    # ------------------------------------------------------------- migration (D3)
    def migrate(self, source_digests, run_copy_digests=None, idem_map=None):
        """Controlled migration entry (D3): the ONLY path into migrate_apply.

        preview 先行——任何源/运行副本授权冲突（含运行副本多出的身份）
        MigrationConflict 停查，不取并集、不复活 friend2；preview 通过后
        受控调用 migrate_apply（同一事务含行/代次/发布意图），随后持久化
        ops 记录。备份由调用方在调用本方法前完成（ShareStore.backup）。
        """
        store = self._open()
        try:
            preview = store.migration_preview(source_digests,
                                              run_copy_digests or {})
            result, idem_map = store.migrate_apply(source_digests,
                                                   idem_map=idem_map)
            generation = store.generation()
            # R29-7: record_operation returns (op_id, created) — unpack the
            # scalar id so callers/JSON see a string, never a tuple.
            op_id, _created = store.record_operation(
                "migrate", "completed",
                {"migrated": sorted(result),
                 "source_count": preview["source_count"],
                 "run_copy_count": preview["run_copy_count"],
                 "config_generation": generation["config_generation"]})
            return {"mapping": result, "idempotency_map": idem_map,
                    "preview": preview,
                    "operation_id": op_id,
                    "desired_generation": generation["config_generation"],
                    "applied_generation": generation["applied_generation"]}
        finally:
            store.close()

    # ------------------------------------------------------------- host actions
    def _update_operation(self, op_id, status, payload_merge):
        try:
            store = self._open()
        except StoreError:
            return
        try:
            store.update_operation(op_id, status, payload_merge)
        finally:
            store.close()

    def _persist_lifecycle_block(self, kind, compensation):
        """R31-2: persist 'entry may be open, close NOT confirmed' as a
        queryable lifecycle block (independent of the publish-gap ops).
        Later starts refuse; restart reconciliation re-drives the close with
        a bounded attempt; a verified close releases the block."""
        try:
            store = self._open()
        except StoreError:
            return None
        try:
            op_id, _created = store.record_operation("lifecycle_block",
                                                     "blocked", {
                                                         "kind": kind,
                                                         "compensation":
                                                             compensation})
            return op_id
        finally:
            store.close()

    def _lifecycle_blocks(self, store):
        """Unresolved lifecycle blocks: blocked lifecycle_block ops that
        have not been re-driven to a VERIFIED close."""
        return list(store.operations(kind="lifecycle_block", status="blocked"))

    def _resolve_lifecycle_block_on(self, store, op):
        """One bounded re-confirmation attempt for a persisted lifecycle
        block (restart reconciliation, R31-2): drive the restricted
        emergency stop; a VERIFIED close releases the block (applied), an
        unconfirmed close keeps it blocked."""
        compensation = None
        closed = False
        try:
            compensation = self.broker.emergency_stop()
            closed = isinstance(compensation, dict) \
                and compensation.get("stopped") is True
        except Exception as exc:  # noqa: BLE001
            compensation = {"error": f"{type(exc).__name__}: {exc}"}
        if closed:
            store.update_operation(op["id"], "applied", {
                "resolved": True, "compensation": compensation})
        else:
            store.update_operation(op["id"], "blocked", {
                "reconfirmed": True,
                "compensation": compensation})

    def _execute_host_action(self, op_id, kind, fn, epoch_at_accept):
        """Executor thread: advance the persisted operation independently of
        the HTTP caller (R6-a) AND against the lifecycle epoch (D2): a
        completed emergency stop may never be reopened by a start ACCEPTED
        before the stop ran (A6). The epoch is snapshotted at ACCEPT time —
        an executor that starts late still carries its acceptance epoch."""
        self._update_operation(op_id, "running", {})
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            # R31-2: an exception does NOT mean the side effect never
            # happened - a late start that opened the entry and then threw
            # must still pass through the epoch compensation check.
            if kind == "start" \
                    and self._lifecycle_snapshot()[0] != epoch_at_accept:
                compensation = None
                closed = False
                try:
                    compensation = self.broker.emergency_stop()
                    closed = isinstance(compensation, dict) \
                        and compensation.get("stopped") is True
                except BrokerError as comp_exc:
                    compensation = {"error": str(comp_exc)}
                if closed:
                    self._update_operation(op_id, "failed", {
                        "error": f"{type(exc).__name__}: {exc}; close "
                                 f"compensation VERIFIED (A6)",
                        "exception": True,
                        "closed": True})
                    self._lifecycle_bump_epoch("stopped")
                    return
                block_op = self._persist_lifecycle_block(kind, compensation)
                self._update_operation(op_id, "failed", {
                    "error": f"{type(exc).__name__}: {exc}; close "
                             f"compensation NOT confirmed (lifecycle "
                             f"blocked)",
                    "exception": True,
                    "closed": False,
                    "lifecycle_block": block_op})
                self._note_failure(
                    "start_exception_superseded",
                    "start raised after opening during emergency stop; "
                    "close compensation NOT confirmed")
                return
            self._update_operation(op_id, "failed",
                                   {"error": f"{type(exc).__name__}: {exc}"})
            return
        epoch_now, state_now = self._lifecycle_snapshot()
        # R31-1: the lifecycle state is advanced ONLY AFTER the broker
        # result is explicitly confirmed - an unconfirmed stop/failed start
        # must never be recorded as running/stopped in the manager.
        confirmed = _action_confirmed(kind, result)
        if kind == "emergency_stop":
            # 紧急停止意图已执行：epoch 前进；期望状态仅在确认后推进——
            # 未确认关闭不写成 stopped。
            self._lifecycle_bump_epoch(
                "stopped" if confirmed else "blocked")
        elif kind == "start" and epoch_now != epoch_at_accept:
            # R29-1/R31-2: the entry may already have been opened by fn() -
            # a status change alone never satisfies A6. Drive a VERIFIED
            # close compensation through the broker; an unconfirmed
            # compensation persists a lifecycle block that later starts
            # must refuse and restart reconciliation re-drives (R31-2).
            compensation = None
            closed = False
            try:
                compensation = self.broker.emergency_stop()
                closed = isinstance(compensation, dict) \
                    and compensation.get("stopped") is True
            except BrokerError as comp_exc:
                compensation = {"error": str(comp_exc)}
            if closed:
                self._update_operation(op_id, "failed", {
                    "error": "superseded by a completed emergency stop; "
                             "close compensation VERIFIED (A6)",
                    "superseded": True,
                    "closed": True,
                    "compensation": compensation})
                self._lifecycle_bump_epoch("stopped")
                return
            # 补偿未确认：持久生命周期阻塞（R31-2）——后续 start 拒绝，
            # 重启对账有界重新确认/关闭。
            block_op = self._persist_lifecycle_block(kind, compensation)
            self._update_operation(op_id, "failed", {
                "error": "superseded by a completed emergency stop; close "
                         "compensation NOT confirmed (lifecycle blocked)",
                "superseded": True,
                "closed": False,
                "compensation": compensation,
                "lifecycle_block": block_op})
            self._note_failure(
                "start_superseded",
                "late start opened during emergency stop; close "
                "compensation NOT confirmed")
            return
        elif kind == "start" and confirmed:
            self._lifecycle_advance("running")
        elif kind == "graceful_stop" and confirmed \
                and epoch_now == epoch_at_accept:
            self._lifecycle_advance("stopped")
        if not confirmed:
            self._update_operation(op_id, "failed", {
                "error": "broker result did not confirm completion",
                "broker": result})
            return
        self._update_operation(op_id, "completed", {"broker": result})

    def _launch_host_action(self, kind, fn, idempotency_key=None):
        """Persist the operation BEFORE executing and return 202 immediately.

        A retry with the same idempotency key resolves to the SAME operation
        of the SAME kind — a key bound to a different action returns 409
        (R6-a.2), never a foreign success. Without a key, the single active
        operation of this kind is atomically claimed (BEGIN IMMEDIATE reuse-
        or-insert, R6-a.3), so a timed-out client retrying or two concurrent
        requests can never double-execute the action. Starting additionally
        requires NO unresolved blocked publication (D2 start gate)."""
        if kind == "start":
            store = self._open()
            try:
                blockers = [b for b in self._publication_blockers(store)
                            if b["blocked"]]
                lifecycle_blocks = self._lifecycle_blocks(store)
            finally:
                store.close()
            if lifecycle_blocks:
                # R31-2: an unresolved lifecycle block (entry possibly open,
                # close NOT confirmed) refuses every later start until the
                # restart reconciliation verifies closure.
                raise BrokerError(
                    "share start is blocked by an unresolved lifecycle "
                    "block (close NOT confirmed); restart reconciliation "
                    "must verify closure first")
            if blockers:
                raise BrokerError(
                    "share is blocked by an unconfirmed revocation "
                    "publication; resolve blocked_publications before start")
        store = self._open()
        try:
            if idempotency_key:
                existing = store.find_operation_by_idempotency_key(idempotency_key)
                if existing is not None:
                    # R6-a.2: an idempotency key is bound to ONE action kind.
                    if existing["kind"] != kind:
                        raise Conflict(
                            "idempotency key already used for a different "
                            "operation kind")
                    return {"operation_id": existing["id"],
                            "status": existing["status"], "reused": True,
                            "broker": None}
                op_id, created = store.record_operation(
                    kind, "pending",
                    {"action": kind, "started_at": _now_text()},
                    idempotency_key=idempotency_key)
                if not created:
                    # 并发输家（显式 key）：同 kind 的赢家 op 已存在——复用
                    # 它，绝不为自己再启动一个 executor。
                    return {"operation_id": op_id, "status": "pending",
                            "reused": True, "broker": None}
            else:
                # R6-a.3: 原子认领——复用或插入在同一 BEGIN IMMEDIATE 事务
                # 内完成，同类活动操作至多一个，后到者复用先到者。
                op_id, created = store.claim_active_operation(
                    kind, {"action": kind, "started_at": _now_text()})
                if not created:
                    return {"operation_id": op_id, "status": "pending",
                            "reused": True, "broker": None}
        finally:
            store.close()
        # 受理时刻的 epoch：迟到的 executor 也携带它受理时的轮次。
        epoch_at_accept = self._lifecycle_snapshot()[0]
        threading.Thread(target=self._execute_host_action,
                         args=(op_id, kind, fn, epoch_at_accept),
                         daemon=True).start()
        return {"operation_id": op_id, "status": "pending", "broker": None}

    def start(self, idempotency_key=None):
        return self._launch_host_action("start", self.broker.start,
                                        idempotency_key)

    def graceful_stop(self, idempotency_key=None):
        return self._launch_host_action("graceful_stop",
                                        self.broker.graceful_stop,
                                        idempotency_key)

    def emergency_stop(self, idempotency_key=None):
        return self._launch_host_action("emergency_stop",
                                        self.broker.emergency_stop,
                                        idempotency_key)


def _now_text():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
def status_code_for(exc):
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, (Conflict, Disabled, VersionMismatch)):
        return 409
    if isinstance(exc, BrokerError):
        return 503
    if isinstance(exc, StoreError):
        return 400
    return 500
