//! #87: the retained local actions sidecar.
//!
//! The local role's service, account and model-context actions are not migrated
//! to Rust yet. Each one runs its retained Python handler in a ONE-SHOT child
//! process through [`crate::sidecar`]; the child prints exactly one JSON envelope
//! and exits. A failed, timed-out or cancelled child is an explicit error, never a
//! simulated success. `model.catalog` keeps the #84 catalogue executor unchanged.

use std::path::PathBuf;
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::config::ConsoleConfig;
use crate::model_catalog::PythonCatalogExecutor;
use crate::operation_manager::{ActionExecutor, ActionFailure, OperationContext};
use crate::sidecar::{run_sidecar, SidecarLabels};

const SIDECAR_SCRIPT: &str = r#"
import contextlib
import json
import sys

payload = json.loads(sys.argv[1])
action = payload.get("action") or ""
target = payload.get("target") or ""
params = payload.get("params") or {}


def fail(exc):
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        message = " ".join(str(exc).split())[:200]
    else:
        code = "action_failed"
        message = " ".join(("%s: %s" % (type(exc).__name__, exc)).split())[:200]
    sys.stdout.write(json.dumps(
        {"ok": False, "error": {"code": code, "message": message}},
        ensure_ascii=False))
    sys.exit(3)


try:
    # Handler/import chatter must never corrupt the single stdout envelope.
    with contextlib.redirect_stdout(sys.stderr):
        if action == "service.start":
            from console.adapters import RuntimeServiceAdapter
            result = RuntimeServiceAdapter().service_start(target, params)
        elif action == "service.stop":
            from console.adapters import RuntimeServiceAdapter
            result = RuntimeServiceAdapter().service_stop(target, params)
        elif action == "service.restart":
            from console.adapters import RuntimeServiceAdapter
            result = RuntimeServiceAdapter().service_restart(target, params)
        elif action == "account.um_status":
            from console.um_adapter import um_auth_status
            result = um_auth_status(target, params)
        elif action == "account.um_verify":
            from console.um_adapter import um_verify_account
            result = um_verify_account(target, params)
        elif action == "account.login":
            from console.um_login import relogin_um
            result = relogin_um(target, params)
        elif action == "model.context_probe_preview":
            from console.model_actions import model_context_probe_preview
            result = model_context_probe_preview(target, params)
        elif action == "model.context_probe_run":
            from console.model_actions import model_context_probe_run
            result = model_context_probe_run(target, params)
        elif action == "model.context_overrides":
            from console.model_actions import model_context_overrides
            result = model_context_overrides(target, params)
        elif action == "model.context_override_set":
            from console.model_actions import model_context_override_set
            result = model_context_override_set(target, params)
        elif action == "model.context_override_clear":
            from console.model_actions import model_context_override_clear
            result = model_context_override_clear(target, params)
        else:
            raise RuntimeError("unknown retained action: %s" % action)
except Exception as exc:
    fail(exc)

sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
"#;

const LABELS: SidecarLabels = SidecarLabels {
    failure_code: "action_failed",
    timeout_code: "action_timeout",
    label: "动作 sidecar",
    default_message: "动作 sidecar 执行失败",
    require_object_result: false,
    collapse_error_codes: false,
};

/// Runs the retained Python handlers for the local service/account/context actions.
pub struct RetainedActionExecutor {
    interpreter: PathBuf,
    working_dir: PathBuf,
    catalog: PythonCatalogExecutor,
}

impl RetainedActionExecutor {
    pub fn new(interpreter: impl Into<PathBuf>, working_dir: impl Into<PathBuf>) -> Self {
        let interpreter = interpreter.into();
        let working_dir = working_dir.into();
        Self {
            catalog: PythonCatalogExecutor::new(interpreter.clone(), working_dir.clone()),
            interpreter,
            working_dir,
        }
    }

    pub fn from_config(config: &ConsoleConfig) -> Self {
        Self::new(config.python_exe.clone(), config.repo_root.clone())
    }
}

/// Python budgets from the console action contract (front-end `timeoutMs`).
fn timeout_for(action: &str) -> Option<Duration> {
    Some(match action {
        "service.start" | "service.stop" => Duration::from_secs(120),
        "service.restart" => Duration::from_secs(180),
        "account.um_status" => Duration::from_secs(30),
        "account.um_verify" => Duration::from_secs(60),
        "account.login" => Duration::from_secs(300),
        "model.context_probe_preview"
        | "model.context_overrides"
        | "model.context_override_set"
        | "model.context_override_clear" => Duration::from_secs(120),
        "model.context_probe_run" => Duration::from_secs(1200),
        _ => return None,
    })
}

impl ActionExecutor for RetainedActionExecutor {
    fn execute(
        &self,
        action: &str,
        target: &str,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        if action == "model.catalog" {
            return self.catalog.execute(action, target, params, ctx);
        }
        let Some(timeout) = timeout_for(action) else {
            return Err(ActionFailure::new(
                "invalid_action",
                format!("unknown action {action:?}"),
            ));
        };
        let payload = json!({
            "action": action,
            "target": target,
            "params": params,
        });
        run_sidecar(
            &self.interpreter,
            &self.working_dir,
            SIDECAR_SCRIPT,
            &payload,
            timeout,
            ctx,
            LABELS,
        )
    }
}
