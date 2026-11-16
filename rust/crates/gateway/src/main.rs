//! Candidate gateway entry point.
//!
//! The binary only binds an ephemeral loopback port. It prints the actual
//! address so a test (or an operator) can address the candidate, then serves
//! until the process is stopped. No production port, `chat2api.py`, broker or
//! display relay is started.

use std::io::Write;
use std::process::ExitCode;

use um_gateway::config::{AuthMode, GatewayConfig};
use um_gateway::GatewayServer;

fn main() -> ExitCode {
    let mut config = GatewayConfig::from_env();
    let mut args = std::env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--host" => match args.next() {
                Some(value) => config.bind_host = value,
                None => return usage("--host requires a value"),
            },
            "--port" => match args.next().and_then(|value| value.parse::<u16>().ok()) {
                Some(value) => config.bind_port = value,
                None => return usage("--port requires an integer"),
            },
            "--upstream-base-url" => match args.next() {
                Some(value) => config.upstream_base_url = value,
                None => return usage("--upstream-base-url requires a value"),
            },
            "--token" => match args.next() {
                Some(value) => config.upstream_token = value,
                None => return usage("--token requires a value"),
            },
            "--share-config" => match args.next() {
                Some(value) => config.share_config = Some(value.into()),
                None => return usage("--share-config requires a path"),
            },
            "--auth-mode" => match args.next().as_deref().and_then(AuthMode::parse) {
                Some(value) => config.auth_mode = value,
                None => return usage("--auth-mode must be projection or legacy"),
            },
            "--control-token-file" => match args.next() {
                Some(value) => config.control_token_file = Some(value.into()),
                None => return usage("--control-token-file requires a path"),
            },
            "--no-browser" => config.browser_disabled = true,
            "--help" | "-h" => {
                println!(
                    "usage: um-gateway-candidate --upstream-base-url URL \
                     [--host 127.0.0.1] [--port 0] [--token TOKEN] \
                     [--share-config PATH] [--auth-mode projection|legacy] \
                     [--control-token-file PATH] [--no-browser]"
                );
                return ExitCode::SUCCESS;
            }
            other => return usage(&format!("unknown argument: {other}")),
        }
    }
    if config.upstream_base_url.is_empty() {
        return usage("--upstream-base-url is required");
    }
    let server = match GatewayServer::start(config) {
        Ok(server) => server,
        Err(error) => {
            eprintln!("um-gateway-candidate: {error}");
            return ExitCode::from(1);
        }
    };
    let addr = server.addr();
    println!("um-gateway listening on http://{addr}");
    let _ = std::io::stdout().flush();
    loop {
        std::thread::sleep(std::time::Duration::from_secs(3600));
    }
}

fn usage(message: &str) -> ExitCode {
    eprintln!("um-gateway-candidate: {message}");
    ExitCode::from(64)
}
