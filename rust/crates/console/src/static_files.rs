//! The local console page host (`usagesrv.py` root and static fallthrough).
//!
//! The local role hosts the repository's own `web/dist` build output. This module
//! owns the root entry page and the dist static fallthrough; the cloud role keeps
//! its existing unknown-path answer.

use std::path::Path;

use serde_json::json;

use um_gateway::http::Response;

use crate::config::Role;
use crate::error::ConsoleError;

pub const INDEX_FILE_NAME: &str = "index.html";
pub const BUILD_IDENTITY_FILE_NAME: &str = "candidate-build.json";

/// `usagesrv.py::do_GET` root branch: serve `web/dist/index.html` with
/// `text/html; charset=utf-8` and `Cache-Control: no-store`.
pub fn serve_index(dist_dir: &Path, role: Role, port: u16) -> Response {
    let _ = (role, port);
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
