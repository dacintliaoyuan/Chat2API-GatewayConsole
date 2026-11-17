//! The local console page host (`usagesrv.py` root and static fallthrough).
//!
//! The local role hosts the repository's own `web/dist` build output. This module
//! owns the root entry page, the build-identity gate and the dist static
//! fallthrough; the cloud role keeps its existing unknown-path answer.

use std::path::Path;

use serde_json::{json, Value};

use um_gateway::http::Response;

use crate::config::Role;
use crate::error::ConsoleError;

pub const INDEX_FILE_NAME: &str = "index.html";
pub const BUILD_IDENTITY_FILE_NAME: &str = "candidate-build.json";
const IDENTITY_LABEL: &str = "web/dist/candidate-build.json";

/// `usagesrv.py::_console_build_identity_error`: `None` means the hosted tree
/// names this process's role, is a production build and carries a source
/// identity. The file is read on every request so a rebuild is seen without a
/// restart.
pub fn build_identity_error(dist_dir: &Path, role: Role, port: u16) -> Option<String> {
    let expected = role.as_str();
    let raw = match std::fs::read(dist_dir.join(BUILD_IDENTITY_FILE_NAME)) {
        Ok(raw) => raw,
        Err(_) => {
            return Some(format!(
                "{IDENTITY_LABEL} 缺失：无法确认托管页面属于 {expected} 角色"
            ));
        }
    };
    let identity: Value = match serde_json::from_slice(&raw) {
        Ok(identity) => identity,
        Err(_) => {
            return Some(format!(
                "{IDENTITY_LABEL} 不是有效 JSON：无法确认托管页面属于 {expected} 角色"
            ));
        }
    };
    let Some(object) = identity.as_object() else {
        return Some(format!(
            "{IDENTITY_LABEL} 不是构建身份对象：无法确认托管页面属于 {expected} 角色"
        ));
    };
    let role_value = object.get("role");
    let identity_role = match role_value.and_then(Value::as_str) {
        Some("local") => Some(Role::Local),
        Some("cloud") => Some(Role::Cloud),
        _ => None,
    };
    let Some(identity_role) = identity_role else {
        return Some(format!(
            "{IDENTITY_LABEL} 的 role 无效（实际={}）：无法确认托管页面属于 {expected} 角色",
            python_repr(role_value)
        ));
    };
    if identity_role != role {
        return Some(format!(
            "托管页面是 {} 角色的构建，本进程是 {expected} 角色：拒绝把 {} 控制台页面托管到 {port} 端口",
            identity_role.as_str(),
            identity_role.as_str()
        ));
    }
    let line = object.get("line");
    if line.and_then(Value::as_str) != Some("production") {
        return Some(format!(
            "{IDENTITY_LABEL} 的 line={}：候选/非生产构建不能托管到 {port} 端口",
            python_repr(line)
        ));
    }
    let source_hash = object.get("sourceHash");
    let valid_source_hash = source_hash
        .and_then(Value::as_str)
        .map(|hash| {
            hash.len() == 64
                && hash
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .unwrap_or(false);
    if !valid_source_hash {
        return Some(format!(
            "{IDENTITY_LABEL} 缺少有效的 sourceHash（实际={}）：无法确认托管页面来自生产构建",
            python_repr(source_hash)
        ));
    }
    None
}

/// Python's `%r` for the JSON values this identity file can carry.
fn python_repr(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::String(text)) => {
            format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'"))
        }
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Bool(false)) => "False".to_string(),
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::Array(_)) => "[...]".to_string(),
        Some(Value::Object(_)) => "{...}".to_string(),
    }
}

/// `usagesrv.py::do_GET` root branch: verify the build identity, then serve
/// `web/dist/index.html` with `text/html; charset=utf-8` and
/// `Cache-Control: no-store`.
pub fn serve_index(dist_dir: &Path, role: Role, port: u16) -> Response {
    if let Some(reason) = build_identity_error(dist_dir, role, port) {
        return ConsoleError::new(500, json!({"error": reason})).response();
    }
    match std::fs::read(dist_dir.join(INDEX_FILE_NAME)) {
        Ok(body) => Response::new(200, body)
            .with_header("Content-Type", "text/html; charset=utf-8")
            .with_header("Cache-Control", "no-store"),
        Err(_) => ConsoleError::unknown_path().response(),
    }
}

/// `usagesrv.py::_serve_console_static`: serve one regular file under the dist
/// root with the Python MIME map and `Cache-Control: no-store`.
pub fn serve_static(dist_dir: &Path, path: &str) -> Response {
    let _ = (dist_dir, path);
    ConsoleError::new(404, json!({"error": "unknown path"})).response()
}
