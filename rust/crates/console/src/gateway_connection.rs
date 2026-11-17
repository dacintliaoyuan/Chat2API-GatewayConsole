//! Local `GET /api/console/gateway-connection` read face.
//!
//! `console/gateway_connection.py::connection_info` is a narrow read: it never
//! sends an HTTP request, never triggers a login and never copies the control
//! token. The port and Base URL come from the runtime snapshot only. The Rust
//! local role does not inspect the running instance command line, so the auth and
//! key facts stay explicitly `unknown` instead of inventing a no-key state.

use serde_json::{json, Value};

pub const SERVICE: &str = "chat2api";
pub const PROTOCOL: &str = "openai-compatible";
pub const PROTOCOL_PATH: &str = "/v1";
pub const HEALTH_PATH: &str = "/health";
pub const KEY_SOURCE: &str = "env:CHAT2API_LOCAL_API_KEY";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatewayConnectionError(pub String);

pub fn connection_info(
    service: &str,
    row: Option<&Value>,
    checked_epoch: i64,
) -> Result<Value, GatewayConnectionError> {
    if service != SERVICE {
        return Err(GatewayConnectionError(format!(
            "未知或不在范围的 service：{service:?}"
        )));
    }
    let mut payload = json!({
        "ok": true,
        "service": service,
        "display_name": null,
        "port": null,
        "health_path": HEALTH_PATH,
        "health_path_source": "descriptor",
        "base_url": null,
        "protocol": PROTOCOL,
        "protocol_path": PROTOCOL_PATH,
        "runtime_state": null,
        "runtime_healthy": null,
        "auth": "unknown",
        "key_state": "unknown",
        "key_plaintext": null,
        "key_source": KEY_SOURCE,
        "key_source_state": "unknown",
        "key_in_use": "unknown",
        "reason": "",
        "evidence": [],
        "checked_epoch": checked_epoch,
    });
    let Some(entry) = row else {
        payload["reason"] = json!(
            "运行实例无法确认：runtime 快照不可用或缺少该服务条目；端口与 Base URL 不从显示名或描述符猜算"
        );
        payload["evidence"] = json!([
            "runtime 快照不可用（console_services_snapshot 失败或缺少 chat2api 条目）"
        ]);
        return Ok(payload);
    };
    if let Some(display_name) = entry.get("display_name").and_then(Value::as_str) {
        payload["display_name"] = json!(display_name);
    }
    if let Some(state) = entry.get("state").and_then(Value::as_str) {
        payload["runtime_state"] = json!(state);
    }
    if let Some(healthy) = entry.get("healthy").and_then(Value::as_bool) {
        payload["runtime_healthy"] = json!(healthy);
    }
    let Some(port) = entry
        .get("port")
        .and_then(Value::as_u64)
        .filter(|port| (1..=65535).contains(port))
    else {
        payload["reason"] = json!(format!(
            "运行实例无法确认：runtime 快照条目的端口不可用（{:?}）；绝不猜端口",
            entry.get("port")
        ));
        payload["evidence"] = json!(["runtime 快照端口不可用"]);
        return Ok(payload);
    };
    payload["port"] = json!(port);
    payload["base_url"] = json!(format!("http://127.0.0.1:{port}{PROTOCOL_PATH}"));
    payload["reason"] = json!(
        "运行实例无法确认鉴权要求：Rust 本机角色未读取运行实例命令行与实例级环境；端口与 Base URL 来自 runtime 快照，鉴权按未知上报"
    );
    payload["evidence"] = json!([
        "runtime 快照提供端口与运行状态",
        "Rust 本机角色未读取运行实例命令行（不猜测 --host/--port 或实例级环境）",
        format!("控制台进程环境{} CHAT2API_LOCAL_API_KEY",
                if std::env::var("CHAT2API_LOCAL_API_KEY").is_ok_and(|value| !value.trim().is_empty()) {
                    "已设置"
                } else {
                    "未设置"
                }),
        "Key 使用方式：chat2api.py 进程导入时读取 CHAT2API_LOCAL_API_KEY（启动后环境变更不生效）",
    ]);
    Ok(payload)
}
