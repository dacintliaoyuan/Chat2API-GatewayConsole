use std::cell::RefCell;
use std::fs;
use std::process::Command;

use serde_json::{json, Value};
use um_ops::ingress::check_ingress;
use um_ops::public_probe::{
    addresses_from_answers, aggregate_dns, bounded_check_deadline, check_exit_code,
    check_public_connection, negative_ttl, public_host, public_origin, public_source,
    summarize_check, DnsResolution, DnsSource, ProbeFailure, ProbeTransport, TlsCheck,
};

mod support;

use support::{env_map, fixed_clock, fixture, strip_timing, temp};

fn expected(path: &[&str]) -> Value {
    let mut value = fixture();
    for key in path {
        value = value.get(*key).cloned().expect("fixture key");
    }
    value
}

struct FixtureTransport {
    resolution: RefCell<Option<Result<DnsResolution, ProbeFailure>>>,
    tls: RefCell<Vec<TlsCheck>>,
    calls: RefCell<usize>,
}

impl FixtureTransport {
    fn from_case(case: &Value) -> Self {
        let reason = case.get("reason").and_then(Value::as_str);
        if reason == Some("public_url_unavailable") {
            return Self {
                resolution: RefCell::new(None),
                tls: RefCell::new(Vec::new()),
                calls: RefCell::new(0),
            };
        }
        let dns = case.get("dns").cloned().expect("dns detail");
        let state = dns.get("state").and_then(Value::as_str);
        let resolution = if reason == Some("dns_timeout") && state == Some("timeout") {
            Err(ProbeFailure::DnsTimeout(dns))
        } else {
            Ok(DnsResolution {
                addresses: dns
                    .get("addresses")
                    .and_then(Value::as_array)
                    .expect("dns addresses")
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect(),
                reason: dns
                    .get("reason")
                    .and_then(Value::as_str)
                    .expect("dns reason")
                    .to_string(),
                detail: dns,
            })
        };
        let tls = case
            .get("entries")
            .and_then(Value::as_array)
            .expect("entries")
            .iter()
            .map(|entry| TlsCheck {
                state: entry
                    .get("state")
                    .and_then(Value::as_str)
                    .expect("state")
                    .to_string(),
                tls: entry
                    .get("tls")
                    .and_then(Value::as_str)
                    .expect("tls")
                    .to_string(),
                certificate: entry
                    .get("certificate")
                    .and_then(Value::as_str)
                    .expect("certificate")
                    .to_string(),
                reason: entry
                    .get("reason")
                    .and_then(Value::as_str)
                    .expect("reason")
                    .to_string(),
            })
            .collect();
        Self {
            resolution: RefCell::new(Some(resolution)),
            tls: RefCell::new(tls),
            calls: RefCell::new(0),
        }
    }
}

impl ProbeTransport for FixtureTransport {
    fn resolve_public_addresses(
        &self,
        _host: &str,
        _deadline_ms: f64,
    ) -> Result<DnsResolution, ProbeFailure> {
        *self.calls.borrow_mut() += 1;
        self.resolution
            .borrow_mut()
            .take()
            .expect("unexpected DNS call")
    }

    fn check_tls(
        &self,
        _host: &str,
        _address: &str,
        _deadline_ms: f64,
    ) -> Result<TlsCheck, ProbeFailure> {
        *self.calls.borrow_mut() += 1;
        let mut tls = self.tls.borrow_mut();
        if tls.is_empty() {
            panic!("unexpected TLS call");
        }
        Ok(tls.remove(0))
    }
}

#[test]
fn the_ingress_check_matches_the_python_checker() {
    let cases = expected(&["cloudflare", "ingress"]);
    for (name, case) in cases.as_object().expect("ingress cases") {
        let temp = temp();
        let path = temp.path().join(format!("{name}.yml"));
        if matches!(name.as_str(), "invalidYaml" | "unreadable") {
            if name == "invalidYaml" {
                fs::write(&path, "ingress: [\n").expect("invalid yaml");
            }
            let result = check_ingress(&path, "fixture.example");
            assert_eq!(result["ok"], case["ok"], "{name}");
            assert_eq!(result["rules"], case["rules"], "{name}");
            let problems = result["problems"].as_array().expect("problems");
            assert_eq!(problems.len(), 1, "{name}");
            assert!(
                problems[0]
                    .as_str()
                    .expect("problem")
                    .starts_with(case["problemPrefix"].as_str().expect("prefix")),
                "{name}: {problems:?}"
            );
            continue;
        }
        fs::write(&path, case["config"].as_str().expect("config text")).expect("ingress config");
        let host = case["expectHost"].as_str().expect("expect host");
        assert_eq!(check_ingress(&path, host), case["result"], "{name}");
    }
}

#[test]
fn public_host_and_origin_match_the_python_parser() {
    let cases = expected(&["cloudflare", "publicHost"]);
    let unconfigured = env_map(&[]);
    for (url, value) in cases["unconfigured"]["accepted"]
        .as_object()
        .expect("accepted")
    {
        assert_eq!(
            public_host(Some(url), &unconfigured),
            value.as_str().map(str::to_owned),
            "{url}"
        );
        assert_eq!(
            public_source(value.as_str().expect("host"), &unconfigured),
            "tailscale"
        );
    }
    for case in cases["unconfigured"]["rejected"]
        .as_array()
        .expect("rejected")
    {
        assert_eq!(
            public_host(case["input"].as_str(), &unconfigured),
            None,
            "{:?}",
            case["input"]
        );
    }
    let configured = env_map(&[("CONSOLE_PUBLIC_ORIGIN", "https://fixture.tail-test.ts.net")]);
    assert_eq!(
        json!(public_origin(&configured)),
        cases["configured"]["publicOrigin"]
    );
    assert_eq!(
        public_host(Some("https://fixture.tail-test.ts.net"), &configured),
        Some("fixture.tail-test.ts.net".to_string())
    );
    assert_eq!(
        public_source("fixture.tail-test.ts.net", &configured),
        "configured"
    );
    assert_eq!(
        public_host(Some("https://other.tail-test.ts.net"), &configured),
        None
    );
    let invalid = env_map(&[("CONSOLE_PUBLIC_ORIGIN", "http://fixture.tail-test.ts.net")]);
    assert_eq!(
        json!(public_origin(&invalid)),
        cases["invalidConfigured"]["publicOrigin"]
    );
    assert_eq!(
        public_host(Some("https://fixture.tail-test.ts.net"), &invalid),
        None
    );
    assert_eq!(
        json!(public_origin(&env_map(&[]))),
        cases["unconfiguredOrigin"]
    );
}

#[test]
fn the_dns_aggregate_matches_the_python_state_machine() {
    let cases = expected(&["cloudflare", "dnsSources"]);
    let clock = fixed_clock();
    for (name, sources) in cases.as_object().expect("dns sources") {
        let sources: Vec<DnsSource> = sources
            .as_array()
            .expect("sources")
            .iter()
            .map(|source| DnsSource::from_json(source).expect("dns source"))
            .collect();
        assert_eq!(
            strip_timing(&aggregate_dns(&sources, 0.0, &clock)),
            expected(&["cloudflare", "dnsAggregate", name]),
            "{name}"
        );
    }
}

#[test]
fn dns_answers_and_negative_ttl_match_python() {
    let inputs = expected(&["cloudflare", "dnsAnswersInputs"]);
    for (name, data) in inputs.as_object().expect("answers") {
        let (addresses, ttl, truncated) = addresses_from_answers(data);
        assert_eq!(
            json!({"addresses": addresses, "ttl": ttl, "truncated": truncated}),
            expected(&["cloudflare", "dnsAnswers", name]),
            "{name}"
        );
    }
    let inputs = expected(&["cloudflare", "negativeTtlInputs"]);
    for (name, data) in inputs.as_object().expect("negative ttl inputs") {
        assert_eq!(
            json!(negative_ttl(data)),
            expected(&["cloudflare", "negativeTtl", name]),
            "{name}"
        );
    }
}

#[test]
fn the_check_summary_and_exit_codes_match_python() {
    let cases = expected(&["cloudflare", "summarizeInputs"]);
    for (name, entries) in cases.as_object().expect("summarize inputs") {
        let mut result = expected(&["cloudflare", "summarize", name]);
        result["entries"] = entries.clone();
        summarize_check(&mut result);
        assert_eq!(
            strip_timing(&result),
            expected(&["cloudflare", "summarize", name]),
            "{name}"
        );
    }
    assert_eq!(check_exit_code("ok"), 0);
    assert_eq!(check_exit_code("not_checked"), 2);
    assert_eq!(check_exit_code("all_failed"), 1);
    assert_eq!(check_exit_code("degraded"), 1);
    let deadline = expected(&["cloudflare", "deadline"]);
    assert_eq!(json!(bounded_check_deadline(None)), deadline["default"]);
    assert_eq!(json!(bounded_check_deadline(Some(30.0))), deadline["valid"]);
    assert_eq!(
        json!(bounded_check_deadline(Some(0.1))),
        deadline["tooSmall"]
    );
    assert_eq!(
        json!(bounded_check_deadline(Some(61.0))),
        deadline["tooLarge"]
    );
    assert_eq!(
        json!(bounded_check_deadline(Some(f64::NAN))),
        deadline["nan"]
    );
}

#[test]
fn the_public_check_matches_python_with_injected_dns_and_tls() {
    let cases = expected(&["cloudflare", "check"]);
    let clock = fixed_clock();
    let env = env_map(&[]);
    for (name, case) in cases.as_object().expect("check cases") {
        let transport = FixtureTransport::from_case(case);
        let url = if name == "invalidUrl" {
            None
        } else {
            Some("https://fixture.tail-test.ts.net")
        };
        let result = check_public_connection(url, 15.0, &transport, &clock, &env);
        assert_eq!(strip_timing(&result), strip_timing(case), "{name}");
        if name == "invalidUrl" {
            assert_eq!(*transport.calls.borrow(), 0);
        }
    }
}

#[test]
fn the_public_probe_is_forwarded_to_the_python_worker() {
    let temp = temp();
    let script = temp.path().join("fake_public_probe.py");
    fs::write(
        &script,
        "import json, sys\n\
print(json.dumps({'forwarded': sys.argv[1:]}, sort_keys=True))\n\
sys.exit(2)\n",
    )
    .expect("fake probe");
    let output = Command::new(env!("CARGO_BIN_EXE_um-ops"))
        .args([
            "--python",
            "python",
            "--public-probe",
            script.to_str().expect("script"),
            "cloudflare",
            "probe",
            "--public-url",
            "https://fixture.tail-test.ts.net",
            "--deadline-s",
            "15",
        ])
        .output()
        .expect("probe");
    assert_eq!(output.status.code(), Some(2));
    let body: Value = serde_json::from_slice(&output.stdout).expect("probe json");
    let forwarded = body["forwarded"].as_array().expect("forwarded");
    assert_eq!(forwarded[0], "--check-connection");
    assert!(forwarded
        .iter()
        .any(|value| value == "https://fixture.tail-test.ts.net"));
    assert!(forwarded.iter().any(|value| value == "--deadline-s"));
}
