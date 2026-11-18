//! #87/#94: the retained local model actions run in-process.
//!
//! The model-sync actions (`model.catalog` and the five `model.context_*`
//! actions) are executed in-process by [`crate::model_sync::ModelSyncExecutor`]
//! and never start Python. The service actions (#92) and UM auth actions (#93)
//! are dispatched by their own executors before this one.

use serde_json::{Map, Value};

use crate::config::ConsoleConfig;
use crate::model_sync::ModelSyncExecutor;
use crate::operation_manager::{ActionExecutor, ActionFailure, OperationContext};

/// Runs the retained model-sync actions in-process.
pub struct RetainedActionExecutor {
    model_sync: ModelSyncExecutor,
}

impl RetainedActionExecutor {
    pub fn from_config(config: &ConsoleConfig) -> Self {
        Self {
            model_sync: ModelSyncExecutor::from_config(config),
        }
    }
}

impl ActionExecutor for RetainedActionExecutor {
    fn execute(
        &self,
        action: &str,
        target: &str,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        self.model_sync.execute(action, target, params, ctx)
    }
}
