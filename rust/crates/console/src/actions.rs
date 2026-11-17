//! Cloud console action catalogue.
//!
//! `console/adapters.py::build_cloud_registry` assembles exactly the UM share
//! actions. The read-only catalogue is frozen here; S6 adds the write routes
//! that execute them.

use serde_json::{json, Value};

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
    let reason = "本机 Rust 控制台尚未接线该动作执行器（后续票）";
    let actions: Vec<Value> = [
        ("service.start", "启动本机 UM runtime（经 runtime 权威）"),
        ("service.stop", "停止本机 UM runtime（经 runtime 权威）"),
        ("service.restart", "重启本机 UM runtime"),
        ("account.um_status", "UM 认证状态查询（内存态只读）"),
        ("account.um_verify", "UM 账号主动核验"),
        ("account.login", "账号登录/重新授权（UM 编排）"),
        (
            "model.catalog",
            "模型目录只读查询（各来源 /v1/models；无探测、无计费、无启停）",
        ),
    ]
    .into_iter()
    .map(|(action, description)| {
        let enabled = action == "model.catalog";
        json!({
            "action": action,
            "enabled": enabled,
            "disabled_reason": if enabled { Value::Null } else { json!(reason) },
            "cancelable": true,
            "description": description,
            "param_schema": if enabled {
                json!({
                    "sources": {"type": "str", "max_len": 512},
                    "fresh": {"type": "bool"},
                })
            } else {
                json!({})
            },
        })
    })
    .collect();
    json!({"actions": actions})
}
