//! Gateway entry point.
//!
//! Candidate mode is the default: the binary only binds an ephemeral loopback
//! port. Production mode must be selected explicitly with
//! `--mode production` and only binds 8000/8002/8003 on loopback. It prints the
//! actual address so a test (or an operator) can address the gateway, then serves
//! until the process is stopped. No `chat2api.py`, broker or display relay is
//! started.

use std::io::Write;
use std::process::ExitCode;
use std::sync::atomic::Ordering;

use um_gateway::config::{resolve_auth_wait_budget, AuthMode, BindMode, GatewayConfig, Role};
use um_gateway::GatewayServer;

fn main() -> ExitCode {
    let mut config = GatewayConfig::from_env();
    let mut role = None;
    let mut auth_wait_budget_cli = None;
    let mut args = std::env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--mode" => match args.next().as_deref().and_then(BindMode::parse) {
                Some(BindMode::Candidate) => config.candidate = true,
                Some(BindMode::Production) => config.candidate = false,
                None => return usage("--mode must be candidate or production"),
            },
            "--role" => match args.next().as_deref().and_then(Role::parse) {
                Some(value) => role = Some(value),
                None => return usage("--role must be local or cloud"),
            },
            "--auth-wait-budget-seconds" => match args
                .next()
                .and_then(|value| value.parse::<f64>().ok())
                .filter(|value| value.is_finite())
            {
                Some(value) => auth_wait_budget_cli = Some(value),
                None => return usage("--auth-wait-budget-seconds requires a finite number"),
            },
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
            "--token-file" => match args.next() {
                Some(value) => config.token_file = Some(value.into()),
                None => return usage("--token-file requires a path"),
            },
            "--auth-state-file" => match args.next() {
                Some(value) => config.auth_state_file = Some(value.into()),
                None => return usage("--auth-state-file requires a path"),
            },
            "--profile-dir" => match args.next() {
                Some(value) => config.profile_dir = Some(value.into()),
                None => return usage("--profile-dir requires a path"),
            },
            "--login-display-file" => match args.next() {
                Some(value) => config.login_display_file = Some(value.into()),
                None => return usage("--login-display-file requires a path"),
            },
            "--login-timeout" => match args.next().and_then(|value| value.parse::<u64>().ok()) {
                Some(value) => config.login_timeout = std::time::Duration::from_secs(value.max(1)),
                None => return usage("--login-timeout requires an integer"),
            },
            "--usage-log" => match args.next() {
                Some(value) => config.usage_log = Some(value.into()),
                None => return usage("--usage-log requires a path"),
            },
            "--service-log" => match args.next() {
                Some(value) => config.service_log = Some(value.into()),
                None => return usage("--service-log requires a path"),
            },
            "--effort-model" => match args.next() {
                Some(value) => config.effort_models.push(value),
                None => return usage("--effort-model requires a model id"),
            },
            "--instance-key-file" => match args.next() {
                Some(value) => config.instance_key_file = Some(value.into()),
                None => return usage("--instance-key-file requires a path"),
            },
            "--instance-pipe-prefix" => match args.next() {
                Some(value) => config.instance_pipe_prefix = Some(value),
                None => return usage("--instance-pipe-prefix requires a prefix"),
            },
            "--registration-file" => match args.next() {
                Some(value) => config.registration_file = Some(value.into()),
                None => return usage("--registration-file requires a path"),
            },
            "--no-browser" => config.browser_disabled = true,
            "--help" | "-h" => {
                println!(
                    "usage: um-gateway-candidate [--mode candidate|production] \
                     [--role local|cloud] --upstream-base-url URL [--host 127.0.0.1] \
                     [--port 0|8000|8003|8002] [--token TOKEN] \
                     [--share-config PATH] [--auth-mode projection|legacy] \
                     [--control-token-file PATH] [--token-file PATH] \
                     [--auth-state-file PATH] \
                     [--profile-dir PATH] [--login-display-file PATH] \
                     [--login-timeout SECONDS] [--no-browser] \
                     [--auth-wait-budget-seconds SECONDS] \
                     [--effort-model MODEL]\n\
                     modes: candidate (default) binds an ephemeral loopback port only; \
                     production must be selected explicitly and binds 7998, 7999, 8000, 8002 or 8003 \
                     on loopback only; --role defaults to local for 8000 and cloud otherwise"
                );
                return ExitCode::SUCCESS;
            }
            other => return usage(&format!("unknown argument: {other}")),
        }
    }
    if config.upstream_base_url.is_empty() {
        return usage("--upstream-base-url is required");
    }
    // Apply `--role` and `--port` only after the whole argv is parsed, so
    // `validate_bind_policy` sees the final pair regardless of flag order.
    config.role = role.unwrap_or_else(|| Role::for_port(config.bind_port));
    if let Some(value) = auth_wait_budget_cli {
        config.auth_wait_budget = Some(resolve_auth_wait_budget(Some(value), None, config.role));
    }
    let mut server = match GatewayServer::start(config) {
        Ok(server) => server,
        Err(error) => {
            eprintln!("um-gateway-candidate: {error}");
            return ExitCode::from(1);
        }
    };
    let addr = server.addr();
    println!("um-gateway listening on http://{addr}");
    let _ = std::io::stdout().flush();
    // `POST /admin/shutdown` asks for the same graceful stop a signal would:
    // the listener closes, the login browser and the instance channel stop.
    let shutdown_request = std::sync::Arc::clone(&server.state().shutdown_request);
    while !shutdown_request.load(Ordering::SeqCst) {
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    server.shutdown();
    ExitCode::SUCCESS
}

fn usage(message: &str) -> ExitCode {
    eprintln!("um-gateway-candidate: {message}");
    ExitCode::from(64)
}
