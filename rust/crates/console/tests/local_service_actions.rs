//! #92: local service actions execute through the managed PowerShell runtime.
//!
//! The service identity, start/stop strategies and the cross-process service lock
//! stay in `invoke-console-runtime-json.ps1` + `lib/gateway-runtime.ps1`. Rust
//! invokes that entry directly and parses its one-object JSON envelope; Python is
//! deliberately absent (`python_exe` points at a path that does not exist).
//!
//! These tests use a fake repo root whose PowerShell entry records its argv and
//! prints canned envelopes, so no test touches the real 8000/8001 services.

mod support;

use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_console::config::Role;
use um_console::lock::ResourceLock;
use um_console::operation_manager::{local_registry, OperationManager};
use um_console::service_actions::PowerShellServiceExecutor;
use um_console::ConsoleServer;
use um_test_harness::TempDataDir;

use support::*;

const FAKE_WRAPPER: &str = r#"
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$action = ''
$key = ''
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq '-Action') { $action = $args[$i + 1] }
    if ($args[$i] -eq '-Key') { $key = $args[$i + 1] }
}
$record = [ordered]@{
    script = $PSCommandPath
    args = @($args)
    cwd = (Get-Location).Path
}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'wrapper_call.json'),
    ($record | ConvertTo-Json -Compress -Depth 4),
    (New-Object System.Text.UTF8Encoding($false)))

if ($action -eq 'start') {
    [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
        '","status":"Started","action":"start"}}')
    exit 0
}
if ($action -eq 'stop') {
    [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
        '","status":"Stopped","action":"Stop","terminated_process_ids":[4242]}}')
    exit 0
}
if ($action -eq 'restart') {
    [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
        '","status":"Restarted","action":"Restart","terminated_process_ids":[4242]}}')
    exit 0
}
[Console]::Out.Write('{"ok":false,"error":"unsupported","detail":"unexpected action"}')
exit 1
"#;

const STATEFUL_WRAPPER: &str = r#"
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$action = ''
$key = ''
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq '-Action') { $action = $args[$i + 1] }
    if ($args[$i] -eq '-Key') { $key = $args[$i + 1] }
}
$record = [ordered]@{
    script = $PSCommandPath
    args = @($args)
    cwd = (Get-Location).Path
}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'wrapper_call.json'),
    ($record | ConvertTo-Json -Compress -Depth 4),
    (New-Object System.Text.UTF8Encoding($false)))

$modePath = Join-Path $PSScriptRoot 'runtime_mode.txt'
$mode = if (Test-Path $modePath) { (Get-Content $modePath -Raw).Trim() } else { 'normal' }
if ($mode -eq 'conflict') {
    [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
        '","status":"Conflict","action":"None","detail":"端口被未知程序占用"}}')
    exit 0
}

$statePath = Join-Path $PSScriptRoot 'runtime_state.txt'
$state = if (Test-Path $statePath) { (Get-Content $statePath -Raw).Trim() } else { 'stopped' }
function Set-State([string]$Value) {
    [IO.File]::WriteAllText($statePath, $Value,
        (New-Object System.Text.UTF8Encoding($false)))
}
if ($action -eq 'start') {
    if ($state -eq 'running') {
        [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
            '","status":"AlreadyRunning","action":"None"}}')
    } else {
        Set-State 'running'
        [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
            '","status":"Started","action":"Start"}}')
    }
    exit 0
}
if ($action -eq 'stop') {
    if ($state -eq 'stopped') {
        [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
            '","status":"AlreadyStopped","action":"None"}}')
    } else {
        Set-State 'stopped'
        [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
            '","status":"Stopped","action":"Stop","terminated_process_ids":[4242]}}')
    }
    exit 0
}
if ($action -eq 'restart') {
    Set-State 'running'
    [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
        '","status":"Restarted","action":"Restart","terminated_process_ids":[4242]}}')
    exit 0
}
[Console]::Out.Write('{"ok":false,"error":"unsupported","detail":"unexpected action"}')
exit 1
"#;

const MODE_WRAPPER: &str = r#"
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$action = ''
$key = ''
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq '-Action') { $action = $args[$i + 1] }
    if ($args[$i] -eq '-Key') { $key = $args[$i + 1] }
}
$record = [ordered]@{
    script = $PSCommandPath
    args = @($args)
    cwd = (Get-Location).Path
}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'wrapper_call.json'),
    ($record | ConvertTo-Json -Compress -Depth 4),
    (New-Object System.Text.UTF8Encoding($false)))

$modePath = Join-Path $PSScriptRoot 'runtime_mode.txt'
$mode = if (Test-Path $modePath) { (Get-Content $modePath -Raw).Trim() } else { 'normal' }
if ($mode -eq 'stop_refused') {
    [Console]::Out.Write('{"ok":false,"error":"stop_scope_unconfirmed","detail":"进程归属无法确认（PID 990107），未终止任何进程"}')
    exit 1
}
if ($mode -eq 'garbage') {
    [Console]::Out.Write('not json')
    exit 1
}
if ($mode -eq 'slow') {
    Start-Sleep -Seconds 5
    [Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
        '","status":"Started","action":"Start"}}')
    exit 0
}
if ($mode -eq 'runtime_error') {
    [Console]::Out.Write('{"ok":false,"error":"weird"}')
    exit 0
}
[Console]::Out.Write('{"ok":true,"result":{"key":"' + $key +
    '","status":"Started","action":"Start"}}')
exit 0
"#;

fn fake_repo(temp: &TempDataDir, wrapper: &str) -> PathBuf {
    let root = temp.path().join("repo");
    std::fs::create_dir_all(&root).expect("fake repo root");
    std::fs::write(root.join("invoke-console-runtime-json.ps1"), wrapper)
        .expect("fake PowerShell entry");
    root
}

fn start_local_console(temp: &TempDataDir, repo: &Path) -> Console {
    let mut config = base_config(temp);
    config.role = Role::Local;
    config.port = 8001;
    config.gateway_port = 8000;
    // Python must not be involved in the service action path.
    config.python_exe = temp.path().join("missing-python");
    config.repo_root = repo.to_path_buf();
    let server =
        ConsoleServer::start_with_runtime(config, stopped_runtime()).expect("start local console");
    Console::from_server(server)
}

fn request(console: &Console, method: &str, target: &str, body: Option<Value>) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    request.method = method.to_string();
    if let Some(body) = body {
        request = request.json_body(body.to_string());
    }
    client_request(console.addr, &request).expect("request")
}

fn wait_terminal(console: &Console, operation: &Value) -> Value {
    let op_id = operation["id"].as_str().expect("operation id").to_string();
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let fetched = request(
            console,
            "GET",
            &format!("/api/console/operations/{op_id}"),
            None,
        );
        assert_eq!(fetched.status, 200);
        let operation = fetched.json()["operation"].clone();
        if matches!(
            operation["state"].as_str(),
            Some("succeeded" | "failed" | "cancelled" | "interrupted")
        ) {
            return operation;
        }
        assert!(
            Instant::now() < deadline,
            "operation never finished: {operation}"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn submit(console: &Console, action: &str, target: &str, params: Value) -> ClientResponse {
    request(
        console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": action, "target": target, "params": params})),
    )
}

#[test]
fn the_service_resource_lock_blocks_a_second_action_before_the_wrapper_runs() {
    let temp = temp();
    let repo = fake_repo(&temp, FAKE_WRAPPER);
    let lock_dir = temp.path().join("service-locks");
    let held = ResourceLock::acquire(
        &lock_dir,
        "service:chat2api",
        "test-holder",
        Duration::from_secs(1),
        &AtomicBool::new(false),
    )
    .expect("hold the service lock");
    let executor = Arc::new(PowerShellServiceExecutor::new(
        "pwsh",
        repo.join("invoke-console-runtime-json.ps1"),
        &repo,
    ));
    let manager = Arc::new(
        OperationManager::with_timeout(
            temp.path().join("service-state.sqlite3"),
            lock_dir,
            local_registry(),
            executor,
            Duration::from_millis(300),
        )
        .expect("operation manager"),
    );

    let (_, operation) = manager
        .submit("service.start", "chat2api", None, None)
        .expect("submit");
    let operation = wait_manager_terminal(&manager, &operation);
    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(operation["error"]["code"], json!("resource_busy_timeout"));
    assert_eq!(
        operation["error"]["message"],
        json!("resource lock wait exceeded 0.3s")
    );
    assert!(
        !repo.join("wrapper_call.json").exists(),
        "the wrapper must not run while the service lock is held"
    );
    drop(held);
}

#[test]
fn a_refused_stop_scope_is_a_runtime_unavailable_failure() {
    let temp = temp();
    let repo = fake_repo(&temp, MODE_WRAPPER);
    std::fs::write(repo.join("runtime_mode.txt"), "stop_refused").expect("refused mode");
    let console = start_local_console(&temp, &repo);

    let created = submit(&console, "service.stop", "chat2api", json!({}));
    assert_eq!(created.status, 201);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(operation["result"], Value::Null);
    assert_eq!(operation["error"]["code"], json!("runtime_unavailable"));
    let message = operation["error"]["message"].as_str().unwrap_or_default();
    assert!(
        message.contains("进程归属无法确认") && message.contains("990107"),
        "{operation}"
    );
}

#[test]
fn a_non_json_wrapper_envelope_is_runtime_unavailable_with_the_exit_code() {
    let temp = temp();
    let repo = fake_repo(&temp, MODE_WRAPPER);
    std::fs::write(repo.join("runtime_mode.txt"), "garbage").expect("garbage mode");
    let console = start_local_console(&temp, &repo);

    let created = submit(&console, "service.start", "chat2api", json!({}));
    assert_eq!(created.status, 201);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(operation["error"]["code"], json!("runtime_unavailable"));
    assert_eq!(
        operation["error"]["message"],
        json!("console runtime wrapper returned non-JSON output (rc=1)")
    );
}

#[test]
fn an_ok_false_envelope_from_a_zero_exit_is_runtime_error() {
    let temp = temp();
    let repo = fake_repo(&temp, MODE_WRAPPER);
    std::fs::write(repo.join("runtime_mode.txt"), "runtime_error").expect("runtime error mode");
    let console = start_local_console(&temp, &repo);

    let created = submit(&console, "service.start", "chat2api", json!({}));
    assert_eq!(created.status, 201);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(operation["error"]["code"], json!("runtime_error"));
    assert_eq!(operation["error"]["message"], json!("weird"));
}

fn manager_with(
    temp: &TempDataDir,
    repo: &Path,
    powershell: &str,
    timeout: Duration,
) -> Arc<OperationManager> {
    let executor = Arc::new(
        PowerShellServiceExecutor::new(
            powershell,
            repo.join("invoke-console-runtime-json.ps1"),
            repo,
        )
        .with_timeouts(timeout, timeout),
    );
    Arc::new(
        OperationManager::new(
            temp.path().join("service-state.sqlite3"),
            temp.path().join("service-locks"),
            local_registry(),
            executor,
        )
        .expect("operation manager"),
    )
}

fn wait_manager_terminal(manager: &OperationManager, operation: &Value) -> Value {
    let op_id = operation["id"].as_str().expect("operation id").to_string();
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let operation = manager.get(&op_id).expect("operation row").expect("row");
        if matches!(
            operation["state"].as_str(),
            Some("succeeded" | "failed" | "cancelled" | "interrupted")
        ) {
            return operation;
        }
        assert!(
            Instant::now() < deadline,
            "operation never finished: {operation}"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn a_wrapper_timeout_is_runtime_unavailable() {
    let temp = temp();
    let repo = fake_repo(&temp, MODE_WRAPPER);
    std::fs::write(repo.join("runtime_mode.txt"), "slow").expect("slow mode");
    let manager = manager_with(&temp, &repo, "pwsh", Duration::from_secs(1));

    let (_, operation) = manager
        .submit("service.start", "chat2api", None, None)
        .expect("submit");
    let operation = wait_manager_terminal(&manager, &operation);
    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(operation["error"]["code"], json!("runtime_unavailable"));
    assert_eq!(
        operation["error"]["message"],
        json!("console runtime wrapper timed out after 1s")
    );
}

#[test]
fn a_missing_powershell_executable_is_runtime_unavailable() {
    let temp = temp();
    let repo = fake_repo(&temp, MODE_WRAPPER);
    let manager = manager_with(
        &temp,
        &repo,
        temp.path().join("missing-pwsh").to_str().expect("path"),
        Duration::from_secs(5),
    );

    let (_, operation) = manager
        .submit("service.start", "chat2api", None, None)
        .expect("submit");
    let operation = wait_manager_terminal(&manager, &operation);
    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(operation["error"]["code"], json!("runtime_unavailable"));
    assert!(
        operation["error"]["message"]
            .as_str()
            .unwrap_or_default()
            .contains("cannot run console runtime wrapper"),
        "{operation}"
    );
}

const REAL_RUNTIME_OVERRIDE: &str = r#"
# Test fixture: the real runtime keeps its identity/start/stop implementation,
# only the managed descriptor points at an isolated dummy service.
$script:GatewayServiceDescriptors = [ordered]@{
    'chat2api' = [pscustomobject]@{
        Key = 'chat2api'; DisplayName = 'fixture'; Port = __PORT__
        HealthPath = '/health'; StartOrder = 1; OpenUrl = $null
        StartAction = 'HiddenBatch'; StopAction = 'Port'
        StartPath = 'fake-start.cmd'; StartArguments = ''
        StartWorkingDirectory = '.'; StartTimeoutSeconds = 10
        StopStrategy = 'Port'
        StopProcessName = 'pwsh.exe'
        StopCommandLinePattern = '(^|\s)dummy-server\.ps1(\s|$)'
        StopInstanceScript = 'dummy-server.ps1'
        StopIdentities = @([pscustomobject]@{
            ProcessName = 'pwsh.exe'
            CommandLinePattern = '(^|\s)dummy-server\.ps1(\s|$)'
            InstanceScript = 'dummy-server.ps1'
            ForeignArgumentPatterns = @()
        })
        OwnerProcessName = 'pwsh.exe'
        OwnerCommandLinePattern = 'dummy-server\.ps1'
        OwnerIdentities = @([pscustomobject]@{
            ProcessName = 'pwsh.exe'
            CommandLinePattern = 'dummy-server\.ps1'
        })
        DesiredStateTracked = $false
    }
}
"#;

const DUMMY_SERVER: &str = r#"
param([int]$Port, [string]$PidFile = 'dummy_pid.txt')
$PID | Out-File (Join-Path $PSScriptRoot $PidFile) -Encoding ascii
$listener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback, $Port)
$listener.Start(10)
while ($true) {
    $client = $listener.AcceptTcpClient()
    try {
        $stream = $client.GetStream()
        $stream.ReadTimeout = 2000
        $buffer = [byte[]]::new(4096)
        [void]$stream.Read($buffer, 0, $buffer.Length)
        $response = "HTTP/1.1 200 OK`r`nContent-Length: 2`r`nConnection: close`r`n`r`n{}"
        $bytes = [Text.Encoding]::ASCII.GetBytes($response)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
    } finally {
        $client.Dispose()
    }
}
"#;

fn real_runtime_repo(temp: &TempDataDir, port: u16) -> PathBuf {
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let root = temp.path().join("real-runtime");
    std::fs::create_dir_all(root.join("lib")).expect("fake runtime lib");
    for (relative, from) in [
        (
            "invoke-console-runtime-json.ps1",
            "invoke-console-runtime-json.ps1",
        ),
        (
            "lib/console-runtime-bridge.ps1",
            "lib/console-runtime-bridge.ps1",
        ),
        ("lib/gateway-runtime.ps1", "lib/gateway-runtime.ps1"),
    ] {
        std::fs::copy(source.join(from), root.join(relative)).expect("copy real runtime");
    }
    let mut runtime = std::fs::OpenOptions::new()
        .append(true)
        .open(root.join("lib/gateway-runtime.ps1"))
        .expect("open runtime copy");
    use std::io::Write;
    runtime
        .write_all(
            REAL_RUNTIME_OVERRIDE
                .replace("__PORT__", &port.to_string())
                .as_bytes(),
        )
        .expect("append descriptor override");
    std::fs::write(root.join("dummy-server.ps1"), DUMMY_SERVER).expect("dummy server");
    std::fs::write(
        root.join("foreign-server.ps1"),
        DUMMY_SERVER.replace("'dummy_pid.txt'", "'foreign_pid.txt'"),
    )
    .expect("foreign server");
    std::fs::write(
        root.join("fake-start.cmd"),
        format!("@echo off\r\nstart \"\" /b pwsh -NoProfile -File \"%~dp0dummy-server.ps1\" -Port {port}\r\n"),
    )
    .expect("start script");
    root
}

fn free_port() -> u16 {
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).expect("bind ephemeral");
    listener.local_addr().expect("ephemeral addr").port()
}

struct ProcessCleanup {
    root: PathBuf,
}

impl Drop for ProcessCleanup {
    fn drop(&mut self) {
        let script = format!(
            "Get-CimInstance Win32_Process | Where-Object {{ $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains('{}') }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}",
            self.root.display().to_string().replace('\'', "''")
        );
        let _ = std::process::Command::new("pwsh")
            .args(["-NoProfile", "-Command", &script])
            .output();
    }
}

#[cfg(windows)]
#[test]
fn the_real_powershell_entry_covers_running_stopped_repeated_and_conflict() {
    let temp = temp();
    let port = free_port();
    let repo = real_runtime_repo(&temp, port);
    let _cleanup = ProcessCleanup { root: repo.clone() };
    let console = start_local_console(&temp, &repo);

    let started = submit(&console, "service.start", "chat2api", json!({}));
    assert_eq!(started.status, 201);
    let operation = wait_terminal(&console, &started.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        operation["result"],
        json!({"key": "chat2api", "status": "Started", "action": "Start"})
    );
    let dummy_pid: i64 = std::fs::read_to_string(repo.join("dummy_pid.txt"))
        .expect("dummy pid")
        .trim()
        .parse()
        .expect("dummy pid number");

    let repeated = submit(&console, "service.start", "chat2api", json!({}));
    assert_eq!(repeated.status, 201);
    let operation = wait_terminal(&console, &repeated.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        operation["result"],
        json!({"key": "chat2api", "status": "AlreadyRunning", "action": "None"})
    );

    let stopped = submit(&console, "service.stop", "chat2api", json!({}));
    assert_eq!(stopped.status, 201);
    let operation = wait_terminal(&console, &stopped.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(operation["result"]["status"], json!("Stopped"));
    assert_eq!(
        operation["result"]["terminated_process_ids"],
        json!([dummy_pid])
    );

    let repeated = submit(&console, "service.stop", "chat2api", json!({}));
    assert_eq!(repeated.status, 201);
    let operation = wait_terminal(&console, &repeated.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        operation["result"],
        json!({"key": "chat2api", "status": "AlreadyStopped", "action": "None"})
    );

    // Port conflict: a foreign process owns the managed port. The runtime
    // must report Conflict and terminate/launch nothing.
    let foreign_path = repo.join("foreign-server.ps1");
    let port_text = port.to_string();
    let mut foreign = std::process::Command::new("pwsh")
        .args([
            "-NoProfile",
            "-File",
            foreign_path.to_str().expect("foreign path"),
            "-Port",
            port_text.as_str(),
        ])
        .spawn()
        .expect("foreign listener");
    let deadline = Instant::now() + Duration::from_secs(10);
    while !repo.join("foreign_pid.txt").exists() {
        assert!(Instant::now() < deadline, "foreign listener never started");
        std::thread::sleep(Duration::from_millis(20));
    }

    for action in ["service.start", "service.stop"] {
        let created = submit(&console, action, "chat2api", json!({}));
        assert_eq!(created.status, 201, "{action}");
        let operation = wait_terminal(&console, &created.json()["operation"]);
        assert_eq!(
            operation["state"],
            json!("succeeded"),
            "{action}: {operation}"
        );
        assert_eq!(
            operation["result"],
            json!({
                "key": "chat2api",
                "status": "Conflict",
                "action": "None",
                "detail": "端口被未知程序占用",
            }),
            "{action}"
        );
        assert!(
            operation["result"].get("terminated_process_ids").is_none(),
            "{action} must not report a termination it did not perform"
        );
    }
    assert!(
        foreign.try_wait().expect("foreign status").is_none(),
        "the foreign listener must survive the conflict verdict"
    );
}

#[test]
fn repeated_service_triggers_and_port_conflicts_keep_the_runtime_verdicts() {
    let temp = temp();
    let repo = fake_repo(&temp, STATEFUL_WRAPPER);
    let console = start_local_console(&temp, &repo);

    let first = submit(&console, "service.start", "chat2api", json!({}));
    assert_eq!(first.status, 201);
    let operation = wait_terminal(&console, &first.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(operation["result"]["status"], json!("Started"));

    let repeated = submit(&console, "service.start", "chat2api", json!({}));
    assert_eq!(repeated.status, 201);
    let operation = wait_terminal(&console, &repeated.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        operation["result"],
        json!({"key": "chat2api", "status": "AlreadyRunning", "action": "None"})
    );

    let stopped = submit(&console, "service.stop", "chat2api", json!({}));
    assert_eq!(stopped.status, 201);
    let operation = wait_terminal(&console, &stopped.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(operation["result"]["status"], json!("Stopped"));
    assert_eq!(operation["result"]["terminated_process_ids"], json!([4242]));

    let repeated = submit(&console, "service.stop", "chat2api", json!({}));
    assert_eq!(repeated.status, 201);
    let operation = wait_terminal(&console, &repeated.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        operation["result"],
        json!({"key": "chat2api", "status": "AlreadyStopped", "action": "None"})
    );

    std::fs::write(repo.join("runtime_mode.txt"), "conflict").expect("conflict mode");
    for action in ["service.start", "service.stop"] {
        let created = submit(&console, action, "chat2api", json!({}));
        assert_eq!(created.status, 201, "{action}");
        let operation = wait_terminal(&console, &created.json()["operation"]);
        assert_eq!(
            operation["state"],
            json!("succeeded"),
            "{action}: {operation}"
        );
        assert_eq!(
            operation["result"],
            json!({
                "key": "chat2api",
                "status": "Conflict",
                "action": "None",
                "detail": "端口被未知程序占用",
            }),
            "{action}"
        );
    }
}

#[test]
fn service_stop_and_restart_keep_the_runtime_result_shapes() {
    let temp = temp();
    let repo = fake_repo(&temp, FAKE_WRAPPER);
    let console = start_local_console(&temp, &repo);

    let stopped = submit(&console, "service.stop", "chat2api", json!({}));
    assert_eq!(stopped.status, 201);
    let operation = wait_terminal(&console, &stopped.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        operation["result"],
        json!({
            "key": "chat2api",
            "status": "Stopped",
            "action": "Stop",
            "terminated_process_ids": [4242],
        })
    );

    let restarted = submit(&console, "service.restart", "chat2api", json!({}));
    assert_eq!(restarted.status, 201);
    let operation = wait_terminal(&console, &restarted.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        operation["result"],
        json!({
            "key": "chat2api",
            "status": "Restarted",
            "action": "Restart",
            "terminated_process_ids": [4242],
        })
    );
}

#[test]
fn service_start_uses_the_powershell_runtime_entry_without_python() {
    let temp = temp();
    let repo = fake_repo(&temp, FAKE_WRAPPER);
    let console = start_local_console(&temp, &repo);

    let created = submit(&console, "service.start", "chat2api", json!({}));
    assert_eq!(created.status, 201);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        operation["result"],
        json!({"key": "chat2api", "status": "Started", "action": "start"})
    );

    let call: Value = serde_json::from_str(
        &std::fs::read_to_string(repo.join("wrapper_call.json")).expect("wrapper call record"),
    )
    .expect("wrapper call json");
    let args: Vec<&str> = call["args"]
        .as_array()
        .expect("args")
        .iter()
        .map(|value| value.as_str().expect("arg string"))
        .collect();
    let wrapper = repo.join("invoke-console-runtime-json.ps1");
    assert_eq!(
        call["script"].as_str(),
        Some(wrapper.to_str().expect("wrapper path"))
    );
    assert_eq!(
        args,
        vec!["-Action", "start", "-Key", "chat2api", "-CallerHoldsLock",]
    );
    assert_eq!(
        call["cwd"].as_str(),
        Some(repo.to_str().expect("repo path")),
        "the wrapper runs from the repo root"
    );
}
