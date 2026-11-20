use std::path::Path;

use um_desktop::account::Credentials;
use um_desktop::config::DEFAULT_MODEL_API_PORT;
use um_desktop::gateway::{
    ensure_control_token, gateway_args, gateway_env, resolve_gateway_binary, BinaryError,
    GatewayProgram, InstancePaths, DEFAULT_INSTANCE_PORT, GOVERNED_PORTS, TEST_INSTANCE_PORT,
    UPSTREAM_BASE_URL,
};

mod common;
use common::TempDir;

#[test]
fn default_instance_port_is_7999_and_test_instance_is_7998() {
    assert_eq!(DEFAULT_INSTANCE_PORT, 7999);
    assert_eq!(DEFAULT_INSTANCE_PORT, DEFAULT_MODEL_API_PORT);
    assert_eq!(TEST_INSTANCE_PORT, 7998);
    assert_eq!(GOVERNED_PORTS, [8000, 8001, 8002, 8003]);
    assert_eq!(UPSTREAM_BASE_URL, "https://chat.genai.um.edu.mo");
}

#[test]
fn instance_paths_live_under_the_app_data_directory() {
    let root = Path::new(r"C:\Users\someone\AppData\Local\Chat2API");
    let paths = InstancePaths::under(root);
    assert_eq!(paths.instance_dir, root.join("instance"));
    assert_eq!(
        paths.control_token,
        root.join("instance").join("admin-control-token")
    );
    assert_eq!(paths.token, root.join("instance").join("token.json"));
    assert_eq!(paths.profile_dir, root.join("instance").join("profile"));
    assert_eq!(
        paths.auth_state,
        root.join("instance").join("auth-state.json")
    );
    assert_eq!(
        paths.login_history,
        root.join("instance").join("login-history.jsonl")
    );
    assert_eq!(paths.usage_log, root.join("instance").join("usage.jsonl"));
    assert_eq!(paths.service_log, root.join("instance").join("gateway.log"));
    assert_eq!(
        paths.upstream_faults,
        root.join("instance").join("upstream-faults.json")
    );
    assert_eq!(
        paths.stderr_log,
        root.join("instance").join("gateway-stderr.log")
    );
    assert_eq!(paths.supervision_log, root.join("logs").join("desktop.log"));
    assert_eq!(paths.base_url(7999), "http://127.0.0.1:7999");
}

#[test]
fn control_token_is_generated_once_and_stays_redacted() {
    let dir = TempDir::new("gateway-token");
    let path = dir.path().join("instance").join("admin-control-token");
    let first = ensure_control_token(&path).expect("generate token");
    let raw = std::fs::read_to_string(&path).expect("read token file");
    assert_eq!(raw.trim(), first.expose());
    assert_eq!(first.expose().len(), 64);
    assert!(first.expose().bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert!(!format!("{first:?}").contains(first.expose()));
    let second = ensure_control_token(&path).expect("reuse token");
    assert_eq!(first.expose(), second.expose());
}

#[test]
fn empty_or_malformed_control_tokens_are_rejected() {
    let dir = TempDir::new("gateway-token-bad");
    let path = dir.path().join("instance").join("admin-control-token");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, "  \n").unwrap();
    let error = ensure_control_token(&path).expect_err("empty token");
    assert!(error.contains("为空"), "{error}");
    std::fs::write(&path, "not-a-token").unwrap();
    let error = ensure_control_token(&path).expect_err("malformed token");
    assert!(error.contains("格式"), "{error}");
}

#[test]
fn gateway_args_pin_the_instance_cli_contract() {
    let root = Path::new(r"C:\Users\someone\AppData\Local\Chat2API");
    let paths = InstancePaths::under(root);
    let args = gateway_args(7999, &paths);
    let rendered: Vec<String> = args
        .iter()
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    assert_eq!(
        rendered,
        vec![
            "--mode".to_string(),
            "production".to_string(),
            "--port".to_string(),
            "7999".to_string(),
            "--role".to_string(),
            "local".to_string(),
            "--upstream-base-url".to_string(),
            "https://chat.genai.um.edu.mo".to_string(),
            "--control-token-file".to_string(),
            paths.control_token.to_string_lossy().into_owned(),
            "--usage-log".to_string(),
            paths.usage_log.to_string_lossy().into_owned(),
            "--service-log".to_string(),
            paths.service_log.to_string_lossy().into_owned(),
            "--token-file".to_string(),
            paths.token.to_string_lossy().into_owned(),
            "--profile-dir".to_string(),
            paths.profile_dir.to_string_lossy().into_owned(),
            "--auth-state-file".to_string(),
            paths.auth_state.to_string_lossy().into_owned(),
        ]
    );
    assert!(
        !rendered.iter().any(|arg| arg == "--login-history-file"),
        "the gateway CLI has no --login-history-file flag"
    );
}

#[test]
fn gateway_env_carries_the_login_history_path() {
    let root = Path::new(r"C:\Users\someone\AppData\Local\Chat2API");
    let paths = InstancePaths::under(root);
    let env = gateway_env(&paths, None);
    assert_eq!(env.len(), 2);
    assert_eq!(env[0].0, "CHAT2API_LOGIN_HISTORY_FILE");
    assert_eq!(
        env[0].1,
        root.join("instance")
            .join("login-history.jsonl")
            .into_os_string()
    );
    assert_eq!(env[1].0, "CHAT2API_UPSTREAM_FAULT_LOG");
    assert_eq!(
        env[1].1,
        root.join("instance")
            .join("upstream-faults.json")
            .into_os_string()
    );
}

#[test]
fn gateway_env_injects_the_saved_credentials() {
    let root = Path::new(r"C:\Users\someone\AppData\Local\Chat2API");
    let paths = InstancePaths::under(root);
    let credentials = Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "super-secret".to_string(),
    };
    let env = gateway_env(&paths, Some(&credentials));
    let account = env
        .iter()
        .find(|(key, _)| key == "CHAT2API_UM_ACCOUNT")
        .expect("account env");
    assert_eq!(account.1, "someone@um.edu.mo");
    let password = env
        .iter()
        .find(|(key, _)| key == "CHAT2API_UM_PASSWORD")
        .expect("password env");
    assert_eq!(password.1, "super-secret");
}

#[test]
fn gateway_env_without_credentials_does_not_set_them() {
    let root = Path::new(r"C:\Users\someone\AppData\Local\Chat2API");
    let paths = InstancePaths::under(root);
    let env = gateway_env(&paths, None);
    assert!(!env
        .iter()
        .any(|(key, _)| key == "CHAT2API_UM_ACCOUNT" || key == "CHAT2API_UM_PASSWORD"));
}

#[test]
fn resolve_prefers_the_env_override() {
    let dir = TempDir::new("gateway-resolve-env");
    let app_dir = dir.path().join("app");
    let override_path = dir.path().join("custom-gateway.exe");
    std::fs::create_dir_all(&app_dir).unwrap();
    std::fs::write(app_dir.join("um-gateway.exe"), b"app").unwrap();
    std::fs::write(&override_path, b"override").unwrap();
    let program =
        resolve_gateway_binary(Some(override_path.clone()), &app_dir).expect("env override wins");
    assert_eq!(program, GatewayProgram::plain(override_path));
}

#[test]
fn resolve_reports_a_missing_env_override_instead_of_falling_back() {
    let dir = TempDir::new("gateway-resolve-missing-env");
    let app_dir = dir.path().join("app");
    std::fs::create_dir_all(&app_dir).unwrap();
    std::fs::write(app_dir.join("um-gateway.exe"), b"app").unwrap();
    let override_path = dir.path().join("missing-gateway.exe");
    match resolve_gateway_binary(Some(override_path.clone()), &app_dir) {
        Err(BinaryError::Missing { source, path }) => {
            assert_eq!(source, "UM_DESKTOP_GATEWAY_BIN");
            assert_eq!(path, override_path);
        }
        other => panic!("a missing override must be reported, got {other:?}"),
    }
}

#[test]
fn resolve_prefers_the_app_directory_then_the_dev_fallback() {
    let dir = TempDir::new("gateway-resolve-order");
    let repo = dir.path().join("repo");
    let app_dir = repo.join("desktop").join("target").join("release");
    let dev_binary = repo
        .join("rust")
        .join("target")
        .join("release")
        .join("um-gateway-candidate.exe");
    std::fs::create_dir_all(&app_dir).unwrap();
    std::fs::create_dir_all(dev_binary.parent().unwrap()).unwrap();
    std::fs::write(&dev_binary, b"dev").unwrap();

    let program = resolve_gateway_binary(None, &app_dir).expect("dev fallback");
    assert_eq!(program, GatewayProgram::plain(dev_binary));

    let app_binary = app_dir.join("um-gateway.exe");
    std::fs::write(&app_binary, b"app").unwrap();
    let program = resolve_gateway_binary(None, &app_dir).expect("app binary");
    assert_eq!(program, GatewayProgram::plain(app_binary));
}

#[test]
fn resolve_reports_a_non_executable_app_binary() {
    let dir = TempDir::new("gateway-resolve-not-executable");
    let app_dir = dir.path().join("app");
    std::fs::create_dir_all(app_dir.join("um-gateway.exe")).unwrap();
    match resolve_gateway_binary(None, &app_dir) {
        Err(BinaryError::NotExecutable { path, reason }) => {
            assert!(path.ends_with("um-gateway.exe"));
            assert!(reason.contains("普通文件"), "{reason}");
        }
        other => panic!("a non-executable app binary must be reported, got {other:?}"),
    }
}

#[test]
fn resolve_reports_every_tried_path() {
    let dir = TempDir::new("gateway-resolve-none");
    let app_dir = dir.path().join("app");
    std::fs::create_dir_all(&app_dir).unwrap();
    match resolve_gateway_binary(None, &app_dir) {
        Err(BinaryError::NotFound { tried }) => {
            assert!(tried.iter().any(|path| path.ends_with("um-gateway.exe")));
            assert!(tried
                .iter()
                .any(|path| path.ends_with("um-gateway-candidate.exe")));
        }
        other => panic!("no gateway binary must be reported, got {other:?}"),
    }
}

#[test]
fn instance_directories_are_created_before_start() {
    let dir = TempDir::new("gateway-dirs");
    let paths = InstancePaths::under(dir.path());
    paths.ensure_dirs().expect("create instance dirs");
    assert!(paths.instance_dir.is_dir());
    assert!(paths.profile_dir.is_dir());
    assert!(paths.supervision_log.parent().unwrap().is_dir());
}

#[cfg(windows)]
mod windows_acl {
    use um_desktop::gateway::ensure_control_token;

    use super::TempDir;

    #[test]
    fn control_token_acl_is_restricted_to_current_user() {
        let dir = TempDir::new("gateway-token-acl");
        let path = dir.path().join("instance").join("admin-control-token");
        ensure_control_token(&path).expect("generate token");
        let (ace_count, only_current_user) = super::common::acl_ace_count(&path);
        assert_eq!(ace_count, 1, "control token must carry one ACE");
        assert!(only_current_user, "the only ACE must be the current user");
    }
}

// ---------------------------------------------------------------------------
// Supervisor seam: a fake HTTP edge and a fake process launcher.
// ---------------------------------------------------------------------------

use std::collections::{HashMap, HashSet};
use std::ffi::OsString;
use std::net::TcpListener;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use um_desktop::gateway::{
    quit_app, ChildProcess, GatewayHttp, GatewayState, GatewaySupervisor, HttpGateway, Ownership,
    PortSwitch, ProcessExit, ProcessLauncher, Sleeper, StopReason, SystemLauncher, ThreadSleeper,
};

#[derive(Debug, Clone, PartialEq, Eq)]
struct SpawnRecord {
    program: PathBuf,
    prefix_args: Vec<OsString>,
    args: Vec<String>,
    envs: Vec<(String, String)>,
    stderr_log: PathBuf,
}

#[derive(Default)]
struct FakeProcessState {
    exit: Option<ProcessExit>,
    killed: bool,
    try_wait_calls: usize,
}

struct FakeProcess {
    state: Arc<Mutex<FakeProcessState>>,
}

impl ChildProcess for FakeProcess {
    fn id(&self) -> u32 {
        4242
    }

    fn try_wait(&mut self) -> Result<Option<ProcessExit>, String> {
        let mut state = self.state.lock().unwrap();
        state.try_wait_calls += 1;
        Ok(state.exit)
    }

    fn kill(&mut self) -> Result<(), String> {
        let mut state = self.state.lock().unwrap();
        state.killed = true;
        state.exit = Some(ProcessExit { code: Some(-1) });
        Ok(())
    }
}

#[derive(Default)]
struct FakeLauncher {
    records: Mutex<Vec<SpawnRecord>>,
    processes: Arc<Mutex<Vec<Arc<Mutex<FakeProcessState>>>>>,
    http: Mutex<Option<Arc<FakeHttp>>>,
    fail_spawn_ports: Mutex<HashMap<u16, ProcessExit>>,
    stderr_text: Mutex<Option<String>>,
}

impl FakeLauncher {
    fn attach_http(&self, http: Arc<FakeHttp>) {
        *self.http.lock().unwrap() = Some(http);
    }

    fn spawn_count(&self) -> usize {
        self.records.lock().unwrap().len()
    }

    fn record(&self, index: usize) -> SpawnRecord {
        self.records.lock().unwrap()[index].clone()
    }

    fn process(&self, index: usize) -> Arc<Mutex<FakeProcessState>> {
        Arc::clone(&self.processes.lock().unwrap()[index])
    }

    fn fail_spawn_on(&self, port: u16, code: i32) {
        self.fail_spawn_ports
            .lock()
            .unwrap()
            .insert(port, ProcessExit { code: Some(code) });
    }

    fn set_stderr_text(&self, text: &str) {
        *self.stderr_text.lock().unwrap() = Some(text.to_string());
    }

    fn spawn_inner(
        &self,
        program: &GatewayProgram,
        args: &[OsString],
        envs: &[(OsString, OsString)],
        stderr_log: &Path,
    ) -> Result<Box<dyn ChildProcess>, String> {
        let rendered: Vec<String> = args
            .iter()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect();
        let port = rendered
            .windows(2)
            .find(|pair| pair[0] == "--port")
            .and_then(|pair| pair[1].parse::<u16>().ok())
            .unwrap_or(0);
        let rendered_envs: Vec<(String, String)> = envs
            .iter()
            .map(|(key, value)| {
                (
                    key.to_string_lossy().into_owned(),
                    value.to_string_lossy().into_owned(),
                )
            })
            .collect();
        self.records.lock().unwrap().push(SpawnRecord {
            program: program.program.clone(),
            prefix_args: program.prefix_args.clone(),
            args: rendered,
            envs: rendered_envs,
            stderr_log: stderr_log.to_path_buf(),
        });
        std::fs::write(
            stderr_log,
            self.stderr_text
                .lock()
                .unwrap()
                .as_deref()
                .unwrap_or("fake gateway stderr\n"),
        )
        .ok();
        if let Some(http) = self.http.lock().unwrap().as_ref() {
            http.mark_spawned(port);
        }
        let exit = self.fail_spawn_ports.lock().unwrap().get(&port).copied();
        let state = Arc::new(Mutex::new(FakeProcessState {
            exit,
            ..Default::default()
        }));
        self.processes.lock().unwrap().push(Arc::clone(&state));
        Ok(Box::new(FakeProcess { state }))
    }
}

struct FakeHttp {
    fail_ports: Mutex<HashSet<u16>>,
    healthy: Mutex<HashMap<u16, bool>>,
    shutdown_calls: Mutex<Vec<u16>>,
    health_calls: AtomicUsize,
    shutdown_keeps_healthy: AtomicBool,
    shutdown_stops_processes: AtomicBool,
    processes: Arc<Mutex<Vec<Arc<Mutex<FakeProcessState>>>>>,
}

impl FakeHttp {
    fn new(processes: Arc<Mutex<Vec<Arc<Mutex<FakeProcessState>>>>>) -> Self {
        Self {
            fail_ports: Mutex::new(HashSet::new()),
            healthy: Mutex::new(HashMap::new()),
            shutdown_calls: Mutex::new(Vec::new()),
            health_calls: AtomicUsize::new(0),
            shutdown_keeps_healthy: AtomicBool::new(false),
            shutdown_stops_processes: AtomicBool::new(true),
            processes,
        }
    }

    fn fail_port(&self, port: u16) {
        self.fail_ports.lock().unwrap().insert(port);
    }

    fn healthy_instance(&self, port: u16) {
        self.healthy.lock().unwrap().insert(port, true);
    }

    fn mark_spawned(&self, port: u16) {
        let healthy = !self.fail_ports.lock().unwrap().contains(&port);
        self.healthy.lock().unwrap().insert(port, healthy);
    }

    fn health_calls(&self) -> usize {
        self.health_calls.load(Ordering::SeqCst)
    }

    fn shutdown_calls(&self) -> Vec<u16> {
        self.shutdown_calls.lock().unwrap().clone()
    }

    fn keep_healthy_after_shutdown(&self) {
        self.shutdown_keeps_healthy.store(true, Ordering::SeqCst);
    }

    fn keep_process_after_shutdown(&self) {
        self.shutdown_stops_processes.store(false, Ordering::SeqCst);
    }

    fn port_of(base_url: &str) -> u16 {
        base_url
            .rsplit(':')
            .next()
            .and_then(|port| port.trim_end_matches('/').parse().ok())
            .unwrap_or(0)
    }
}

impl GatewayHttp for FakeHttp {
    fn health(&self, base_url: &str) -> Result<(), String> {
        self.health_calls.fetch_add(1, Ordering::SeqCst);
        let port = Self::port_of(base_url);
        if self.fail_ports.lock().unwrap().contains(&port) {
            return Err(format!("127.0.0.1:{port}: connection refused"));
        }
        match self.healthy.lock().unwrap().get(&port).copied() {
            Some(true) => Ok(()),
            _ => Err(format!("127.0.0.1:{port}: status=stopped")),
        }
    }

    fn shutdown(
        &self,
        base_url: &str,
        token: &um_desktop::gateway::ControlToken,
    ) -> Result<(), String> {
        let port = Self::port_of(base_url);
        assert_eq!(token.expose().len(), 64, "shutdown must carry the token");
        self.shutdown_calls.lock().unwrap().push(port);
        if !self.shutdown_keeps_healthy.load(Ordering::SeqCst) {
            self.healthy.lock().unwrap().insert(port, false);
        }
        if self.shutdown_stops_processes.load(Ordering::SeqCst) {
            for process in self.processes.lock().unwrap().iter() {
                process.lock().unwrap().exit = Some(ProcessExit { code: Some(0) });
            }
        }
        Ok(())
    }
}

#[derive(Default)]
struct NoSleep {
    sleeps: Mutex<Vec<Duration>>,
}

impl NoSleep {
    fn total(&self) -> Duration {
        self.sleeps.lock().unwrap().iter().sum()
    }
}

impl Sleeper for NoSleep {
    fn sleep(&self, duration: Duration) {
        self.sleeps.lock().unwrap().push(duration);
    }
}

struct SharedSleeper(Arc<NoSleep>);

impl Sleeper for SharedSleeper {
    fn sleep(&self, duration: Duration) {
        self.0.sleep(duration);
    }
}

struct SharedLauncher(Arc<FakeLauncher>);

impl ProcessLauncher for SharedLauncher {
    fn spawn(
        &self,
        program: &GatewayProgram,
        args: &[OsString],
        envs: &[(OsString, OsString)],
        stderr_log: &Path,
    ) -> Result<Box<dyn ChildProcess>, String> {
        self.0.spawn_inner(program, args, envs, stderr_log)
    }
}

/// The fake processes do not listen, so the loopback port checks are the only
/// shared resource. Serialize the supervisor tests that use 7997.
static PORT_LOCK: Mutex<()> = Mutex::new(());

struct FakeWorld {
    _port_guard: std::sync::MutexGuard<'static, ()>,
    launcher: Arc<FakeLauncher>,
    http: Arc<FakeHttp>,
    sleeper: Arc<NoSleep>,
}

impl FakeWorld {
    fn new() -> Self {
        let port_guard = PORT_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let processes = Arc::new(Mutex::new(Vec::new()));
        let http = Arc::new(FakeHttp::new(Arc::clone(&processes)));
        let launcher = Arc::new(FakeLauncher {
            processes: Arc::clone(&processes),
            ..Default::default()
        });
        launcher.attach_http(Arc::clone(&http));
        Self {
            _port_guard: port_guard,
            launcher,
            http,
            sleeper: Arc::new(NoSleep::default()),
        }
    }

    fn supervisor(&self, dir: &TempDir, port: u16) -> GatewaySupervisor {
        self.supervisor_with_program(dir, port, Ok(GatewayProgram::plain("fake-gateway.exe")))
    }

    fn supervisor_with_program(
        &self,
        dir: &TempDir,
        port: u16,
        program: Result<GatewayProgram, BinaryError>,
    ) -> GatewaySupervisor {
        GatewaySupervisor::new(
            port,
            InstancePaths::under(dir.path()),
            program,
            Arc::clone(&self.http) as Arc<dyn GatewayHttp>,
            Box::new(SharedLauncher(Arc::clone(&self.launcher))),
            Box::new(SharedSleeper(Arc::clone(&self.sleeper))),
        )
    }
}

#[test]
fn start_waits_for_health_and_reports_a_managed_child() {
    let dir = TempDir::new("gateway-start");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let status = supervisor.start_or_attach();
    assert_eq!(
        status.state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );
    assert_eq!(status.port, 7997);
    assert_eq!(status.base_url, "http://127.0.0.1:7997");
    assert_eq!(supervisor.child_count(), 1);
    assert_eq!(world.launcher.spawn_count(), 1);
    assert!(
        world.http.health_calls() >= 2,
        "the start must wait for /health"
    );

    let record = world.launcher.record(0);
    assert_eq!(record.program, PathBuf::from("fake-gateway.exe"));
    assert!(record.prefix_args.is_empty());
    assert!(record
        .args
        .windows(2)
        .any(|pair| pair == ["--port", "7997"]));
    assert!(record
        .args
        .windows(2)
        .any(|pair| pair == ["--mode", "production"]));
    assert!(record
        .args
        .windows(2)
        .any(|pair| pair == ["--role", "local"]));
    assert!(record
        .envs
        .iter()
        .any(|(key, _)| key == "CHAT2API_LOGIN_HISTORY_FILE"));

    let token_file = dir.path().join("instance").join("admin-control-token");
    let token = std::fs::read_to_string(&token_file).expect("token file");
    assert_eq!(token.trim().len(), 64);
    assert!(!record.args.iter().any(|arg| arg.contains(token.trim())));
    assert!(!record
        .envs
        .iter()
        .any(|(_, value)| value.contains(token.trim())));

    let log = std::fs::read_to_string(&status.supervision_log).expect("supervision log");
    assert!(log.contains("\"event\":\"start\""));
    assert!(!log.contains(token.trim()));
}

#[test]
fn start_attaches_to_a_healthy_instance_without_spawning() {
    let dir = TempDir::new("gateway-attach");
    let world = FakeWorld::new();
    world.http.healthy_instance(7997);
    let mut supervisor = world.supervisor(&dir, 7997);
    let status = supervisor.start_or_attach();
    assert_eq!(
        status.state,
        GatewayState::Running {
            ownership: Ownership::Attached
        }
    );
    assert_eq!(supervisor.child_count(), 0);
    assert_eq!(world.launcher.spawn_count(), 0);
    let log = std::fs::read_to_string(&status.supervision_log).expect("supervision log");
    assert!(log.contains("\"event\":\"attach\""));
}

#[test]
fn a_second_start_reuses_the_single_managed_child() {
    let dir = TempDir::new("gateway-single");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    let _ = supervisor.start_or_attach();
    assert_eq!(world.launcher.spawn_count(), 1);
    assert_eq!(supervisor.child_count(), 1);
}

#[test]
fn a_restart_after_the_managed_child_exited_does_not_claim_it() {
    let dir = TempDir::new("gateway-child-exited");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    world.launcher.process(0).lock().unwrap().exit = Some(ProcessExit { code: Some(0) });
    let status = supervisor.start_or_attach();
    assert_eq!(
        status.state,
        GatewayState::Running {
            ownership: Ownership::Attached
        }
    );
    assert_eq!(supervisor.child_count(), 0);
    assert_eq!(world.launcher.spawn_count(), 1);
}

#[test]
fn an_occupied_port_is_a_distinct_state_and_spawns_nothing() {
    let dir = TempDir::new("gateway-port-in-use");
    let world = FakeWorld::new();
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("occupy a port");
    let port = listener.local_addr().unwrap().port();
    let mut supervisor = world.supervisor(&dir, port);
    let status = supervisor.start_or_attach();
    match status.state {
        GatewayState::PortInUse {
            port: reported,
            reason,
        } => {
            assert_eq!(reported, port);
            assert!(reason.contains(&port.to_string()), "{reason}");
        }
        other => panic!("an occupied port must be distinct, got {other:?}"),
    }
    assert_eq!(world.launcher.spawn_count(), 0);
    drop(listener);
}

#[test]
fn a_start_failure_reports_the_exit_and_the_stderr_tail() {
    let dir = TempDir::new("gateway-start-failed");
    let world = FakeWorld::new();
    world.http.fail_port(7997);
    world.launcher.fail_spawn_on(7997, 1);
    let mut supervisor = world.supervisor(&dir, 7997);
    let status = supervisor.start_or_attach();
    match status.state {
        GatewayState::StartFailed { reason } => {
            assert!(reason.contains("退出码 1"), "{reason}");
            assert!(reason.contains("fake gateway stderr"), "{reason}");
        }
        other => panic!("a failed start must be distinct, got {other:?}"),
    }
    assert_eq!(supervisor.child_count(), 0);
    let log = std::fs::read_to_string(&status.supervision_log).expect("supervision log");
    assert!(log.contains("\"event\":\"start_failed\""));
}

#[test]
fn a_health_timeout_keeps_the_child_and_reports_the_log() {
    let dir = TempDir::new("gateway-health-timeout");
    let world = FakeWorld::new();
    world.http.fail_port(7997);
    let mut supervisor = world.supervisor(&dir, 7997);
    let status = supervisor.start_or_attach();
    match status.state {
        GatewayState::HealthTimeout { reason } => {
            assert!(reason.contains("20 秒"), "{reason}");
            assert!(reason.contains("127.0.0.1:7997"), "{reason}");
        }
        other => panic!("a health timeout must be distinct, got {other:?}"),
    }
    assert_eq!(supervisor.child_count(), 1);
    assert_eq!(world.sleeper.total(), Duration::from_secs(20));
}

#[test]
fn a_missing_binary_is_a_setup_failure_with_the_reason() {
    let dir = TempDir::new("gateway-setup-failed");
    let world = FakeWorld::new();
    let program = Err(BinaryError::NotFound {
        tried: vec![PathBuf::from(r"C:\missing\um-gateway.exe")],
    });
    let mut supervisor = world.supervisor_with_program(&dir, 7997, program);
    let status = supervisor.start_or_attach();
    match status.state {
        GatewayState::SetupFailed { reason } => {
            assert!(reason.contains("未找到网关二进制"), "{reason}");
        }
        other => panic!("a missing binary must be a setup failure, got {other:?}"),
    }
    assert_eq!(world.launcher.spawn_count(), 0);
}

#[test]
fn stop_posts_shutdown_and_waits_for_its_own_child() {
    let dir = TempDir::new("gateway-stop");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    let status = supervisor.stop(StopReason::UserRequested);
    assert_eq!(status.state, GatewayState::Stopped);
    assert_eq!(supervisor.child_count(), 0);
    assert_eq!(world.http.shutdown_calls(), vec![7997]);
    let process = world.launcher.process(0);
    let process = process.lock().unwrap();
    assert!(!process.killed, "a graceful stop must not kill the child");
    drop(process);
    let log = std::fs::read_to_string(&status.supervision_log).expect("supervision log");
    assert!(log.contains("\"event\":\"stop\""), "{log}");
    assert!(log.contains("\"reason\":\"user\""), "{log}");
    assert!(!log.contains("\"forced\":true"), "{log}");
}

#[test]
fn stop_kills_its_own_child_only_after_the_timeout() {
    let dir = TempDir::new("gateway-stop-timeout");
    let world = FakeWorld::new();
    world.http.keep_process_after_shutdown();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    let status = supervisor.stop(StopReason::UserRequested);
    assert_eq!(status.state, GatewayState::Stopped);
    assert_eq!(supervisor.child_count(), 0);
    let process = world.launcher.process(0);
    let process = process.lock().unwrap();
    assert!(process.killed, "only the timeout may kill the child");
    drop(process);
    assert!(world.sleeper.total() >= Duration::from_secs(10));
    let log = std::fs::read_to_string(&status.supervision_log).expect("supervision log");
    assert!(log.contains("\"forced\":true"), "{log}");
}

#[test]
fn stop_never_kills_an_attached_instance() {
    let dir = TempDir::new("gateway-stop-attached");
    let world = FakeWorld::new();
    world.http.healthy_instance(7997);
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    let status = supervisor.stop(StopReason::UserRequested);
    assert_eq!(status.state, GatewayState::Stopped);
    assert_eq!(supervisor.child_count(), 0);
    assert_eq!(world.http.shutdown_calls(), vec![7997]);
    assert_eq!(world.launcher.spawn_count(), 0);
}

#[test]
fn stop_keeps_an_attached_instance_that_does_not_stop() {
    let dir = TempDir::new("gateway-stop-attached-timeout");
    let world = FakeWorld::new();
    world.http.healthy_instance(7997);
    world.http.keep_healthy_after_shutdown();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    let status = supervisor.stop(StopReason::UserRequested);
    match status.state {
        GatewayState::StopFailed { reason } => {
            assert!(reason.contains("10 秒"), "{reason}");
        }
        other => panic!("an attached instance that does not stop must fail, got {other:?}"),
    }
    assert_eq!(supervisor.child_count(), 0);
    assert_eq!(world.http.shutdown_calls(), vec![7997]);
    assert_eq!(world.launcher.spawn_count(), 0);
}

#[test]
fn switch_port_stops_the_old_and_starts_the_new_port() {
    let dir = TempDir::new("gateway-switch");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    let mut saved = Vec::new();
    let outcome = supervisor.switch_port(7996, &mut |port| {
        saved.push(port);
        Ok(())
    });
    assert_eq!(
        outcome,
        PortSwitch::Switched {
            from: 7997,
            to: 7996
        }
    );
    assert_eq!(supervisor.port(), 7996);
    assert_eq!(saved, vec![7996]);
    assert_eq!(world.launcher.spawn_count(), 2);
    assert_eq!(world.http.shutdown_calls(), vec![7997]);
    assert_eq!(
        supervisor.status().state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );
}

#[test]
fn switch_port_rejects_an_occupied_port_and_keeps_the_old_listener() {
    let dir = TempDir::new("gateway-switch-occupied");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("occupy a port");
    let new_port = listener.local_addr().unwrap().port();
    let mut saved = Vec::new();
    let outcome = supervisor.switch_port(new_port, &mut |port| {
        saved.push(port);
        Ok(())
    });
    match outcome {
        PortSwitch::Rejected { from, to, reason } => {
            assert_eq!(from, 7997);
            assert_eq!(to, new_port);
            assert!(reason.contains(&new_port.to_string()), "{reason}");
        }
        other => panic!("an occupied port must be rejected, got {other:?}"),
    }
    assert_eq!(supervisor.port(), 7997);
    assert!(saved.is_empty());
    assert_eq!(world.launcher.spawn_count(), 1);
    assert_eq!(
        supervisor.status().state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );
    drop(listener);
}

#[test]
fn switch_port_restores_the_old_port_when_the_new_start_fails() {
    let dir = TempDir::new("gateway-switch-restore");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    world.http.fail_port(7996);
    world.launcher.fail_spawn_on(7996, 1);
    let mut saved = Vec::new();
    let outcome = supervisor.switch_port(7996, &mut |port| {
        saved.push(port);
        Ok(())
    });
    match outcome {
        PortSwitch::Restored { from, to, reason } => {
            assert_eq!(from, 7997);
            assert_eq!(to, 7996);
            assert!(reason.contains("退出码 1"), "{reason}");
        }
        other => panic!("a failed new start must restore the old port, got {other:?}"),
    }
    assert_eq!(supervisor.port(), 7997);
    assert_eq!(saved, vec![7996, 7997]);
    assert_eq!(world.launcher.spawn_count(), 3);
    assert_eq!(
        supervisor.status().state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );
}

#[test]
fn switch_port_rejects_governed_ports_without_touching_the_listener() {
    let dir = TempDir::new("gateway-switch-governed");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    for port in [8000, 8001, 8002, 8003] {
        let mut saved = Vec::new();
        let outcome = supervisor.switch_port(port, &mut |port| {
            saved.push(port);
            Ok(())
        });
        match outcome {
            PortSwitch::Rejected { from, to, reason } => {
                assert_eq!(from, 7997);
                assert_eq!(to, port);
                assert!(reason.contains(&port.to_string()), "{reason}");
            }
            other => panic!("governed port {port} must be rejected, got {other:?}"),
        }
        assert!(saved.is_empty());
    }
    assert_eq!(supervisor.port(), 7997);
    assert_eq!(world.launcher.spawn_count(), 1);
}

#[test]
fn switch_port_recovers_from_an_occupied_old_port() {
    let dir = TempDir::new("gateway-switch-recover-port");
    let world = FakeWorld::new();
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("occupy a port");
    let old_port = listener.local_addr().unwrap().port();
    let mut supervisor = world.supervisor(&dir, old_port);
    assert!(matches!(
        supervisor.start_or_attach().state,
        GatewayState::PortInUse { .. }
    ));
    let new_port = {
        let probe = TcpListener::bind(("127.0.0.1", 0)).expect("free a port");
        let port = probe.local_addr().unwrap().port();
        drop(probe);
        port
    };
    let mut saved = Vec::new();
    let outcome = supervisor.switch_port(new_port, &mut |port| {
        saved.push(port);
        Ok(())
    });
    assert_eq!(
        outcome,
        PortSwitch::Switched {
            from: old_port,
            to: new_port
        }
    );
    assert_eq!(supervisor.port(), new_port);
    assert_eq!(saved, vec![new_port]);
    assert_eq!(world.launcher.spawn_count(), 1);
    drop(listener);
}

#[test]
fn switch_port_recovers_from_a_start_failure() {
    let dir = TempDir::new("gateway-switch-recover-start");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    world.http.fail_port(7997);
    world.launcher.fail_spawn_on(7997, 1);
    assert!(matches!(
        supervisor.start_or_attach().state,
        GatewayState::StartFailed { .. }
    ));
    let mut saved = Vec::new();
    let outcome = supervisor.switch_port(7996, &mut |port| {
        saved.push(port);
        Ok(())
    });
    assert_eq!(
        outcome,
        PortSwitch::Switched {
            from: 7997,
            to: 7996
        }
    );
    assert_eq!(supervisor.port(), 7996);
    assert_eq!(saved, vec![7996]);
    assert_eq!(world.launcher.spawn_count(), 2);
}

#[test]
fn switch_port_to_the_same_port_is_unchanged() {
    let dir = TempDir::new("gateway-switch-same");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    let mut saved = Vec::new();
    let outcome = supervisor.switch_port(7997, &mut |port| {
        saved.push(port);
        Ok(())
    });
    assert_eq!(outcome, PortSwitch::Unchanged { port: 7997 });
    assert!(saved.is_empty());
    assert_eq!(world.launcher.spawn_count(), 1);
}

#[test]
fn quit_stops_the_gateway_before_exiting_the_app() {
    let dir = TempDir::new("gateway-quit");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let _ = supervisor.start_or_attach();
    let mut quit_called = false;
    let mut shutdowns_at_quit = 0;
    let status = quit_app(&mut supervisor, &mut || {
        quit_called = true;
        shutdowns_at_quit = world.http.shutdown_calls().len();
    });
    assert_eq!(status.state, GatewayState::Stopped);
    assert!(quit_called);
    assert_eq!(shutdowns_at_quit, 1);
}

#[test]
fn a_real_fake_gateway_receives_the_instance_args_and_stops() {
    let _port_guard = PORT_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let dir = TempDir::new("gateway-real-process");
    let port = {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("free port");
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        port
    };
    let state_dir = dir.path().join("fake-state");
    std::fs::create_dir_all(&state_dir).unwrap();
    std::env::set_var("FAKE_GATEWAY_STATE_DIR", &state_dir);

    let python = std::env::var("PYTHON").unwrap_or_else(|_| "python".to_string());
    let script = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("fake_gateway.py");
    assert!(script.is_file(), "missing fake gateway script");
    let program =
        GatewayProgram::with_prefix(python, vec![OsString::from("-I"), script.into_os_string()]);
    let paths = InstancePaths::under(dir.path());
    let mut supervisor = GatewaySupervisor::new(
        port,
        paths.clone(),
        Ok(program),
        Arc::new(HttpGateway::new()) as Arc<dyn GatewayHttp>,
        Box::new(SystemLauncher),
        Box::new(ThreadSleeper),
    );
    let status = supervisor.start_or_attach();
    assert_eq!(
        status.state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );

    let spawn_record = std::fs::read_dir(&state_dir)
        .unwrap()
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .find(|path| {
            path.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("spawn-")
        })
        .expect("fake gateway spawn record");
    let record: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&spawn_record).unwrap()).unwrap();
    let recorded_args: Vec<&str> = record["args"]
        .as_array()
        .unwrap()
        .iter()
        .map(|value| value.as_str().unwrap())
        .collect();
    let port_text = port.to_string();
    assert!(
        recorded_args
            .windows(2)
            .any(|pair| pair == ["--port", port_text.as_str()]),
        "{recorded_args:?}"
    );
    assert_eq!(
        record["login_history_env"].as_str().unwrap(),
        paths.login_history.to_string_lossy().as_ref()
    );
    let token = std::fs::read_to_string(&paths.control_token).unwrap();
    assert!(!recorded_args.iter().any(|arg| arg.contains(token.trim())));
    assert!(!record.to_string().contains(token.trim()));

    let stop = supervisor.stop(StopReason::UserRequested);
    assert_eq!(stop.state, GatewayState::Stopped);
    let shutdown: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(state_dir.join("shutdown.json")).unwrap())
            .unwrap();
    assert_eq!(shutdown["token_ok"], true);
    let log = std::fs::read_to_string(&stop.supervision_log).unwrap();
    assert!(!log.contains(token.trim()));
    std::env::remove_var("FAKE_GATEWAY_STATE_DIR");
}

#[test]
fn http_gateway_requires_the_gateway_health_contract() {
    use std::io::{Read, Write};

    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("health server");
    let port = listener.local_addr().unwrap().port();
    let server = std::thread::spawn(move || {
        for body in [
            r#"{"status":"ok"}"#,
            r#"{"status":"ok","chat2api_version":"test"}"#,
        ] {
            let (mut stream, _) = listener.accept().expect("health request");
            let mut buffer = [0u8; 1024];
            let _ = stream.read(&mut buffer);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("health response");
        }
    });
    let http = HttpGateway::new();
    let base = format!("http://127.0.0.1:{port}");
    assert!(http.health(&base).is_err(), "a non-gateway must not attach");
    assert!(http.health(&base).is_ok(), "the gateway health must attach");
    server.join().expect("health server");
}

/// Real binary verification for #124. Run with
/// `UM_DESKTOP_REAL_GATEWAY=<path> cargo test --test gateway -- --ignored --nocapture`.
#[test]
#[ignore = "requires UM_DESKTOP_REAL_GATEWAY=<built um-gateway path>"]
fn a_real_gateway_starts_stops_switches_and_attaches_on_7999() {
    let _port_guard = PORT_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let Some(bin) = std::env::var_os("UM_DESKTOP_REAL_GATEWAY").map(PathBuf::from) else {
        panic!("set UM_DESKTOP_REAL_GATEWAY to the built um-gateway binary");
    };
    let dir = TempDir::new("gateway-real-supervisor");
    let paths = InstancePaths::under(dir.path());
    let http = Arc::new(HttpGateway::new());
    let mut supervisor = GatewaySupervisor::new(
        7999,
        paths.clone(),
        Ok(GatewayProgram::plain(bin)),
        Arc::clone(&http) as Arc<dyn GatewayHttp>,
        Box::new(SystemLauncher),
        Box::new(ThreadSleeper),
    );

    let started = supervisor.start_or_attach();
    assert_eq!(
        started.state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );
    assert_eq!(supervisor.child_count(), 1);
    assert!(http.health("http://127.0.0.1:7999").is_ok());

    let stopped = supervisor.stop(StopReason::UserRequested);
    assert_eq!(stopped.state, GatewayState::Stopped);
    assert_eq!(supervisor.child_count(), 0);
    assert!(http.health("http://127.0.0.1:7999").is_err());

    let restarted = supervisor.start_or_attach();
    assert_eq!(
        restarted.state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );
    assert!(http.health("http://127.0.0.1:7999").is_ok());

    let mut saved = Vec::new();
    let switched = supervisor.switch_port(7998, &mut |port| {
        saved.push(port);
        Ok(())
    });
    assert_eq!(
        switched,
        PortSwitch::Switched {
            from: 7999,
            to: 7998
        }
    );
    assert_eq!(supervisor.port(), 7998);
    assert!(http.health("http://127.0.0.1:7998").is_ok());
    assert!(http.health("http://127.0.0.1:7999").is_err());

    let (listener, occupied_port) = {
        let listener = TcpListener::bind(("127.0.0.1", 7997)).expect("occupy 7997");
        (listener, 7997)
    };
    let rejected = supervisor.switch_port(occupied_port, &mut |port| {
        saved.push(port);
        Ok(())
    });
    match rejected {
        PortSwitch::Rejected { from, to, reason } => {
            assert_eq!(from, 7998);
            assert_eq!(to, occupied_port);
            assert!(reason.contains(&occupied_port.to_string()), "{reason}");
        }
        other => panic!("an occupied port must be rejected, got {other:?}"),
    }
    assert_eq!(supervisor.port(), 7998);
    assert!(http.health("http://127.0.0.1:7998").is_ok());
    drop(listener);

    let back = supervisor.switch_port(7999, &mut |port| {
        saved.push(port);
        Ok(())
    });
    assert_eq!(
        back,
        PortSwitch::Switched {
            from: 7998,
            to: 7999
        }
    );
    assert!(http.health("http://127.0.0.1:7999").is_ok());
    assert_eq!(saved, vec![7998, 7999]);

    let final_stop = supervisor.stop(StopReason::UserRequested);
    assert_eq!(final_stop.state, GatewayState::Stopped);
}

#[test]
#[ignore = "requires UM_DESKTOP_REAL_GATEWAY=<built um-gateway path>"]
fn a_real_gateway_is_attached_without_a_second_child() {
    let _port_guard = PORT_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let Some(bin) = std::env::var_os("UM_DESKTOP_REAL_GATEWAY").map(PathBuf::from) else {
        panic!("set UM_DESKTOP_REAL_GATEWAY to the built um-gateway binary");
    };
    let dir = TempDir::new("gateway-real-attach");
    let paths = InstancePaths::under(dir.path());
    let _token = ensure_control_token(&paths.control_token).expect("control token");
    let mut envs = gateway_env(&paths, None);
    envs.push((OsString::from("CHAT2API_NO_BROWSER"), OsString::from("1")));
    let mut child = SystemLauncher
        .spawn(
            &GatewayProgram::plain(bin),
            &gateway_args(7999, &paths),
            &envs,
            &paths.stderr_log,
        )
        .expect("start the external gateway");
    let http = HttpGateway::new();
    for _ in 0..200 {
        if http.health("http://127.0.0.1:7999").is_ok() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    assert!(http.health("http://127.0.0.1:7999").is_ok());

    let mut supervisor = GatewaySupervisor::new(
        7999,
        paths.clone(),
        Ok(GatewayProgram::plain("unused-gateway.exe")),
        Arc::new(HttpGateway::new()) as Arc<dyn GatewayHttp>,
        Box::new(SystemLauncher),
        Box::new(ThreadSleeper),
    );
    let attached = supervisor.start_or_attach();
    assert_eq!(
        attached.state,
        GatewayState::Running {
            ownership: Ownership::Attached
        }
    );
    assert_eq!(supervisor.child_count(), 0);

    let stopped = supervisor.stop(StopReason::UserRequested);
    assert_eq!(stopped.state, GatewayState::Stopped);
    for _ in 0..50 {
        if child.try_wait().expect("wait external gateway").is_some() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    assert!(
        child.try_wait().expect("wait external gateway").is_some(),
        "the attached gateway must exit after /admin/shutdown"
    );
}

#[test]
fn restart_with_new_credentials_restarts_the_gateway_with_the_new_environment() {
    let dir = TempDir::new("gateway-restart-credentials");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    let started = supervisor.start_or_attach();
    assert_eq!(
        started.state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );
    let first = world.launcher.record(0);
    assert!(!first
        .envs
        .iter()
        .any(|(key, _)| key == "CHAT2API_UM_ACCOUNT" || key == "CHAT2API_UM_PASSWORD"));

    let credentials = Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "super-secret".to_string(),
    };
    supervisor.set_credentials(Some(credentials.clone()));
    let restarted = supervisor.restart(StopReason::CredentialsChanged);
    assert_eq!(
        restarted.state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );
    assert_eq!(
        world.launcher.spawn_count(),
        2,
        "the old child must be replaced"
    );
    assert_eq!(world.http.shutdown_calls(), vec![7997]);
    let second = world.launcher.record(1);
    assert_eq!(
        second
            .envs
            .iter()
            .find(|(key, _)| key == "CHAT2API_UM_ACCOUNT")
            .map(|(_, value)| value.as_str()),
        Some("someone@um.edu.mo")
    );
    assert_eq!(
        second
            .envs
            .iter()
            .find(|(key, _)| key == "CHAT2API_UM_PASSWORD")
            .map(|(_, value)| value.as_str()),
        Some("super-secret")
    );

    let log = std::fs::read_to_string(&restarted.supervision_log).expect("supervision log");
    assert!(log.contains("\"event\":\"restart\""));
    assert!(!log.contains("super-secret"), "{log}");
    assert!(!log.contains("someone@um.edu.mo"), "{log}");
}

#[test]
fn restart_without_credentials_removes_the_environment_on_the_next_spawn() {
    let dir = TempDir::new("gateway-restart-clear");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    supervisor.set_credentials(Some(Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "super-secret".to_string(),
    }));
    let _ = supervisor.start_or_attach();
    assert!(world
        .launcher
        .record(0)
        .envs
        .iter()
        .any(|(key, _)| key == "CHAT2API_UM_ACCOUNT"));

    supervisor.set_credentials(None);
    let restarted = supervisor.restart(StopReason::CredentialsChanged);
    assert_eq!(
        restarted.state,
        GatewayState::Running {
            ownership: Ownership::Managed
        }
    );
    let second = world.launcher.record(1);
    assert!(!second
        .envs
        .iter()
        .any(|(key, _)| key == "CHAT2API_UM_ACCOUNT" || key == "CHAT2API_UM_PASSWORD"));
}

#[test]
fn load_control_token_reads_without_creating_a_token() {
    let dir = TempDir::new("gateway-load-token");
    let path = dir.path().join("instance").join("admin-control-token");
    assert!(um_desktop::gateway::load_control_token(&path).is_err());
    assert!(!path.exists(), "a status read must not create a token");

    let token = ensure_control_token(&path).expect("create token");
    let loaded = um_desktop::gateway::load_control_token(&path).expect("load token");
    assert_eq!(loaded.expose(), token.expose());

    std::fs::write(&path, "bad").unwrap();
    assert!(um_desktop::gateway::load_control_token(&path).is_err());
}

#[test]
fn a_start_failure_redacts_the_saved_credentials_from_stderr_and_log() {
    let dir = TempDir::new("gateway-credentials-redaction");
    let world = FakeWorld::new();
    let mut supervisor = world.supervisor(&dir, 7997);
    supervisor.set_credentials(Some(Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "super-secret".to_string(),
    }));
    world
        .launcher
        .set_stderr_text("login someone@um.edu.mo with super-secret\n");
    world.http.fail_port(7997);
    world.launcher.fail_spawn_on(7997, 1);
    let status = supervisor.start_or_attach();
    match &status.state {
        GatewayState::StartFailed { reason } => {
            assert!(reason.contains("<redacted>"), "{reason}");
            assert!(!reason.contains("super-secret"), "{reason}");
            assert!(!reason.contains("someone@um.edu.mo"), "{reason}");
        }
        other => panic!("expected a start failure, got {other:?}"),
    }
    let log = std::fs::read_to_string(&status.supervision_log).expect("supervision log");
    assert!(!log.contains("super-secret"), "{log}");
    assert!(!log.contains("someone@um.edu.mo"), "{log}");
}
