//! Candidate console entry point.
//!
//! The binary only binds an ephemeral loopback port. It prints the actual
//! address so a test (or an operator) can address the candidate, then serves
//! until the process is stopped. No production port is bound, no SQLite/ledger
//! is written and the role is the cloud console role only.

use std::io::Write;
use std::process::ExitCode;

use um_console::config::{ConsoleConfig, EntryKind, Role};
use um_console::ConsoleServer;

fn main() -> ExitCode {
    let mut config = match ConsoleConfig::from_env() {
        Ok(config) => config,
        Err(error) => {
            eprintln!("um-console-candidate: {error}");
            return ExitCode::from(1);
        }
    };
    let mut args = std::env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--host" => match args.next() {
                Some(value) => config.bind_host = value,
                None => return usage("--host requires a value"),
            },
            "--bind-port" => match args.next().and_then(|value| value.parse::<u16>().ok()) {
                Some(value) => config.bind_port = value,
                None => return usage("--bind-port requires an integer"),
            },
            "--report-port" => match args.next().and_then(|value| value.parse::<u16>().ok()) {
                Some(value) => config.port = value,
                None => return usage("--report-port requires an integer"),
            },
            "--role" => match args.next().as_deref().and_then(Role::parse) {
                Some(value) => config.role = value,
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
                    "usage: um-console-candidate [--host 127.0.0.1] [--bind-port 0] \
                     [--report-port 8002] [--role cloud] [--state-file PATH] \
                     [--gateway-pid-file PATH] [--gateway-state-dir PATH] \
                     [--gateway-port 8003] [--gateway-entry NAME] \
                     [--gateway-entry-kind python|native] [--proc-root PATH] \
                     [--expected-uid N]"
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
