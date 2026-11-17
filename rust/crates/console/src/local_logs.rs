//! Local gateway run-log read faces (`console/log_reader.py`).
//!
//! The source registry is server-side only: the client sends source/category
//! ids and never a path. `log-sources` returns the registry with paths
//! reduced to file names; `log-entries`/`log-overview` read bounded windows
//! from the registered targets and keep missing/unconfigured/failed targets
//! distinct from an empty log.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::config::ConsoleConfig;

pub const MAX_TAIL_BYTES: u64 = 262_144;
pub const MAX_ROWS: usize = 400;
pub const MAX_LINE_BYTES: usize = 8192;
pub const MAX_QUERY_CHARS: usize = 120;
pub const SCAN_SECONDS: f64 = 3.0;
pub const MAX_ROTATION_INDEX: u32 = 5;
pub const MAX_OVERVIEW_ROWS: usize = 1200;
pub const SHARE_RUN_ROOT: &str = r"C:\ProgramData\UMShareRun";

const CONSOLE_NOTE: &str = "8001 宿主不落盘行日志（start_usage_service.bat 无重定向，usagesrv 的输出只到隐藏窗口）；控制台自身的操作记录在「操作记录」页签（data/console-state.sqlite3），此处不伪造日志文件。";

#[derive(Debug, Clone)]
pub struct LogPaths {
    pub service_log: PathBuf,
    pub ledger: PathBuf,
    pub watch_dir: Option<PathBuf>,
    pub share_backend_dir: Option<PathBuf>,
}

impl LogPaths {
    pub fn from_config(config: &ConsoleConfig) -> Self {
        let share_backend_dir = match &config.share_backend_dir {
            Some(dir) if dir.is_absolute() => Some(dir.clone()),
            Some(_) => None,
            None => local_app_data()
                .map(|root| root.join("SelfUseAI").join("ai-gateway").join("share-backend")),
        };
        let watch_dir = match &config.watch_dir {
            Some(dir) if dir.is_absolute() => Some(dir.clone()),
            Some(_) => None,
            None => local_app_data()
                .map(|root| root.join("SelfUseAI").join("ai-gateway").join("watch")),
        };
        Self {
            service_log: config.service_log.clone(),
            ledger: config.usage_log.clone(),
            watch_dir,
            share_backend_dir,
        }
    }
}

fn local_app_data() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

#[derive(Debug, Clone)]
pub struct LogCategory {
    pub category: &'static str,
    pub display: &'static str,
    pub path: Option<PathBuf>,
    pub rotating: bool,
    pub rotation_index: Option<u32>,
    pub level_filter: bool,
    pub whole_document: bool,
}

#[derive(Debug, Clone)]
pub struct LogSource {
    pub source: &'static str,
    pub display: &'static str,
    pub note: Option<&'static str>,
    pub categories: Vec<LogCategory>,
}

fn category(
    category: &'static str,
    display: &'static str,
    path: Option<PathBuf>,
    rotating: bool,
    rotation_index: Option<u32>,
    level_filter: bool,
    whole_document: bool,
) -> LogCategory {
    LogCategory {
        category,
        display,
        path,
        rotating,
        rotation_index,
        level_filter,
        whole_document,
    }
}

pub fn build_registry(paths: &LogPaths) -> Vec<LogSource> {
    let chat2api_dir = paths
        .service_log
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .to_path_buf();
    let share = paths.share_backend_dir.as_ref();
    let share_run = Path::new(SHARE_RUN_ROOT);
    vec![
        LogSource {
            source: "chat2api",
            display: "chat2api 主链（8000）",
            note: None,
            categories: vec![
                category(
                    "service",
                    "运行日志 chat2api.log",
                    Some(paths.service_log.clone()),
                    true,
                    Some(MAX_ROTATION_INDEX),
                    true,
                    false,
                ),
                category(
                    "ledger",
                    "调用账本 usage-chat2api.jsonl",
                    Some(paths.ledger.clone()),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "console",
                    "隐藏启动输出 chat2api.console.log（含启动/退出记录）",
                    Some(chat2api_dir.join("chat2api.console.log")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "console_prev",
                    "上一次启动输出 chat2api.console.prev.log",
                    Some(chat2api_dir.join("chat2api.console.prev.log")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "watch",
                    "守护日志 chat2api-watch.log",
                    paths
                        .watch_dir
                        .as_ref()
                        .map(|dir| dir.join("chat2api-watch.log")),
                    true,
                    Some(1),
                    false,
                    false,
                ),
            ],
        },
        LogSource {
            source: "console",
            display: "用量统计服务 8001（本控制台宿主）",
            note: Some(CONSOLE_NOTE),
            categories: Vec::new(),
        },
        LogSource {
            source: "share",
            display: "分享链 8100（合并进程，非 8000 主链）",
            note: None,
            categories: vec![
                category(
                    "service",
                    "运行日志 share-backend.log",
                    share.map(|root| root.join("share-backend.log")),
                    true,
                    Some(MAX_ROTATION_INDEX),
                    true,
                    false,
                ),
                category(
                    "ledger",
                    "调用账本 usage-share-backend.jsonl",
                    share.map(|root| root.join("usage-share-backend.jsonl")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "gateway",
                    "8100 任务输出 raw.txt",
                    Some(share_run.join("run").join("live").join("raw.txt")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "faults",
                    "8100 故障记录 share-faults.jsonl",
                    Some(share_run.join("run").join("live").join("share-faults.jsonl")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "broker",
                    "broker 启停记录 lifecycle.json",
                    Some(share_run.join("broker").join("lifecycle.json")),
                    false,
                    None,
                    false,
                    true,
                ),
            ],
        },
    ]
}

fn file_name(path: &Path) -> Option<String> {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| !name.is_empty())
}

fn category_json(category: &LogCategory) -> Value {
    let mut object = json!({
        "category": category.category,
        "display": category.display,
        "rotating": category.rotating,
        "name": category.path.as_deref().and_then(file_name),
    });
    let map = object.as_object_mut().expect("category object");
    if let Some(index) = category.rotation_index {
        map.insert("rotation_index".to_string(), json!(index));
    }
    if category.level_filter {
        map.insert("level_filter".to_string(), json!(true));
    }
    if category.whole_document {
        map.insert("whole_document".to_string(), json!(true));
    }
    object
}

pub fn registry_payload(paths: &LogPaths) -> Value {
    let sources: Vec<Value> = build_registry(paths)
        .iter()
        .map(|source| {
            let mut object = json!({
                "source": source.source,
                "display": source.display,
                "categories": source.categories.iter().map(category_json).collect::<Vec<_>>(),
            });
            if let Some(note) = source.note {
                object["note"] = json!(note);
            }
            object
        })
        .collect();
    json!({"ok": true, "sources": sources})
}
