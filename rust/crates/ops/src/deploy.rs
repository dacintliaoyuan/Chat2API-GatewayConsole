//! Read-only parity for deployment assembly and the Python deploy CLI contract.
//!
//! The assembly check is pure text: it refuses a unit/env pair that lost the
//! security fence. Backup, restore, update and rollback stay Python/bash
//! tools; [`run_backup_restore`] and [`run_deploy_script`] only forward their
//! stdout/stderr and exit code without changing archives or permissions.

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::path::Path;
use std::process::Command;

use serde_json::{json, Map, Value};

pub const SHARE_SECRET_NAME: &str = "share-secret.key";
pub const SHARE_SECRET_KEY_ENV: &str = "CHAT2API_SHARE_SECRET_KEY_FILE";
pub const CONSOLE_REQUIRED_ENV: [&str; 2] =
    ["UM_CLOUD_SECURITY_STATE", "CHAT2API_SHARE_SECRET_KEY_FILE"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeploymentError {
    message: String,
}

impl DeploymentError {
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for DeploymentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for DeploymentError {}

#[derive(Debug, Clone)]
pub struct DeploymentPaths {
    pub current: String,
    pub etc: String,
    pub var: String,
    pub console: String,
}

impl Default for DeploymentPaths {
    fn default() -> Self {
        Self {
            current: "/opt/um-share/current".to_string(),
            etc: "/etc/um-share".to_string(),
            var: "/var/lib/um-share".to_string(),
            console: "/var/lib/um-share-console".to_string(),
        }
    }
}

pub fn env_assignments(text: &str) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    for line in text.lines() {
        let stripped = line.trim();
        if stripped.is_empty() || stripped.starts_with('#') || !stripped.contains('=') {
            continue;
        }
        let (key, value) = stripped.split_once('=').expect("assignment");
        let mut value = value.trim();
        if value.len() >= 2
            && (value.starts_with('\'') || value.starts_with('"'))
            && value.ends_with(value.chars().next().unwrap_or('\0'))
        {
            value = &value[1..value.len() - 1];
        }
        values.insert(key.trim().to_string(), value.to_string());
    }
    values
}

pub fn deployment_assembly_problems(
    role: &str,
    unit_text: &str,
    env_text: &str,
) -> Result<Vec<String>, DeploymentError> {
    let mut problems = Vec::new();
    let values = env_assignments(env_text);
    if role == "broker" {
        if !unit_text.contains("EnvironmentFile=") {
            problems.push("broker unit does not load an EnvironmentFile".to_string());
        }
        if !values
            .get("UM_SHARE_UID")
            .is_some_and(|value| !value.is_empty())
        {
            problems.push("broker.env does not set UM_SHARE_UID".to_string());
        }
    } else if role == "console" {
        if !unit_text.contains("EnvironmentFile=") {
            problems.push("console unit does not load an EnvironmentFile".to_string());
        }
        for key in CONSOLE_REQUIRED_ENV {
            if !values.get(key).is_some_and(|value| !value.is_empty()) {
                problems.push(format!(
                    "console.env does not set {key}; the console read face would report configured=false"
                ));
            }
        }
        if !values
            .get("UM_SHARE_UID")
            .is_some_and(|value| !value.is_empty())
        {
            problems.push("console.env does not set UM_SHARE_UID".to_string());
        }
    } else {
        problems.push(format!("unknown deployment role: '{role}'"));
    }
    Ok(problems)
}

pub fn check_deployment_assembly(
    role: &str,
    unit_path: &Path,
    env_path: &Path,
) -> Result<Value, DeploymentError> {
    let unit_text = fs::read_to_string(unit_path).map_err(|error| DeploymentError {
        message: format!("unit file unreadable: {}: {error}", unit_path.display()),
    })?;
    let env_text = fs::read_to_string(env_path).map_err(|error| DeploymentError {
        message: format!(
            "environment file unreadable: {}: {error}",
            env_path.display()
        ),
    })?;
    let problems = deployment_assembly_problems(role, &unit_text, &env_text)?;
    if !problems.is_empty() {
        return Err(DeploymentError {
            message: problems.join("; "),
        });
    }
    Ok(json!({
        "ok": true,
        "role": role,
        "unit": unit_path.display().to_string(),
        "env": env_path.display().to_string(),
    }))
}

pub fn deployment_paths(
    paths: &DeploymentPaths,
    env: &BTreeMap<String, String>,
) -> Result<Value, DeploymentError> {
    let share_secret_key = effective_share_secret_key(paths, env)?;
    let mut output = Map::new();
    output.insert("current".to_string(), json!(paths.current));
    output.insert("etc".to_string(), json!(paths.etc));
    output.insert("var".to_string(), json!(paths.var));
    output.insert("console".to_string(), json!(paths.console));
    output.insert("console_var".to_string(), json!(paths.console));
    output.insert(
        "store".to_string(),
        json!(format!("{}/share-store.db", paths.console)),
    );
    output.insert(
        "ledger".to_string(),
        json!(format!(
            "{}/share-backend/usage-share-backend.jsonl",
            paths.console
        )),
    );
    output.insert(
        "key_file".to_string(),
        json!(format!("{}/backup.key", paths.etc)),
    );
    output.insert(
        "remote_dir".to_string(),
        json!(format!("{}/backup-remote", paths.var)),
    );
    output.insert(
        "state".to_string(),
        json!(format!("{}/backup-freshness.json", paths.var)),
    );
    output.insert(
        "spool".to_string(),
        json!(format!("{}/backup-spool", paths.var)),
    );
    output.insert(
        "budget_db".to_string(),
        json!(format!("{}/budget.db", paths.var)),
    );
    output.insert(
        "config".to_string(),
        json!(format!("{}/share-management.json", paths.etc)),
    );
    output.insert(
        "security_state".to_string(),
        json!(format!("{}/security-state.json", paths.var)),
    );
    output.insert("share_secret_key".to_string(), json!(share_secret_key));
    Ok(Value::Object(output))
}

fn effective_share_secret_key(
    paths: &DeploymentPaths,
    env: &BTreeMap<String, String>,
) -> Result<String, DeploymentError> {
    if let Some(override_value) = env
        .get(SHARE_SECRET_KEY_ENV)
        .map(|value| value.trim())
        .filter(|value| !value.is_empty())
    {
        return Ok(override_value.to_string());
    }
    let env_path = Path::new(&paths.etc).join("console.env");
    let text = match fs::read_to_string(&env_path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(error) => {
            return Err(DeploymentError {
                message: format!(
                    "console environment file unreadable: {}: {error}",
                    env_path.display()
                ),
            });
        }
    };
    let configured = env_assignments(&text)
        .get(SHARE_SECRET_KEY_ENV)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    if let Some(configured) = configured {
        return Ok(configured);
    }
    Ok(format!("{}/data/{SHARE_SECRET_NAME}", paths.current))
}

/// Forward one Python `backup_restore.py` invocation unchanged.
pub fn run_backup_restore(python: &str, script: &Path, args: &[String]) -> i32 {
    run(Command::new(python).arg(script).args(args))
}

/// Forward one `update.sh`/`rollback.sh` invocation unchanged.
pub fn run_deploy_script(script: &Path, args: &[String]) -> i32 {
    let script = script
        .to_string_lossy()
        .replace(std::path::MAIN_SEPARATOR, "/");
    run(Command::new(bash_program()).arg(script).args(args))
}

fn bash_program() -> String {
    if let Ok(program) = std::env::var("UM_OPS_BASH") {
        if !program.trim().is_empty() {
            return program;
        }
    }
    #[cfg(windows)]
    {
        for candidate in [
            "C:/Program Files/Git/bin/bash.exe",
            "C:/Program Files/Git/usr/bin/bash.exe",
            "C:/Program Files (x86)/Git/bin/bash.exe",
        ] {
            if std::path::Path::new(candidate).is_file() {
                return candidate.to_string();
            }
        }
    }
    "bash".to_string()
}

fn run(command: &mut Command) -> i32 {
    match command.status() {
        Ok(status) => status.code().unwrap_or(1),
        Err(error) => {
            eprintln!("um-ops: cannot run child tool: {error}");
            127
        }
    }
}
