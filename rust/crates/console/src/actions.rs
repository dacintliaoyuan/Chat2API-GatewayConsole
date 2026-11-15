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
