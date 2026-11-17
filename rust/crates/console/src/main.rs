//! Console entry point.
//!
//! Candidate mode is the default: the binary only binds an ephemeral loopback
//! port. Production mode must be selected explicitly with `--mode production` and
//! only binds `127.0.0.1:8002`. The binary prints the actual address so a test
//! (or an operator) can address the console, then serves until the process is
//! stopped. No SQLite/ledger is written by a read face and the role is the cloud
//! console role only.

use std::io::Write;
use std::process::ExitCode;

use um_console::config::{ConsoleConfig, EntryKind, Role};
use um_console::ConsoleServer;
use um_gateway::config::BindMode;

fn main() -> ExitCode {
    let mut config = match ConsoleConfig::from_env() {
        Ok(config) => config,
        Err(error) => {
            eprintln!("um-console-candidate: {error}");
            return ExitCode::from(1);
        }
    };
    let mut args = std::env::args().skip(1);
    let mut report_port_set = false;
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--mode" => match args.next().as_deref().and_then(BindMode::parse) {
                Some(BindMode::Candidate) => config.candidate = true,
                Some(BindMode::Production) => config.candidate = false,
                None => return usage("--mode must be candidate or production"),
            },
            "--host" => match args.next() {
                Some(value) => config.bind_host = value,
                None => return usage("--host requires a value"),
            },
            "--bind-port" => match args.next().and_then(|value| value.parse::<u16>().ok()) {
                Some(value) => config.bind_port = value,
                None => return usage("--bind-port requires an integer"),
            },
            "--report-port" => match args.next().and_then(|value| value.parse::<u16>().ok()) {
                Some(value) => {
                    config.port = value;
                    report_port_set = true;
                }
                None => return usage("--report-port requires an integer"),
            },
            "--role" => match args.next().as_deref().and_then(Role::parse) {
                Some(value) => {
                    config.role = value;
                    if !report_port_set {
                        config.port = value.default_port();
                    }
                }
                None => return usage("--role must be local or cloud"),
            },
            "--state-file" => match args.next() {
                Some(value) => config.console_state_file = Some(value.into()),
                None => return usage("--state-file requires a path"),
            },
            "--gateway-pid-file" => match args.next() {
                Some(value) => config.gateway_pid_file = value.into(),
                None => return usage("--gateway-pid-file requires a path"),
            },
            "--gateway-state-dir" => match args.next() {
                Some(value) => config.gateway_state_dir = value.into(),
                None => return usage("--gateway-state-dir requires a path"),
            },
            "--gateway-port" => match args.next().and_then(|value| value.parse::<u16>().ok()) {
                Some(value) => config.gateway_port = value,
                None => return usage("--gateway-port requires an integer"),
            },
            "--gateway-entry" => match args.next() {
                Some(value) => config.gateway_entry = value,
                None => return usage("--gateway-entry requires a value"),
            },
            "--gateway-entry-kind" => match args.next().as_deref().and_then(EntryKind::parse) {
                Some(value) => config.gateway_entry_kind = value,
                None => return usage("--gateway-entry-kind must be python or native"),
            },
            "--proc-root" => match args.next() {
                Some(value) => config.proc_root = value.into(),
                None => return usage("--proc-root requires a path"),
            },
            "--expected-uid" => match args.next().and_then(|value| value.parse::<u32>().ok()) {
                Some(value) => config.expected_uid = Some(value),
                None => return usage("--expected-uid requires an integer"),
            },
            "--help" | "-h" => {
                println!(
                    "usage: um-console-candidate [--mode candidate|production] \
                     [--host 127.0.0.1] [--bind-port 0|8001|8002] \
                     [--report-port 8001|8002] [--role local|cloud] [--state-file PATH] \
                     [--gateway-pid-file PATH] [--gateway-state-dir PATH] \
                     [--gateway-port 8003] [--gateway-entry NAME] \
                     [--gateway-entry-kind python|native] [--proc-root PATH] \
                     [--expected-uid N]\n\
                     modes: candidate (default) binds an ephemeral loopback port only; \
                     production must be selected explicitly and binds 8001 for the local \
                     role or 8002 for the cloud role on loopback only"
                );
                return ExitCode::SUCCESS;
            }
            other => return usage(&format!("unknown argument: {other}")),
        }
    }
    let server = match ConsoleServer::start(config) {
        Ok(server) => server,
        Err(error) => {
            eprintln!("um-console-candidate: {error}");
            return ExitCode::from(1);
        }
    };
    let addr = server.addr();
    println!("um-console listening on http://{addr}");
    let _ = std::io::stdout().flush();
    loop {
        std::thread::sleep(std::time::Duration::from_secs(3600));
    }
}

fn usage(message: &str) -> ExitCode {
    eprintln!("um-console-candidate: {message}");
    ExitCode::from(64)
}
