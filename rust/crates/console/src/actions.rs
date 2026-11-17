//! Cloud console action catalogue.
//!
//! `console/adapters.py::build_cloud_registry` assembles exactly the UM share
//! actions. The read-only catalogue is frozen here; S6 adds the write routes
//! that execute them.

use serde_json::{json, Value};

use crate::operation_manager::local_registry;

pub fn action_catalogue() -> Value {
    json!({
        "actions": [
            {
                "action": "service.restart",
                "enabled": true,
                "disabled_reason": null,
                "cancelable": true,
                "description": "重启 UM 分享入口（停止后显式启动；中断在途工作）",
                "param_schema": {},
            },
            {
                "action": "service.start",
                "enabled": true,
                "disabled_reason": null,
                "cancelable": true,
                "description": "启动 UM 分享入口（Linux pid 文件与真实进程身份）",
                "param_schema": {},
            },
            {
                "action": "service.stop",
                "enabled": true,
                "disabled_reason": null,
                "cancelable": true,
                "description": "停止 UM 分享入口（先持久化停用意图，再发信号）",
                "param_schema": {},
            },
            {
                "action": "share.diagnostics",
                "enabled": true,
                "disabled_reason": null,
                "cancelable": true,
                "description": "UM 分享分层诊断（只读；含维护视图与故障留证）",
                "param_schema": {},
            },
            {
                "action": "share.gateway_restart",
                "enabled": true,
                "disabled_reason": null,
                "cancelable": true,
                "description": "重启分享入口 8100（单层：经受限 broker 宿主协议）",
                "param_schema": {
                    "restart_budget_s": {"type": "int", "min": 10, "max": 180},
                },
            },
            {
                "action": "share.status",
                "enabled": true,
                "disabled_reason": null,
                "cancelable": true,
                "description": "UM 分享分层状态观察（只读）",
                "param_schema": {},
            },
        ],
    })
}

/// The local role catalogue: the UM actions only. Share actions are not
/// assembled locally. `model.catalog` is enabled: #84 executes it through a
/// one-shot Python `model_sync` sidecar. The remaining actions are not wired in
/// this ticket, so they are listed as disabled with their honest reason instead of
/// claiming capability.
pub fn action_catalogue_local() -> Value {
    let actions: Vec<Value> = local_registry()
        .iter()
        .map(|spec| {
            json!({
                "action": spec.name,
                "enabled": spec.enabled,
                "disabled_reason": spec.disabled_reason,
                "cancelable": spec.cancelable,
                "description": spec.description,
                "param_schema": spec.param_schema,
            })
        })
        .collect();
    json!({"actions": actions})
}
