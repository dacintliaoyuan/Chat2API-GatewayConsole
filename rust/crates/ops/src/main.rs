//! `um-ops` candidate CLI.
//!
//! Ledger reads, ingress validation and DNS/TLS aggregation are implemented in
//! Rust and consume the frozen Python JSON contracts. Backup/restore, public probe,
//! deployment assembly and update/rollback stay the Python/bash tools: this binary
//! only forwards their arguments, stdout/stderr and exit codes.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::ExitCode;

use serde_json::Value;
use um_ops::deploy::{run_backup_restore, run_deploy_script, DeploymentPaths};
use um_ops::ingress::check_ingress;
use um_ops::ledger::{read_share_requests, read_share_usage, Clock, RequestOptions, UsageOptions};
use um_ops::SystemClock;

const USAGE: &str = "usage: um-ops [--python PATH] [--backup-restore PATH] \
[--public-probe PATH] [--update-script PATH] [--rollback-script PATH] \
<ledger|cloudflare|deploy|backup|restore|freshness|retry-pending> ...";

struct Options {
    python: String,
    backup_restore: PathBuf,
    public_probe: PathBuf,
    update_script: PathBuf,
    rollback_script: PathBuf,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            python: "python".to_string(),
            backup_restore: PathBuf::from("um-cloud-deploy/backup_restore.py"),
            public_probe: PathBuf::from("console/public_probe.py"),
            update_script: PathBuf::from("um-cloud-deploy/update.sh"),
            rollback_script: PathBuf::from("um-cloud-deploy/rollback.sh"),
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    ExitCode::from(run(&args) as u8)
}

fn run(args: &[String]) -> i32 {
    let mut options = Options::default();
    let mut index = 0;
    while index < args.len() {
        let Some(option) = args[index].strip_prefix("--") else {
            break;
        };
        let value = args.get(index + 1);
        match option {
            "python" => match value {
                Some(value) => options.python = value.clone(),
                None => {
                    eprintln!("um-ops: --python requires a value");
                    return 64;
                }
            },
            "backup-restore" => match value {
                Some(value) => options.backup_restore = PathBuf::from(value),
                None => {
                    eprintln!("um-ops: --backup-restore requires a value");
                    return 64;
                }
            },
            "public-probe" => match value {
                Some(value) => options.public_probe = PathBuf::from(value),
                None => {
                    eprintln!("um-ops: --public-probe requires a value");
                    return 64;
                }
            },
            "update-script" => match value {
                Some(value) => options.update_script = PathBuf::from(value),
                None => {
                    eprintln!("um-ops: --update-script requires a value");
                    return 64;
                }
            },
            "rollback-script" => match value {
                Some(value) => options.rollback_script = PathBuf::from(value),
                None => {
                    eprintln!("um-ops: --rollback-script requires a value");
                    return 64;
                }
            },
            _ => break,
        }
        index += 2;
    }
    if index >= args.len() {
        eprintln!("{USAGE}");
        return 64;
    }
    let command = args[index].as_str();
    let rest = &args[index + 1..];
    match command {
        "ledger" => run_ledger(rest),
        "cloudflare" => run_cloudflare(rest, &options),
        "deploy" => run_deploy(rest, &options),
        "backup" | "restore" => run_backup_restore(&options.python, &options.backup_restore, rest),
        "freshness" => {
            let mut forwarded = vec!["--print-freshness".to_string()];
            forwarded.extend(rest.iter().cloned());
            run_backup_restore(&options.python, &options.backup_restore, &forwarded)
        }
        "retry-pending" => {
            let mut forwarded = vec!["--retry-pending".to_string()];
            forwarded.extend(rest.iter().cloned());
            run_backup_restore(&options.python, &options.backup_restore, &forwarded)
        }
        "-h" | "--help" => {
            println!("{USAGE}");
            0
        }
        _ => {
            eprintln!("um-ops: unknown command: {command}");
            eprintln!("{USAGE}");
            64
        }
    }
}

fn run_ledger(args: &[String]) -> i32 {
    let Some(mode) = args.first().map(String::as_str) else {
        eprintln!("{USAGE}");
        return 64;
    };
    let mut ledger = None;
    let mut options = UsageOptions::default();
    let mut request_options = RequestOptions::default();
    let mut now = None;
    let mut index = 1;
    while index < args.len() {
        let Some(name) = args[index].strip_prefix("--") else {
            eprintln!("um-ops: unknown ledger argument: {}", args[index]);
            return 64;
        };
        let Some(value) = args.get(index + 1) else {
            eprintln!("um-ops: --{name} requires a value");
            return 64;
        };
        match name {
            "ledger" => ledger = Some(PathBuf::from(value)),
            "limit" => match value.parse::<i64>() {
                Ok(value) => {
                    options.limit = value;
                    request_options.limit = value;
                }
                Err(_) => {
                    eprintln!("um-ops: --limit must be an integer");
                    return 64;
                }
            },
            "window" => {
                options.window = Some(value.clone());
                request_options.window = Some(value.clone());
            }
            "key" => {
                options.key_id = Some(value.clone());
                request_options.key_id = Some(value.clone());
            }
            "model" => {
                options.model = Some(value.clone());
                request_options.model = Some(value.clone());
            }
            "result" => {
                options.result = Some(value.clone());
                request_options.result = Some(value.clone());
            }
            "cursor" => request_options.cursor = Some(value.clone()),
            "now" => match value.parse::<f64>() {
                Ok(value) => now = Some(value),
                Err(_) => {
                    eprintln!("um-ops: --now must be seconds");
                    return 64;
                }
            },
            _ => {
                eprintln!("um-ops: unknown ledger argument: --{name}");
                return 64;
            }
        }
        index += 2;
    }
    let Some(ledger) = ledger else {
        eprintln!("um-ops: --ledger is required");
        return 64;
    };
    let clock = OverrideClock {
        now_ms: now.map_or_else(|| SystemClock.now_ms(), |value| value * 1000.0),
        inner: SystemClock,
    };
    let result = match mode {
        "summary" => read_share_usage(&ledger, &options, &clock),
        "requests" => read_share_requests(&ledger, &request_options, &clock),
        _ => {
            eprintln!("um-ops: unknown ledger mode: {mode}");
            return 64;
        }
    };
    match result {
        Ok(value) => print_json(&value),
        Err(error) => {
            eprintln!("um-ops: {error}");
            1
        }
    }
}

struct OverrideClock {
    now_ms: f64,
    inner: SystemClock,
}

impl Clock for OverrideClock {
    fn now_ms(&self) -> f64 {
        self.now_ms
    }

    fn utc_iso_seconds(&self) -> String {
        self.inner.utc_iso_seconds()
    }

    fn monotonic_ms(&self) -> f64 {
        self.inner.monotonic_ms()
    }
}

fn run_cloudflare(args: &[String], options: &Options) -> i32 {
    let Some(mode) = args.first().map(String::as_str) else {
        eprintln!("{USAGE}");
        return 64;
    };
    match mode {
        "ingress" => {
            let mut config = None;
            let mut expect_host = um_ops::ingress::DEFAULT_HOST.to_string();
            let mut index = 1;
            while index < args.len() {
                match args[index].as_str() {
                    "--config" if index + 1 < args.len() => {
                        config = Some(PathBuf::from(&args[index + 1]));
                        index += 2;
                    }
                    "--expect-host" if index + 1 < args.len() => {
                        expect_host = args[index + 1].clone();
                        index += 2;
                    }
                    unknown => {
                        eprintln!("um-ops: unknown ingress argument: {unknown}");
                        return 64;
                    }
                }
            }
            let Some(config) = config else {
                eprintln!("um-ops: --config is required");
                return 64;
            };
            let result = check_ingress(&config, &expect_host);
            let code = if result["ok"].as_bool() == Some(true) {
                0
            } else {
                1
            };
            print_json(&result);
            code
        }
        "probe" => {
            let mut forwarded = vec!["--check-connection".to_string()];
            forwarded.extend(args[1..].iter().cloned());
            run_backup_restore(&options.python, &options.public_probe, &forwarded)
        }
        unknown => {
            eprintln!("um-ops: unknown cloudflare mode: {unknown}");
            64
        }
    }
}

fn run_deploy(args: &[String], options: &Options) -> i32 {
    let Some(mode) = args.first().map(String::as_str) else {
        eprintln!("{USAGE}");
        return 64;
    };
    match mode {
        "assembly" => {
            let mut forwarded = vec!["--check-deployment-assembly".to_string()];
            forwarded.extend(args[1..].iter().cloned());
            run_backup_restore(&options.python, &options.backup_restore, &forwarded)
        }
        "update" => run_deploy_script(&options.update_script, &command_arguments(&args[1..])),
        "rollback" => run_deploy_script(&options.rollback_script, &command_arguments(&args[1..])),
        "paths" => {
            let env: BTreeMap<String, String> = std::env::vars().collect();
            match um_ops::deploy::deployment_paths(&DeploymentPaths::default(), &env) {
                Ok(value) => print_json(&value),
                Err(error) => {
                    eprintln!("um-ops: {error}");
                    1
                }
            }
        }
        unknown => {
            eprintln!("um-ops: unknown deploy mode: {unknown}");
            64
        }
    }
}

fn command_arguments(args: &[String]) -> Vec<String> {
    match args.first().map(String::as_str) {
        Some("--") => args[1..].to_vec(),
        _ => args.to_vec(),
    }
}

fn print_json(value: &Value) -> i32 {
    match serde_json::to_string(value) {
        Ok(text) => {
            println!("{text}");
            0
        }
        Err(error) => {
            eprintln!("um-ops: cannot serialize JSON: {error}");
            70
        }
    }
}
