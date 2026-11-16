use std::fs;
use std::process::Command;

use serde_json::Value;
use um_ops::deploy::{
    check_deployment_assembly, deployment_assembly_problems, deployment_paths, DeploymentPaths,
};

mod support;

use support::{env_map, fixture, replace_tmp, temp};

fn expected(path: &[&str]) -> Value {
    let mut value = fixture();
    for key in path {
        value = value.get(*key).cloned().expect("fixture key");
    }
    value
}

#[test]
fn the_deployment_assembly_check_matches_python() {
    let cases = expected(&["deploy", "assembly"]);
    for (name, role) in [
        ("consoleOk", "console"),
        ("brokerOk", "broker"),
        ("consoleMissing", "console"),
        ("brokerMissing", "broker"),
        ("unknownRole", "unknown"),
    ] {
        let unit = cases["unit"].as_str().expect("unit");
        let env = cases["env"].as_str().expect("env");
        let broker_unit = cases["brokerUnit"].as_str().expect("broker unit");
        let broker_env = cases["brokerEnv"].as_str().expect("broker env");
        let (unit_text, env_text) = match name {
            "consoleOk" => (unit, format!("{env}\nUM_SHARE_UID=1000\n")),
            "brokerOk" => (broker_unit, format!("{broker_env}\nUM_SHARE_UID=1000\n")),
            "consoleMissing" => ("", String::new()),
            "brokerMissing" => ("", String::new()),
            _ => (unit, env.to_string()),
        };
        assert_eq!(
            serde_json::json!(deployment_assembly_problems(role, unit_text, &env_text)
                .expect("assembly problems")),
            cases[name],
            "{name}"
        );
    }
}

#[test]
fn the_deployment_paths_match_python() {
    let cases = expected(&["deploy", "paths"]);
    let temp = temp();
    let root = temp.path();
    let paths = DeploymentPaths {
        current: root.join("paths").join("current").display().to_string(),
        etc: root.join("paths").join("etc").display().to_string(),
        var: root.join("paths").join("var").display().to_string(),
        console: root.join("paths").join("console").display().to_string(),
    };
    for field in ["current", "etc", "var", "console"] {
        fs::create_dir_all(root.join("paths").join(field)).expect("layout dir");
    }
    let empty = env_map(&[]);
    assert_eq!(
        replace_tmp(&deployment_paths(&paths, &empty).expect("paths"), root),
        replace_tmp(&cases["withoutConsoleEnv"], root)
    );
    let console_env = root.join("paths").join("etc").join("console.env");
    fs::write(
        &console_env,
        format!(
            "CHAT2API_SHARE_SECRET_KEY_FILE={}\n",
            root.join("paths").join("share-secret.key").display()
        ),
    )
    .expect("console.env");
    assert_eq!(
        replace_tmp(&deployment_paths(&paths, &empty).expect("paths"), root),
        replace_tmp(&cases["withConsoleEnv"], root)
    );
    let overridden = env_map(&[(
        "CHAT2API_SHARE_SECRET_KEY_FILE",
        "/override/share-secret.key",
    )]);
    assert_eq!(
        replace_tmp(&deployment_paths(&paths, &overridden).expect("paths"), root),
        replace_tmp(&cases["withOverride"], root)
    );
    fs::remove_file(&console_env).expect("remove console.env");
    fs::create_dir(&console_env).expect("broken console.env");
    let error = deployment_paths(&paths, &empty).expect_err("broken console.env");
    assert!(error
        .message()
        .starts_with("console environment file unreadable:"));
}

#[test]
fn check_deployment_assembly_reads_the_installed_pair() {
    let cases = expected(&["deploy", "assembly"]);
    let temp = temp();
    let unit = temp.path().join("console.service");
    let env = temp.path().join("console.env");
    fs::write(&unit, cases["unit"].as_str().expect("unit")).expect("unit");
    fs::write(
        &env,
        format!(
            "{}\nUM_SHARE_UID=1000\n",
            cases["env"].as_str().expect("env")
        ),
    )
    .expect("env");
    let result = check_deployment_assembly("console", &unit, &env).expect("assembly");
    assert_eq!(result["ok"], true);
    assert_eq!(result["role"], "console");
    assert_eq!(result["unit"], unit.display().to_string());
    assert_eq!(result["env"], env.display().to_string());

    let missing_unit = temp.path().join("missing.service");
    let error =
        check_deployment_assembly("console", &missing_unit, &env).expect_err("missing unit");
    assert!(error.message().starts_with("unit file unreadable:"));
    let missing_env = temp.path().join("missing.env");
    let error = check_deployment_assembly("console", &unit, &missing_env).expect_err("missing env");
    assert!(error.message().starts_with("environment file unreadable:"));

    let empty_unit = temp.path().join("empty.service");
    let empty_env = temp.path().join("empty.env");
    fs::write(&empty_unit, "").expect("empty unit");
    fs::write(&empty_env, "").expect("empty env");
    let error =
        check_deployment_assembly("console", &empty_unit, &empty_env).expect_err("drifting");
    let problems = cases["consoleMissing"]
        .as_array()
        .expect("problems")
        .iter()
        .filter_map(Value::as_str)
        .collect::<Vec<_>>()
        .join("; ");
    assert_eq!(error.message(), problems);
}

fn binary() -> Command {
    Command::new(env!("CARGO_BIN_EXE_um-ops"))
}

fn fake_backup_restore(root: &std::path::Path) -> std::path::PathBuf {
    let script = root.join("fake_backup_restore.py");
    fs::write(
        &script,
        r#"import json, os, sys
args = sys.argv[1:]
archive = args[args.index('--archive-out') + 1]
with open(archive, 'wb') as handle:
    handle.write(b'UMCLDBK1-fixture')
if os.name == 'posix':
    os.chmod(archive, 0o600)
print(json.dumps({'ok': True, 'archive': archive, 'args': args}, sort_keys=True))
sys.exit(75 if '--exit-75' in args else 0)
"#,
    )
    .expect("fake script");
    script
}

#[test]
fn backup_and_restore_forward_json_archive_and_exit_code() {
    let temp = temp();
    let root = temp.path();
    let script = fake_backup_restore(root);
    let archive = root.join("fixture.umbak");
    let output = binary()
        .args([
            "--python",
            "python",
            "--backup-restore",
            script.to_str().expect("script"),
            "backup",
            "--store",
            root.join("store.db").to_str().expect("store"),
            "--archive-out",
            archive.to_str().expect("archive"),
        ])
        .output()
        .expect("run backup");
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let body: Value = serde_json::from_slice(&output.stdout).expect("json");
    assert_eq!(body["ok"], true);
    assert_eq!(body["archive"], archive.display().to_string());
    assert!(body["args"]
        .as_array()
        .expect("args")
        .iter()
        .any(|value| value == "--store"));
    assert_eq!(
        fs::read(&archive).expect("archive bytes"),
        b"UMCLDBK1-fixture"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&archive)
                .expect("metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }

    let output = binary()
        .args([
            "--python",
            "python",
            "--backup-restore",
            script.to_str().expect("script"),
            "restore",
            "--archive-out",
            archive.to_str().expect("archive"),
            "--exit-75",
        ])
        .output()
        .expect("run restore");
    assert_eq!(output.status.code(), Some(75));
}

#[test]
fn deployment_assembly_cli_matches_the_python_cli() {
    let temp = temp();
    let root = temp.path();
    let unit = root.join("console.service");
    let env = root.join("console.env");
    let cases = expected(&["deploy", "assembly"]);
    fs::write(&unit, cases["unit"].as_str().expect("unit")).expect("unit");
    fs::write(
        &env,
        format!(
            "{}\nUM_SHARE_UID=1000\n",
            cases["env"].as_str().expect("env")
        ),
    )
    .expect("env");
    let python_cli = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../um-cloud-deploy/backup_restore.py");
    let python = Command::new("python")
        .args([
            python_cli.to_str().expect("cli"),
            "--check-deployment-assembly",
            "console",
            unit.to_str().expect("unit"),
            env.to_str().expect("env"),
        ])
        .output()
        .expect("python assembly");
    let output = binary()
        .args([
            "--python",
            "python",
            "--backup-restore",
            python_cli.to_str().expect("cli"),
            "deploy",
            "assembly",
            "console",
            unit.to_str().expect("unit"),
            env.to_str().expect("env"),
        ])
        .output()
        .expect("rust assembly");
    assert_eq!(output.status.code(), python.status.code());
    let rust_body: Value = serde_json::from_slice(&output.stdout).expect("rust json");
    let python_body: Value = serde_json::from_slice(&python.stdout).expect("python json");
    assert_eq!(rust_body, python_body);

    let output = binary()
        .args([
            "--python",
            "python",
            "--backup-restore",
            python_cli.to_str().expect("cli"),
            "deploy",
            "assembly",
            "console",
            root.join("missing.service").to_str().expect("unit"),
            env.to_str().expect("env"),
        ])
        .output()
        .expect("rust missing assembly");
    assert_eq!(output.status.code(), Some(1));
    let body: Value = serde_json::from_slice(&output.stdout).expect("missing json");
    assert_eq!(body["ok"], false);
    assert_eq!(body["role"], "console");
    assert!(body["problems"]
        .as_array()
        .expect("problems")
        .iter()
        .all(|problem| problem
            .as_str()
            .expect("problem")
            .starts_with("unit file unreadable:")));
}

#[test]
fn update_and_rollback_forward_the_script_contract() {
    let temp = temp();
    let root = temp.path();
    let fake = root.join("fake_deploy.sh");
    fs::write(
        &fake,
        "#!/usr/bin/env bash\necho fake-deploy \"$@\"\nexit 75\n",
    )
    .expect("fake script");
    let output = binary()
        .args([
            "--update-script",
            fake.to_str().expect("fake"),
            "deploy",
            "update",
            "--",
            "--drain-budget",
            "900",
        ])
        .output()
        .expect("update");
    assert_eq!(
        output.status.code(),
        Some(75),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        "fake-deploy --drain-budget 900\n"
    );

    let deploy_root =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../um-cloud-deploy");
    let update = expected(&["deploy", "updateHelp"]);
    let output = binary()
        .args([
            "--update-script",
            deploy_root.join("update.sh").to_str().expect("update"),
            "deploy",
            "update",
            "--help",
        ])
        .output()
        .expect("update help");
    assert_eq!(
        output.status.code(),
        update["code"].as_i64().map(|value| value as i32)
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        update["stdout"].as_str().expect("stdout")
    );

    let rollback = expected(&["deploy", "rollbackHelp"]);
    let output = binary()
        .args([
            "--rollback-script",
            deploy_root.join("rollback.sh").to_str().expect("rollback"),
            "deploy",
            "rollback",
            "--help",
        ])
        .output()
        .expect("rollback help");
    assert_eq!(
        output.status.code(),
        rollback["code"].as_i64().map(|value| value as i32)
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stdout),
        rollback["stdout"].as_str().expect("stdout")
    );

    let output = binary()
        .args([
            "--update-script",
            deploy_root.join("update.sh").to_str().expect("update"),
            "deploy",
            "update",
            "--drain-budget",
            "1",
        ])
        .output()
        .expect("update usage");
    assert_eq!(output.status.code(), Some(64));
    let output = binary()
        .args([
            "--rollback-script",
            deploy_root.join("rollback.sh").to_str().expect("rollback"),
            "deploy",
            "rollback",
            "--drain-budget",
            "1",
        ])
        .output()
        .expect("rollback usage");
    assert_eq!(output.status.code(), Some(64));
}
