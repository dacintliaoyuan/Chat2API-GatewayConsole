use std::fs;

use serde_json::Value;
use um_ops::ledger::{read_share_requests, read_share_usage};
use um_ops::{RequestOptions, UsageOptions};

mod support;

use support::{fixed_clock, fixture, normalize_checked_at, temp};

fn expected(path: &[&str]) -> Value {
    let mut value = fixture();
    for key in path {
        value = value.get(*key).cloned().expect("fixture key");
    }
    value
}

fn ledger_inputs() -> Value {
    fixture()["ledger"]["inputs"].clone()
}

fn write_records(path: &std::path::Path, records: &Value) {
    let mut text = String::new();
    for record in records.as_array().expect("record array") {
        text.push_str(&serde_json::to_string(record).expect("record json"));
        text.push('\n');
    }
    fs::write(path, text).expect("write ledger");
}

fn summary(path: &std::path::Path, options: &UsageOptions) -> Value {
    normalize_checked_at(&read_share_usage(path, options, &fixed_clock()).expect("summary"))
}

fn requests(path: &std::path::Path, options: &RequestOptions) -> Value {
    normalize_checked_at(&read_share_requests(path, options, &fixed_clock()).expect("requests"))
}

fn write_basic_ledger(temp: &um_test_harness::TempDataDir) -> std::path::PathBuf {
    let path = temp.path().join("usage-share-backend.jsonl");
    let inputs = ledger_inputs();
    write_records(&path, &inputs["basicRecords"]);
    let mut text = fs::read_to_string(&path).expect("read ledger");
    text.push_str("{not json\n\n");
    fs::write(&path, text).expect("write bad lines");
    path
}

#[test]
fn the_summary_matches_the_python_reader() {
    let temp = temp();
    let path = write_basic_ledger(&temp);

    assert_eq!(
        summary(&path, &UsageOptions::default()),
        expected(&["ledger", "basic"])
    );
    assert_eq!(
        summary(
            &path,
            &UsageOptions {
                limit: 2,
                ..UsageOptions::default()
            }
        ),
        expected(&["ledger", "limit2"])
    );
    assert_eq!(
        summary(
            &path,
            &UsageOptions {
                limit: 0,
                ..UsageOptions::default()
            }
        ),
        expected(&["ledger", "limit0"])
    );
    assert_eq!(
        summary(
            &path,
            &UsageOptions {
                window: Some("24h".to_string()),
                key_id: Some("key-a".to_string()),
                model: Some("GLM-5.2-NVFP4".to_string()),
                result: Some("success".to_string()),
                ..UsageOptions::default()
            }
        ),
        expected(&["ledger", "filtered"])
    );
    assert_eq!(
        summary(
            &path,
            &UsageOptions {
                window: Some("today".to_string()),
                ..UsageOptions::default()
            }
        ),
        expected(&["ledger", "today"])
    );
    assert_eq!(
        summary(
            &path,
            &UsageOptions {
                key_id: Some("unattributed".to_string()),
                ..UsageOptions::default()
            }
        ),
        expected(&["ledger", "unattributed"])
    );
    assert_eq!(
        summary(
            &path,
            &UsageOptions {
                result: Some("cancelled".to_string()),
                ..UsageOptions::default()
            }
        ),
        expected(&["ledger", "results"])
    );
}

#[test]
fn the_request_page_matches_the_python_reader() {
    let temp = temp();
    let path = write_basic_ledger(&temp);

    assert_eq!(
        requests(&path, &RequestOptions::default()),
        expected(&["ledger", "requests", "basic"])
    );
    assert_eq!(
        requests(
            &path,
            &RequestOptions {
                limit: 2,
                ..RequestOptions::default()
            }
        ),
        expected(&["ledger", "requests", "limit2"])
    );
    assert_eq!(
        requests(
            &path,
            &RequestOptions {
                limit: 2,
                cursor: Some("6".to_string()),
                ..RequestOptions::default()
            }
        ),
        expected(&["ledger", "requests", "cursor"])
    );
    assert_eq!(
        requests(
            &path,
            &RequestOptions {
                window: Some("24h".to_string()),
                key_id: Some("unattributed".to_string()),
                model: Some("GLM-5.2-NVFP4".to_string()),
                result: Some("cancelled".to_string()),
                ..RequestOptions::default()
            }
        ),
        expected(&["ledger", "requests", "filters"])
    );
    assert_eq!(
        requests(
            &path,
            &RequestOptions {
                window: Some("today".to_string()),
                ..RequestOptions::default()
            }
        ),
        expected(&["ledger", "requests", "today"])
    );
    assert_eq!(
        requests(
            &path,
            &RequestOptions {
                limit: 200,
                ..RequestOptions::default()
            }
        ),
        expected(&["ledger", "requests", "limit200"])
    );
}

#[test]
fn a_missing_ledger_is_an_empty_result_not_an_error() {
    let temp = temp();
    let path = temp.path().join("missing.jsonl");
    assert_eq!(
        summary(&path, &UsageOptions::default()),
        expected(&["ledger", "missing", "summary"])
    );
    assert_eq!(
        requests(&path, &RequestOptions::default()),
        expected(&["ledger", "missing", "requests"])
    );
    assert!(!path.exists());
}

#[test]
fn an_empty_ledger_is_a_real_zero() {
    let temp = temp();
    let path = temp.path().join("empty.jsonl");
    fs::write(&path, "").expect("empty ledger");
    assert_eq!(
        summary(&path, &UsageOptions::default()),
        expected(&["ledger", "empty", "summary"])
    );
    assert_eq!(
        requests(&path, &RequestOptions::default()),
        expected(&["ledger", "empty", "requests"])
    );
}

#[test]
fn the_read_limit_is_reported_as_truncation() {
    let temp = temp();
    let path = temp.path().join("truncated.jsonl");
    let count = ledger_inputs()["truncatedRecords"]
        .as_u64()
        .expect("truncated record count");
    fs::write(&path, "{}\n".repeat(count as usize)).expect("truncated ledger");
    assert_eq!(
        summary(&path, &UsageOptions::default()),
        expected(&["ledger", "truncated", "summary"])
    );
    assert_eq!(
        requests(
            &path,
            &RequestOptions {
                limit: 2,
                ..RequestOptions::default()
            }
        ),
        expected(&["ledger", "truncated", "requests"])
    );
}

#[test]
fn the_private_ledger_is_refused() {
    let temp = temp();
    let path = temp.path().join("usage-chat2api.jsonl");
    write_records(&path, &ledger_inputs()["privateRecords"]);
    let summary_error = read_share_usage(&path, &UsageOptions::default(), &fixed_clock())
        .expect_err("private ledger must be refused");
    let request_error = read_share_requests(&path, &RequestOptions::default(), &fixed_clock())
        .expect_err("private ledger must be refused");
    let expected_error = expected(&["ledger", "errors", "summaryPrivate"]);
    assert_eq!(
        serde_json::json!({
            "error": "ValueError",
            "message": summary_error.message(),
        }),
        expected_error
    );
    assert_eq!(request_error.message(), summary_error.message());
}

#[test]
fn invalid_filters_match_the_python_messages() {
    let temp = temp();
    let path = temp.path().join("invalid.jsonl");
    write_records(&path, &ledger_inputs()["invalidRecords"]);
    let invalid = expected(&["ledger", "invalid"]);

    let summary_cases = [
        (
            "summaryWindow",
            UsageOptions {
                window: Some("2d".to_string()),
                ..UsageOptions::default()
            },
        ),
        (
            "summaryLimitNegative",
            UsageOptions {
                limit: -1,
                ..UsageOptions::default()
            },
        ),
        (
            "summaryKey",
            UsageOptions {
                key_id: Some("k".repeat(129)),
                ..UsageOptions::default()
            },
        ),
        (
            "summaryModel",
            UsageOptions {
                model: Some("m".repeat(201)),
                ..UsageOptions::default()
            },
        ),
        (
            "summaryResult",
            UsageOptions {
                result: Some("ok".to_string()),
                ..UsageOptions::default()
            },
        ),
    ];
    for (name, options) in summary_cases {
        let error =
            read_share_usage(&path, &options, &fixed_clock()).expect_err("expected filter error");
        assert_eq!(
            serde_json::json!({"error": "ValueError", "message": error.message()}),
            invalid[name]
        );
    }

    let request_cases = [
        (
            "requestsWindow",
            RequestOptions {
                window: Some("2d".to_string()),
                ..RequestOptions::default()
            },
        ),
        (
            "requestsLimitZero",
            RequestOptions {
                limit: 0,
                ..RequestOptions::default()
            },
        ),
        (
            "requestsLimitLarge",
            RequestOptions {
                limit: 201,
                ..RequestOptions::default()
            },
        ),
        (
            "requestsKey",
            RequestOptions {
                key_id: Some("k".repeat(129)),
                ..RequestOptions::default()
            },
        ),
        (
            "requestsModel",
            RequestOptions {
                model: Some("m".repeat(201)),
                ..RequestOptions::default()
            },
        ),
        (
            "requestsResult",
            RequestOptions {
                result: Some("ok".to_string()),
                ..RequestOptions::default()
            },
        ),
        (
            "requestsCursor",
            RequestOptions {
                cursor: Some("not-a-cursor".to_string()),
                ..RequestOptions::default()
            },
        ),
        (
            "requestsCursorNegative",
            RequestOptions {
                cursor: Some("-1".to_string()),
                ..RequestOptions::default()
            },
        ),
    ];
    for (name, options) in request_cases {
        let error = read_share_requests(&path, &options, &fixed_clock())
            .expect_err("expected filter error");
        assert_eq!(
            serde_json::json!({"error": "ValueError", "message": error.message()}),
            invalid[name]
        );
    }
}

#[test]
fn the_read_is_read_only_and_never_leaks_the_absolute_path() {
    let temp = temp();
    let path = temp.path().join("usage-share-backend.jsonl");
    fs::write(&path, "{\"key_id\":\"key-a\",\"input_tokens\":1}\n").expect("ledger");
    let before = fs::read(&path).expect("read before");
    let summary = summary(&path, &UsageOptions::default());
    let requests = requests(&path, &RequestOptions::default());
    assert_eq!(fs::read(&path).expect("read after"), before);
    let blob = serde_json::to_string(&summary).expect("summary json");
    assert!(!blob.contains(temp.path().to_string_lossy().as_ref()));
    let blob = serde_json::to_string(&requests).expect("requests json");
    assert!(!blob.contains(temp.path().to_string_lossy().as_ref()));
}
