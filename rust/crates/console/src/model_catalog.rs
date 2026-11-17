//! #84: the local `model.catalog` sidecar.
//!
//! `model_sync` is not migrated to Rust yet. The local role executes
//! `model.catalog` by running `console.model_actions.model_catalog` in a
//! ONE-SHOT child process: the child prints exactly one JSON object and exits,
//! and the executor waits for it (bounded) before returning. A failed, timed-out
//! or malformed child is an explicit error, never a fabricated empty catalogue.
//! No long-lived Python process is started or reused.

use std::path::PathBuf;
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::config::ConsoleConfig;
use crate::operation_manager::{ActionExecutor, ActionFailure, OperationContext};
use crate::sidecar::{run_sidecar, SidecarLabels};

pub const DEFAULT_SIDECAR_TIMEOUT: Duration = Duration::from_secs(120);

/// The sidecar imports the retained Python handler in the repo root and prints
/// one envelope object. The handler's native exceptions are mapped to the same
/// `OperationError` codes the Python manager records; anything else is
/// `catalog_failed`. The child always exits, so stdout reaches EOF.
const SIDECAR_SCRIPT: &str = r#"
import json
import sys

payload = json.loads(sys.argv[1])
try:
    from console.model_actions import model_catalog
    result = model_catalog(payload.get("target", ""), payload.get("params") or {})
except Exception as exc:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        message = " ".join(str(exc).split())[:200]
    else:
        code = "catalog_failed"
        message = " ".join(("模型目录查询失败：%s: %s" % (type(exc).__name__, exc)).split())[:200]
    sys.stdout.write(json.dumps(
        {"ok": False, "error": {"code": code, "message": message}},
        ensure_ascii=False))
    sys.exit(3)
sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
"#;

const LABELS: SidecarLabels = SidecarLabels {
    failure_code: "catalog_failed",
    timeout_code: "catalog_timeout",
    label: "模型目录 sidecar",
    default_message: "模型目录 sidecar 执行失败",
    require_object_result: true,
    collapse_error_codes: true,
};

/// Runs the Python `model.catalog` handler once per operation.
pub struct PythonCatalogExecutor {
    interpreter: PathBuf,
    working_dir: PathBuf,
    timeout: Duration,
}

impl PythonCatalogExecutor {
    pub fn new(interpreter: impl Into<PathBuf>, working_dir: impl Into<PathBuf>) -> Self {
        Self {
            interpreter: interpreter.into(),
            working_dir: working_dir.into(),
            timeout: DEFAULT_SIDECAR_TIMEOUT,
        }
    }

    pub fn from_config(config: &ConsoleConfig) -> Self {
        Self::new(config.python_exe.clone(), config.repo_root.clone())
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }
}

impl ActionExecutor for PythonCatalogExecutor {
    fn execute(
        &self,
        action: &str,
        target: &str,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        if action != "model.catalog" {
            return Err(ActionFailure::new(
                "invalid_action",
                format!("unknown action {action:?}"),
            ));
        }
        let payload = json!({
            "target": target,
            "params": params,
        });
        run_sidecar(
            &self.interpreter,
            &self.working_dir,
            SIDECAR_SCRIPT,
            &payload,
            self.timeout,
            ctx,
            LABELS,
        )
    }
}
