//! Gateway entry point.
//!
//! Candidate mode is the default: the binary only binds an ephemeral loopback
//! port. Production mode must be selected explicitly with
//! `--mode production` and only binds 8000/8002/8003 on loopback. It prints the
//! actual address so a test (or an operator) can address the gateway, then serves
//! until the process is stopped. No `chat2api.py`, broker or display relay is
//! started.

use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::Ordering;
use std::time::Duration;

use um_gateway::config::{resolve_auth_wait_budget, AuthMode, GatewayConfig, Role, RunMode};
use um_gateway::ingress::{
    remove_worker_port_file, start_worker, IngressConfig, IngressServer, BOOT_ID_ENV,
    DEFAULT_RECOVERY_QUEUE_MAX, DEFAULT_RECOVERY_QUEUE_MAX_BYTES, DEFAULT_RECOVERY_WAIT_SECONDS,
    DEFAULT_WORKER_PORT_FILE, DESIRED_STATE_ENV,
};
use um_gateway::GatewayServer;

fn main() -> ExitCode {
    let mut config = GatewayConfig::from_env();
    let mut role = None;
    let mut auth_wait_budget_cli = None;
    let mut run_mode = RunMode::Gateway;
    let mut worker_port_file = PathBuf::from(DEFAULT_WORKER_PORT_FILE);
    let mut desired_state_file = None;
    let mut recovery_wait_seconds = DEFAULT_RECOVERY_WAIT_SECONDS;
    let mut recovery_queue_max = DEFAULT_RECOVERY_QUEUE_MAX;
    let mut recovery_queue_max_bytes = DEFAULT_RECOVERY_QUEUE_MAX_BYTES;
    let mut boot_id = None;
    let mut port_cli = None;
    let mut args = std::env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--mode" => match args.next().as_deref() {
                Some("candidate") => {
                    config.candidate = true;
                    run_mode = RunMode::Gateway;
                }
                Some("production") => {
                    config.candidate = false;
                    run_mode = RunMode::Gateway;
                }
                Some("worker") => {
                    config.candidate = false;
                    run_mode = RunMode::Worker;
                }
                Some("ingress") => {
                    config.candidate = false;
                    run_mode = RunMode::Ingress;
                }
                Some(_) | None => {
                    return usage("--mode must be candidate, production, worker or ingress")
                }
            },
            "--worker-port-file" => match args.next() {
                Some(value) => worker_port_file = PathBuf::from(value),
                None => return usage("--worker-port-file requires a path"),
            },
            "--desired-state-file" => match args.next() {
                Some(value) => desired_state_file = Some(PathBuf::from(value)),
                None => return usage("--desired-state-file requires a path"),
            },
            "--recovery-wait-seconds" => {
                match args.next().and_then(|value| value.parse::<u64>().ok()) {
                    Some(value) => recovery_wait_seconds = value,
                    None => return usage("--recovery-wait-seconds requires an integer"),
                }
            }
            "--recovery-queue-max" => {
                match args.next().and_then(|value| value.parse::<usize>().ok()) {
                    Some(value) => recovery_queue_max = value,
                    None => return usage("--recovery-queue-max requires an integer"),
                }
            }
            "--recovery-queue-max-bytes" => {
                match args.next().and_then(|value| value.parse::<u64>().ok()) {
                    Some(value) => recovery_queue_max_bytes = value,
                    None => return usage("--recovery-queue-max-bytes requires an integer"),
                }
            }
            "--boot-id" => match args.next() {
                Some(value) => boot_id = Some(value),
                None => return usage("--boot-id requires a value"),
            },
            "--desktop-instance" => config.desktop_instance = true,
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
                Some(value) => {
                    config.bind_port = value;
                    port_cli = Some(value);
                }
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
                    "usage: um-gateway-candidate [--mode candidate|production|worker|ingress] \
                     [--role local|cloud] --upstream-base-url URL [--host 127.0.0.1] \
                     [--port 0|8000|8003|8002] [--token TOKEN] \
                     [--share-config PATH] [--auth-mode projection|legacy] \
                     [--control-token-file PATH] [--token-file PATH] \
                     [--auth-state-file PATH] \
                     [--profile-dir PATH] [--login-display-file PATH] \
                     [--login-timeout SECONDS] [--no-browser] \
                     [--auth-wait-budget-seconds SECONDS] \
                     [--effort-model MODEL] \
                     [--worker-port-file PATH] [--desired-state-file PATH] \
                     [--recovery-wait-seconds SECONDS] [--recovery-queue-max N] \
                     [--recovery-queue-max-bytes BYTES] [--boot-id ID]\n\
                     modes: candidate (default) binds an ephemeral loopback port only; \
                     production must be selected explicitly and binds 7998, 7999, 8000, 8002 or 8003 \
                     on loopback only; worker binds a loopback port (0 in production) and writes \
                     --worker-port-file; ingress binds 8000 and forwards to the worker; \
                     --role defaults to local for 8000 and cloud otherwise"
                );
                return ExitCode::SUCCESS;
            }
            other => return usage(&format!("unknown argument: {other}")),
        }
    }
    config.run_mode = run_mode;
    if run_mode == RunMode::Ingress {
        let desired_state_file = desired_state_file.or_else(|| {
            std::env::var(DESIRED_STATE_ENV)
                .ok()
                .filter(|value| !value.trim().is_empty())
                .map(PathBuf::from)
        });
        let boot_id = boot_id.or_else(|| {
            std::env::var(BOOT_ID_ENV)
                .ok()
                .filter(|value| !value.trim().is_empty())
        });
        let ingress_config = IngressConfig {
            bind_host: config.bind_host.clone(),
            bind_port: port_cli.unwrap_or(8000),
            worker_port_file,
            desired_state_file,
            boot_id,
            recovery_wait: Duration::from_secs(recovery_wait_seconds),
            queue_max: recovery_queue_max.max(1),
            queue_max_bytes: recovery_queue_max_bytes.max(1),
            control_token_file: config.control_token_file.clone(),
            service_log: config.service_log,
            max_request_bytes: config.max_request_bytes,
            ..IngressConfig::default()
        };
        let mut server = match IngressServer::start(ingress_config) {
            Ok(server) => server,
            Err(error) => {
                eprintln!("um-gateway-candidate: {error}");
                return ExitCode::from(1);
            }
        };
        let addr = server.addr();
        println!("um-gateway ingress listening on http://{addr}");
        let _ = std::io::stdout().flush();
        let shutdown_request = server.shutdown_request();
        while !shutdown_request.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(100));
        }
        server.shutdown();
        return ExitCode::SUCCESS;
    }
    if config.upstream_base_url.is_empty() {
        return usage("--upstream-base-url is required");
    }
    // Apply `--role` and `--port` only after the whole argv is parsed, so
    // `validate_bind_policy` sees the final pair regardless of flag order.
    config.role = role.unwrap_or_else(|| {
        if run_mode == RunMode::Worker || config.desktop_instance {
            Role::Local
        } else {
            Role::for_port(config.bind_port)
        }
    });
    if let Some(value) = auth_wait_budget_cli {
        config.auth_wait_budget = Some(resolve_auth_wait_budget(Some(value), None, config.role));
    }
    if run_mode == RunMode::Worker {
        let mut server = match start_worker(config, &worker_port_file) {
            Ok(server) => server,
            Err(error) => {
                eprintln!("um-gateway-candidate: {error}");
                return ExitCode::from(1);
            }
        };
        let addr = server.addr();
        println!("um-gateway worker listening on http://{addr}");
        let _ = std::io::stdout().flush();
        let shutdown_request = std::sync::Arc::clone(&server.state().shutdown_request);
        while !shutdown_request.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(100));
        }
        server.shutdown();
        remove_worker_port_file(&worker_port_file);
        return ExitCode::SUCCESS;
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
