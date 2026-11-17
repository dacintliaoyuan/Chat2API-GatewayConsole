//! #86: local usage-tokens / usage-rates / stats read faces.

//!

//! The local role reads the private chat2api ledger and the frozen DSH day

//! snapshot. Missing ledgers keep their coverage honest (no fabricated rows or

//! zero totals), and the cloud role still refuses these faces.



mod support;



use chrono::{FixedOffset, TimeZone, Utc};

use serde_json::{json, Value};



use support::*;

use um_console::Role;



fn beijing() -> FixedOffset {

    FixedOffset::east_opt(8 * 3600).expect("+08:00")

}



fn today_stamp() -> (String, String) {

    let now = Utc::now().with_timezone(&beijing());

    let today = now.date_naive();

    let stamp = beijing()

        .from_local_datetime(&today.and_hms_opt(0, 0, 0).expect("midnight"))

        .single()

        .expect("local midnight")

        .format("%Y-%m-%dT%H:%M:%S+08:00")

        .to_string();

    (today.format("%Y-%m-%d").to_string(), stamp)

}



fn write_jsonl(path: &std::path::Path, rows: &[Value]) {

    if let Some(parent) = path.parent() {

        std::fs::create_dir_all(parent).expect("create ledger dir");

    }

    std::fs::write(

        path,

        rows.iter()

            .map(Value::to_string)

            .collect::<Vec<_>>()

            .join("\n")

            + "\n",

    )

    .expect("write ledger");

}



fn local_console(temp: &um_test_harness::TempDataDir) -> Console {

    start_console_with(temp, stopped_runtime(), |config| {

        config.role = Role::Local;

        config.port = 8001;

        config.gateway_port = 8000;

        config.usage_log = temp.path().join("usage-chat2api.jsonl");

        config.dsh_snapshot = temp.path().join("dsh-ledger-20260902.json");

        config.service_log = temp.path().join("chat2api/chat2api.log");

        config.watch_dir = Some(temp.path().join("watch"));

        config.share_backend_dir = Some(temp.path().join("share-backend"));

    })

}



fn seed_usage(temp: &um_test_harness::TempDataDir) -> (String, String) {

    let (today, stamp) = today_stamp();

    let ledger = temp.path().join("usage-chat2api.jsonl");

    write_jsonl(

        &ledger,

        &[

            json!({

                "time": stamp,

                "model": "m1",

                "status": "ok",

                "input_tokens": 100,

                "output_tokens": 20,

                "cached_input_tokens": 10,

                "reasoning_tokens": 5,

                "duration_ms": 1000,

                "usage_source": "upstream",

            }),

            json!({

                "time": stamp,

                "model": "m1",

                "status": "ok",

                "input_tokens": 30,

                "duration_ms": 500,

                "usage_source": "upstream",

            }),

            json!({

                "time": stamp,

                "model": "m2",

                "status": "error",

                "input_tokens": 50,

                "output_tokens": 10,

                "duration_ms": 0,

                "usage_source": "upstream",

            }),

            json!({

                "time": "not-a-time",

                "model": "m3",

                "status": "ok",

                "input_tokens": 1,

                "output_tokens": 1,

            }),

        ],

    );

    std::fs::write(

        temp.path().join("dsh-ledger-20260902.json"),

        serde_json::to_vec(&json!({

            "days": {

                "2026-09-02": {

                    "calls": 3,

                    "input": 3000,

                    "output": 300,

                    "cacheRead": 100,

                    "reasoning": 20,

                    "byProviderModel": {

                        "openwebui:Test-A": {

                            "calls": 2,

                            "input": 2000,

                            "output": 200,

                            "cacheRead": 100,

                            "reasoning": 10,

                        },

                    },

                },

            },

        }))

        .expect("snapshot"),

    )

    .expect("write snapshot");

    (today, stamp)

}



#[test]

fn usage_tokens_matches_python_shape_and_keeps_missing_data_unknown() {

    let temp = temp();

    let (today, _stamp) = seed_usage(&temp);

    let console = local_console(&temp);

    let response = client_request(

        console.addr,

        &ClientRequest::get("/api/console/usage-tokens?days=2"),

    )

    .expect("usage-tokens");

    assert_eq!(response.status, 200);

    assert_eq!(response.header("cache-control"), Some("no-store"));

    let payload = response.json();

    assert_eq!(payload["timezone"], json!("Asia/Shanghai"));

    assert_eq!(payload["coverage"]["days"], json!(2));

    assert_eq!(payload["coverage"]["scanned"], json!(3));

    assert_eq!(payload["coverage"]["complete_rows"], json!(1));

    assert_eq!(payload["coverage"]["partial_rows"], json!(1));

    assert_eq!(

        payload["coverage"]["sources"]["chat2api"]["ledger_present"],

        json!(true)

    );

    assert_eq!(

        payload["coverage"]["sources"]["chat2api"]["events"],

        json!(4)

    );

    assert_eq!(

        payload["coverage"]["sources"]["chat2api"]["undated_records"],

        json!(1)

    );

    let rows = payload["days"].as_array().expect("days");

    let today_row = rows

        .iter()

        .find(|row| row["date"] == json!(today) && row["model"] == json!("m1"))

        .expect("today m1 row");

    assert_eq!(today_row["input_tokens"], json!(100));

    assert_eq!(today_row["output_tokens"], json!(20));

    assert_eq!(today_row["complete"], json!(false));

    assert_eq!(today_row["complete_requests"], json!(1));

    assert_eq!(today_row["partial_requests"], json!(1));

    assert_eq!(today_row["partial_input_tokens"], json!(30));

    assert_eq!(today_row["partial_output_tokens"], Value::Null);

    let missing = payload["missing"].as_array().expect("missing");

    let invalid = missing

        .iter()

        .find(|row| row["reason"] == json!("invalid_time"))

        .expect("invalid_time row");

    assert_eq!(invalid["date"], Value::Null);

    assert_eq!(invalid["input_tokens"], Value::Null);

    assert_eq!(invalid["output_tokens"], Value::Null);

    let partial = missing

        .iter()

        .find(|row| row["reason"] == json!("no_input"))

        .expect("no_input row");

    assert_eq!(partial["date"], json!(today));

    assert_eq!(partial["input_tokens"], json!(30));

    assert_eq!(partial["output_tokens"], Value::Null);

}



#[test]

fn usage_tokens_missing_ledger_keeps_null_coverage_instead_of_zero_rows() {

    let temp = temp();

    let console = local_console(&temp);

    let response = client_request(

        console.addr,

        &ClientRequest::get("/api/console/usage-tokens?days=2"),

    )

    .expect("usage-tokens");

    assert_eq!(response.status, 200);

    let payload = response.json();

    assert_eq!(payload["days"], json!([]));

    assert_eq!(payload["missing"], json!([]));

    assert_eq!(payload["coverage"]["scanned"], json!(0));

    assert_eq!(payload["coverage"]["truncated"], json!(false));

    for source in ["chat2api", "dsh"] {

        assert_eq!(

            payload["coverage"]["sources"][source]["ledger_present"],

            json!(false),

            "{source}"

        );

        assert_eq!(

            payload["coverage"]["sources"][source]["events"],

            json!(0),

            "{source}"

        );

        assert_eq!(

            payload["coverage"]["sources"][source]["note"],

            json!("账本文件缺席：本来源不出行，不冒充零用量"),

            "{source}"

        );

    }

}



#[test]

fn usage_rates_matches_python_shape_and_counts_uncomputable_records_as_skipped() {

    let temp = temp();

    let (today, _stamp) = seed_usage(&temp);

    let console = local_console(&temp);

    let response = client_request(

        console.addr,

        &ClientRequest::get("/api/console/usage-rates?hours=24"),

    )

    .expect("usage-rates");

    assert_eq!(response.status, 200);

    assert_eq!(response.header("cache-control"), Some("no-store"));

    let payload = response.json();

    assert_eq!(payload["timezone"], json!("Asia/Shanghai"));

    assert_eq!(payload["coverage"]["hours"], json!(24));

    assert_eq!(payload["coverage"]["rows"], json!(1));

    assert_eq!(payload["coverage"]["truncated"], json!(false));

    assert_eq!(payload["skipped"]["missing_output_tokens"], json!(1));

    assert_eq!(payload["skipped"]["missing_duration"], json!(0));

    assert_eq!(payload["skipped"]["invalid_duration"], json!(1));

    assert_eq!(payload["skipped"]["invalid_time"], json!(1));

    assert_eq!(payload["recorded_requests"]["chat2api"], json!(3));

    assert_eq!(payload["recorded_requests"]["dsh"], json!(0));

    let hours = payload["hours"].as_array().expect("hours");

    assert_eq!(hours.len(), 1);

    let row = &hours[0];

    assert_eq!(row["hour"], json!(format!("{today}T00:00:00+08:00")));

    assert_eq!(row["source"], json!("chat2api"));

    assert_eq!(row["model"], json!("m1"));

    assert_eq!(row["output_tokens"], json!(20));

    assert_eq!(row["duration_ms"], json!(1000));

    assert_eq!(row["requests"], json!(1));

    assert_eq!(row["rate"], json!(20.0));

}



#[test]

fn usage_rates_missing_ledger_keeps_empty_hours_and_null_recorded_counts() {

    let temp = temp();

    let console = local_console(&temp);

    let response = client_request(

        console.addr,

        &ClientRequest::get("/api/console/usage-rates?hours=24"),

    )

    .expect("usage-rates");

    assert_eq!(response.status, 200);

    let payload = response.json();

    assert_eq!(payload["hours"], json!([]));

    assert_eq!(

        payload["skipped"],

        json!({

            "missing_output_tokens": 0,

            "missing_duration": 0,

            "invalid_duration": 0,

            "invalid_time": 0,

        })

    );

    assert_eq!(payload["recorded_requests"]["chat2api"], Value::Null);

    assert_eq!(payload["recorded_requests"]["dsh"], Value::Null);

    for source in ["chat2api", "dsh"] {

        assert_eq!(

            payload["coverage"]["sources"][source]["ledger_present"],

            json!(false),

            "{source}"

        );

    }

}

fn day_before(day: &str) -> String {
    chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
        .expect("day")
        .pred_opt()
        .expect("previous day")
        .format("%Y-%m-%d")
        .to_string()
}

#[test]
fn stats_matches_python_shape_and_keeps_missing_days_honest() {
    let temp = temp();
    let (today, _stamp) = seed_usage(&temp);
    let yesterday = day_before(&today);
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get(&format!(
            "/api/console/stats?from={yesterday}&to={today}"
        )),
    )
    .expect("stats");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("cache-control"), Some("no-store"));
    let payload = response.json();
    assert_eq!(payload["meta"]["timezone"], json!("Asia/Shanghai"));
    assert_eq!(payload["meta"]["range_days"], json!(2));
    assert_eq!(payload["coverage"]["missing_days"], json!([yesterday]));
    assert_eq!(
        payload["coverage"]["sources"]["chat2api"]["ledger_present"],
        json!(true)
    );
    assert_eq!(
        payload["coverage"]["sources"]["chat2api"]["days_with_records"],
        json!(1)
    );
    assert_eq!(
        payload["coverage"]["sources"]["chat2api"]["undated_records"],
        json!(1)
    );
    assert_eq!(
        payload["coverage"]["sources"]["chat2api"]["ledger_files"],
        json!(["usage-chat2api.jsonl"])
    );
    assert_eq!(
        payload["coverage"]["sources"]["dsh"]["days_with_records"],
        json!(0)
    );
    assert_eq!(payload["totals"]["requests"], json!(3));
    assert_eq!(payload["totals"]["input_tokens"], json!(180));
    assert_eq!(payload["totals"]["output_tokens"], json!(30));
    assert_eq!(payload["totals"]["cache_tokens"], json!(10));
    assert_eq!(payload["totals"]["reasoning_tokens"], json!(5));
    let days = payload["days"].as_array().expect("days");
    assert_eq!(days.len(), 1);
    let today_entry = days
        .iter()
        .find(|day| day["date"] == json!(today))
        .expect("today entry");
    assert_eq!(today_entry["sources"].as_array().expect("sources").len(), 1);
    let block = &today_entry["sources"][0];
    assert_eq!(block["source"], json!("chat2api"));
    assert_eq!(block["kind"], json!("estimate"));
    assert_eq!(
        block["source_totals"],
        json!({
            "requests": 3,
            "input_tokens": 180,
            "output_tokens": 30,
            "cache_tokens": 10,
            "reasoning_tokens": 5,
        })
    );
    let m1 = block["models"]
        .as_array()
        .expect("models")
        .iter()
        .find(|row| row["model"] == json!("m1"))
        .expect("m1 row");
    assert_eq!(m1["requests"], json!(2));
    assert_eq!(m1["input_tokens"], json!(130));
    assert_eq!(m1["output_tokens"], json!(20));
    assert_eq!(m1["cache_tokens"], json!(10));
    assert_eq!(m1["reasoning_tokens"], json!(5));
    assert_eq!(m1["complete_input_tokens"], json!(100));
    assert_eq!(m1["complete_output_tokens"], json!(20));
    assert_eq!(m1["token_total"], json!(120));
    assert_eq!(m1["partial_input_tokens"], json!(30));
    assert_eq!(m1["partial_output_tokens"], Value::Null);
    assert_eq!(m1["partial_token_total"], json!(30));
    assert_eq!(
        m1["token_coverage"],
        json!({
            "records": 2,
            "complete_records": 1,
            "partial_records": 1,
            "estimated_records": 0,
            "unknown_records": 0,
            "complete": false,
            "unit": "request",
        })
    );
    assert_eq!(today_entry["token_coverage"]["records"], json!(3));
    assert_eq!(
        today_entry["token_coverage"]["complete_records"],
        json!(1)
    );
    assert_eq!(
        today_entry["token_coverage"]["partial_records"],
        json!(2)
    );
    assert_eq!(today_entry["token_coverage"]["complete"], json!(false));
    assert_eq!(
        today_entry["day_total"],
        json!({
            "requests": 3,
            "input_tokens": 180,
            "output_tokens": 30,
            "cache_tokens": 10,
            "reasoning_tokens": 5,
        })
    );
    let ranking = today_entry["model_token_ranking"]
        .as_array()
        .expect("ranking");
    assert_eq!(ranking.len(), 1);
    assert_eq!(ranking[0]["model"], json!("m1"));
    assert_eq!(ranking[0]["token_total"], json!(120));
}

#[test]
fn stats_missing_ledgers_keep_missing_days_and_zero_totals() {
    let temp = temp();
    let (today, _stamp) = seed_usage(&temp);
    let yesterday = day_before(&today);
    std::fs::remove_file(temp.path().join("usage-chat2api.jsonl")).expect("remove ledger");
    std::fs::remove_file(temp.path().join("dsh-ledger-20260902.json"))
        .expect("remove snapshot");
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get(&format!(
            "/api/console/stats?from={yesterday}&to={today}"
        )),
    )
    .expect("stats");
    assert_eq!(response.status, 200);
    let payload = response.json();
    assert_eq!(payload["days"], json!([]));
    assert_eq!(payload["coverage"]["missing_days"], json!([yesterday, today]));
    assert_eq!(
        payload["totals"],
        json!({
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_tokens": 0,
            "reasoning_tokens": 0,
        })
    );
    for source in ["chat2api", "dsh"] {
        let coverage = &payload["coverage"]["sources"][source];
        assert_eq!(coverage["ledger_present"], json!(false), "{source}");
        assert_eq!(coverage["days_with_records"], json!(0), "{source}");
        assert!(coverage.get("note").is_none(), "{source}");
        assert!(coverage.get("events").is_none(), "{source}");
        assert!(coverage.get("undated_records").is_none(), "{source}");
    }
}

#[test]
fn stats_invalid_range_is_a_400_invalid_stats_query() {
    let temp = temp();
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/stats?from=2026-11-17&to=2026-11-16"),
    )
    .expect("stats");
    assert_eq!(response.status, 400);
    assert_eq!(response.json()["error"], json!("invalid_stats_query"));
}

#[test]
fn log_sources_matches_python_registry_shape_without_paths() {
    let temp = temp();
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/log-sources"),
    )
    .expect("log-sources");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("cache-control"), Some("no-store"));
    let payload = response.json();
    assert_eq!(payload["ok"], json!(true));
    let sources = payload["sources"].as_array().expect("sources");
    assert_eq!(sources.len(), 3);
    assert_eq!(sources[0]["source"], json!("chat2api"));
    assert_eq!(sources[0]["display"], json!("chat2api 主链（8000）"));
    let categories = sources[0]["categories"].as_array().expect("categories");
    assert_eq!(categories.len(), 5);
    assert_eq!(categories[0]["category"], json!("service"));
    assert_eq!(categories[0]["name"], json!("chat2api.log"));
    assert_eq!(categories[0]["rotating"], json!(true));
    assert_eq!(categories[0]["rotation_index"], json!(5));
    assert_eq!(categories[0]["level_filter"], json!(true));
    assert!(categories[0].get("path").is_none());
    assert_eq!(categories[1]["category"], json!("ledger"));
    assert_eq!(categories[1]["name"], json!("usage-chat2api.jsonl"));
    assert_eq!(categories[2]["name"], json!("chat2api.console.log"));
    assert_eq!(categories[3]["name"], json!("chat2api.console.prev.log"));
    assert_eq!(categories[4]["name"], json!("chat2api-watch.log"));
    assert_eq!(sources[1]["source"], json!("console"));
    assert_eq!(sources[1]["categories"], json!([]));
    assert_eq!(
        sources[1]["note"],
        json!("8001 宿主不落盘行日志（start_usage_service.bat 无重定向，usagesrv 的输出只到隐藏窗口）；控制台自身的操作记录在「操作记录」页签（data/console-state.sqlite3），此处不伪造日志文件。")
    );
    assert_eq!(sources[2]["source"], json!("share"));
    assert_eq!(sources[2]["display"], json!("分享链 8100（合并进程，非 8000 主链）"));
    let share = sources[2]["categories"].as_array().expect("share categories");
    assert_eq!(share.len(), 5);
    assert_eq!(share[0]["name"], json!("share-backend.log"));
    assert_eq!(share[1]["name"], json!("usage-share-backend.jsonl"));
    assert_eq!(share[2]["name"], json!("raw.txt"));
    assert_eq!(share[3]["name"], json!("share-faults.jsonl"));
    assert_eq!(share[4]["name"], json!("lifecycle.json"));
    assert_eq!(share[4]["whole_document"], json!(true));
    for source in sources {
        for category in source["categories"].as_array().expect("categories") {
            assert!(category.get("path").is_none(), "{category}");
        }
    }
}

fn write_service_log(temp: &um_test_harness::TempDataDir) -> std::path::PathBuf {
    let path = temp.path().join("chat2api/chat2api.log");
    std::fs::create_dir_all(path.parent().expect("parent")).expect("log dir");
    let text = [
        "2026-11-17 10:00:00 INFO hello",
        "2026-11-17T10:01:00+08:00 WARN warn line",
        "[launcher] 2026/11/17 周三 10:02:03.65 tagged line",
        "{\"time\": \"2026-11-17T10:03:00+08:00\", \"model\": \"m1\", \"status\": \"ok\", \"input_tokens\": 5, \"output_tokens\": 2, \"duration_ms\": 100}",
        "  Traceback continuation",
        "plain unparsable line",
        "2026-11-17 10:04:00 ERROR token=secret boom",
    ]
    .join("
")
        + "
";
    std::fs::write(&path, text.as_bytes()).expect("write service log");
    path
}

#[test]
fn log_entries_matches_python_rows_redaction_and_missing_data_semantics() {
    let temp = temp();
    let path = write_service_log(&temp);
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/log-entries?source=chat2api&category=service"),
    )
    .expect("log-entries");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("cache-control"), Some("no-store"));
    let payload = response.json();
    assert_eq!(payload["ok"], json!(true));
    assert_eq!(payload["status"], json!("ok"));
    assert_eq!(payload["reset_hint"], json!(false));
    assert_eq!(payload["truncated"], json!(false));
    assert_eq!(payload["window_start_offset"], json!(0));
    assert_eq!(payload["next_cursor"], Value::Null);
    assert_eq!(
        payload["scanned_bytes"],
        json!(std::fs::metadata(&path).expect("metadata").len())
    );
    assert!(payload["file_key"].is_string());
    let rows = payload["rows"].as_array().expect("rows");
    assert_eq!(rows.len(), 5);
    assert_eq!(rows[0]["ts"], json!("2026-11-17 10:00:00"));
    assert_eq!(rows[0]["level"], json!("INFO"));
    assert_eq!(rows[0]["message"], json!("hello"));
    assert_eq!(rows[0]["parse_ok"], json!(true));
    assert_eq!(rows[1]["level"], json!("WARNING"));
    assert_eq!(rows[1]["message"], json!("warn line"));
    assert_eq!(rows[2]["ts"], json!("2026/11/17 周三 10:02:03.65"));
    assert_eq!(rows[2]["level"], Value::Null);
    assert_eq!(rows[2]["message"], json!("[launcher] tagged line"));
    assert_eq!(rows[2]["parse_ok"], json!(false));
    assert_eq!(rows[3]["ts"], json!("2026-11-17T10:03:00+08:00"));
    assert_eq!(rows[3]["level"], Value::Null);
    assert_eq!(
        rows[3]["message"],
        json!("model=m1 status=ok in=5 out=2 duration_ms=100
  Traceback continuation
plain unparsable line")
    );
    assert_eq!(rows[3]["parse_ok"], json!(false));
    assert_eq!(rows[3]["event"]["model"], json!("m1"));
    assert_eq!(rows[3]["event"]["outcome"], json!("ok"));
    assert_eq!(rows[3]["event"]["duration_ms"], json!(100));
    assert_eq!(rows[3]["event"]["token_rate"], json!(20.0));
    assert_eq!(rows[3]["event"]["token_rate_kind"], json!("mean_overall"));
    assert_eq!(rows[4]["level"], json!("ERROR"));
    assert_eq!(rows[4]["message"], json!("token=<redacted> boom"));
    assert_eq!(rows[4]["parse_ok"], json!(true));
}

#[test]
fn log_entries_unknown_source_and_missing_file_are_distinct_outcomes() {
    let temp = temp();
    let console = local_console(&temp);
    let unknown = client_request(
        console.addr,
        &ClientRequest::get("/api/console/log-entries?source=nope&category=service"),
    )
    .expect("log-entries");
    assert_eq!(unknown.status, 200);
    let payload = unknown.json();
    assert_eq!(payload["ok"], json!(true));
    assert_eq!(payload["status"], json!("error"));
    assert_eq!(payload["error"], json!("unknown_log_source"));
    assert_eq!(payload["rows"], json!([]));

    let missing = client_request(
        console.addr,
        &ClientRequest::get("/api/console/log-entries?source=chat2api&category=console"),
    )
    .expect("log-entries");
    assert_eq!(missing.status, 200);
    let payload = missing.json();
    assert_eq!(payload["ok"], json!(true));
    assert_eq!(payload["status"], json!("missing"));
    assert_eq!(payload["error"], json!("file_missing"));
    assert_eq!(payload["rows"], json!([]));
    assert_eq!(payload["truncated"], json!(false));
}

fn seed_overview_logs(temp: &um_test_harness::TempDataDir) {
    write_service_log(temp);
    std::fs::write(
        temp.path().join("chat2api/chat2api.console.log"),
        b"2026-11-17 09:00:00 INFO console line
",
    )
    .expect("console log");
    std::fs::write(
        temp.path().join("usage-chat2api.jsonl"),
        b"{\"time\": \"2026-11-17T10:03:00+08:00\", \"model\": \"m1\", \"status\": \"ok\", \"input_tokens\": 5, \"output_tokens\": 2, \"duration_ms\": 100}
",
    )
    .expect("usage ledger");
    std::fs::create_dir_all(temp.path().join("watch")).expect("watch dir");
    std::fs::write(
        temp.path().join("watch/chat2api-watch.log"),
        b"2026-11-17T08:00:00+08:00 INFO watch line
",
    )
    .expect("watch log");
    std::fs::create_dir_all(temp.path().join("share-backend")).expect("share dir");
    std::fs::write(
        temp.path().join("share-backend/share-backend.log"),
        b"{\"time\": \"2026-11-17T07:00:00+08:00\", \"model\": \"sm\", \"status\": \"ok\"}
",
    )
    .expect("share service log");
    std::fs::write(
        temp.path().join("share-backend/usage-share-backend.jsonl"),
        b"{\"time\": \"2026-11-17T06:00:00+08:00\", \"model\": \"sm2\"}
",
    )
    .expect("share ledger");
}

#[test]
fn log_overview_merges_registered_targets_and_states_coverage() {
    let temp = temp();
    seed_overview_logs(&temp);
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/log-overview?since=2026-11-17"),
    )
    .expect("log-overview");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("cache-control"), Some("no-store"));
    let payload = response.json();
    assert_eq!(payload["ok"], json!(true));
    assert_eq!(payload["coverage"]["scope"], json!("bounded_tail_window"));
    assert_eq!(payload["coverage"]["full_history"], json!(false));
    assert!(payload["coverage"]["note"]
        .as_str()
        .expect("note")
        .contains("不是完整历史"));
    assert_eq!(payload["coverage"]["targets_total"], json!(10));
    assert!(payload["coverage"]["targets_unread"].as_u64().unwrap_or(0) >= 1);
    assert_eq!(payload["coverage"]["rows_dropped_by_budget"], json!(0));
    assert_eq!(payload["coverage"]["sources_without_targets"], json!(["console"]));
    let targets = payload["targets"].as_array().expect("targets");
    let target = |source: &str, category: &str| {
        targets
            .iter()
            .find(|target| target["source"] == json!(source) && target["category"] == json!(category))
            .unwrap_or_else(|| panic!("target {source}|{category}"))
    };
    assert_eq!(target("chat2api", "service")["status"], json!("ok"));
    assert_eq!(target("chat2api", "service")["rows"], json!(5));
    assert_eq!(target("chat2api", "service")["rows_returned"], json!(5));
    assert_eq!(target("chat2api", "ledger")["status"], json!("ok"));
    assert_eq!(target("chat2api", "ledger")["rows"], json!(1));
    assert_eq!(target("chat2api", "console")["status"], json!("ok"));
    assert_eq!(target("chat2api", "console")["rows"], json!(1));
    assert_eq!(target("chat2api", "console_prev")["status"], json!("missing"));
    assert_eq!(target("chat2api", "console_prev")["rows"], Value::Null);
    assert_eq!(target("chat2api", "watch")["status"], json!("ok"));
    assert_eq!(target("share", "service")["status"], json!("ok"));
    assert_eq!(target("share", "ledger")["status"], json!("ok"));
    let next_cursors = payload["next_cursors"].as_object().expect("next cursors");
    for key in [
        "chat2api|service",
        "chat2api|ledger",
        "chat2api|console",
        "chat2api|console_prev",
        "chat2api|watch",
        "share|service",
        "share|ledger",
        "share|gateway",
        "share|faults",
        "share|broker",
    ] {
        assert!(next_cursors.contains_key(key), "{key}");
    }
    let rows = payload["rows"].as_array().expect("rows");
    let messages: Vec<&str> = rows
        .iter()
        .filter_map(|row| row["message"].as_str())
        .collect();
    assert!(messages.iter().any(|message| message.contains("hello")));
    assert!(messages.iter().any(|message| message.contains("console line")));
    assert!(messages.iter().any(|message| message.contains("watch line")));
    assert!(messages.iter().any(|message| message.contains("model=sm status=ok")));
    assert!(messages.iter().any(|message| message.contains("model=sm2")));
    let ledger_row = rows
        .iter()
        .find(|row| row["source"] == json!("chat2api") && row["category"] == json!("ledger"))
        .expect("chat2api ledger row");
    assert_eq!(ledger_row["event"]["model"], json!("m1"));
    assert_eq!(ledger_row["event"]["token_rate"], json!(20.0));
    assert!(ledger_row["ts_norm"].is_number());
    assert!(ledger_row["identity"].is_string());
    let chat2api = payload["sources"]
        .as_array()
        .expect("sources")
        .iter()
        .find(|source| source["source"] == json!("chat2api"))
        .expect("chat2api source");
    assert_eq!(chat2api["status"], json!("partial"));
    assert_eq!(chat2api["targets_total"], json!(5));
    assert_eq!(chat2api["targets_unread"], json!(1));
}

#[test]
fn log_entries_truncates_non_ascii_models_by_characters_without_panicking() {
    let temp = temp();
    let model = "模".repeat(70);
    std::fs::write(
        temp.path().join("usage-chat2api.jsonl"),
        format!(
            "{{\"time\": \"2026-11-17T10:03:00+08:00\", \"model\": \"{model}\", \"status\": \"ok\"}}\n"
        ),
    )
    .expect("write ledger");
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/log-entries?source=chat2api&category=ledger"),
    )
    .expect("log-entries");
    assert_eq!(response.status, 200);
    let payload = response.json();
    let row = &payload["rows"][0];
    assert_eq!(row["event"]["model"], json!("模".repeat(64)));
}

#[test]
fn log_entries_redacts_sign_signature_and_code_quoted_json_values() {
    let temp = temp();
    std::fs::create_dir_all(temp.path().join("chat2api")).expect("log dir");
    std::fs::write(
        temp.path().join("chat2api/chat2api.log"),
        b"2026-11-17 10:00:00 INFO payload {\"sign\": \"sign-value\", \"signature\": \"signature-value\", \"code\": \"code-value\"}\n",
    )
    .expect("write service log");
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/log-entries?source=chat2api&category=service"),
    )
    .expect("log-entries");
    assert_eq!(response.status, 200);
    let payload = response.json();
    let message = payload["rows"][0]["message"].as_str().expect("message");
    assert_eq!(
        message,
        "payload {\"sign\": \"<redacted>\", \"signature\": \"<redacted>\", \"code\": \"<redacted>\"}"
    );
    assert!(!message.contains("sign-value"));
    assert!(!message.contains("signature-value"));
    assert!(!message.contains("code-value"));
}

fn fixed_size_log(marker: &str) -> Vec<u8> {
    (0..500)
        .map(|index| {
            format!(
                "2026-11-17 10:{:02}:00 INFO {marker}-{index:04}\n",
                index % 60
            )
        })
        .collect::<String>()
        .into_bytes()
}

fn encode_query_value(value: &str) -> String {
    value
        .bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
                (byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}

#[test]
fn log_entries_detects_a_same_size_replacement_through_file_identity() {
    let temp = temp();
    std::fs::create_dir_all(temp.path().join("chat2api")).expect("log dir");
    let path = temp.path().join("chat2api/chat2api.log");
    let old_bytes = fixed_size_log("A");
    std::fs::write(&path, &old_bytes).expect("write old log");
    let console = local_console(&temp);
    let first = client_request(
        console.addr,
        &ClientRequest::get("/api/console/log-entries?source=chat2api&category=service"),
    )
    .expect("first log-entries")
    .json();
    assert_eq!(first["reset_hint"], json!(false));
    let cursor = first["next_cursor"].as_str().expect("next cursor").to_string();
    let old_key = first["file_key"].clone();
    assert!(old_key.is_string());

    std::fs::remove_file(&path).expect("remove old log");
    let new_bytes = fixed_size_log("B");
    assert_eq!(new_bytes.len(), old_bytes.len());
    std::fs::write(&path, &new_bytes).expect("write replacement log");
    let response = client_request(
        console.addr,
        &ClientRequest::get(&format!(
            "/api/console/log-entries?source=chat2api&category=service&cursor={}",
            encode_query_value(&cursor)
        )),
    )
    .expect("second log-entries");
    assert_eq!(response.status, 200);
    let second = response.json();
    assert_eq!(second["reset_hint"], json!(true));
    assert_eq!(second["rows"], json!([]));
    assert_ne!(second["file_key"], old_key);
}

#[test]
fn stats_reports_corrupt_dsh_snapshot_shapes_as_corrupt_snapshot() {
    let temp = temp();
    seed_usage(&temp);
    let console = local_console(&temp);

    let assert_corrupt_snapshot = |snapshot: Value| {
        std::fs::write(
            temp.path().join("dsh-ledger-20260902.json"),
            snapshot.to_string(),
        )
        .expect("write dsh snapshot");
        let response = client_request(
            console.addr,
            &ClientRequest::get("/api/console/stats?from=2026-09-02&to=2026-09-02"),
        )
        .expect("stats");
        assert_eq!(response.status, 200);
        let payload = response.json();
        let errors = payload["coverage"]["sources"]["dsh"]["errors"]
            .as_array()
            .expect("dsh errors");
        assert!(
            errors.iter().any(|error| error["type"] == json!("corrupt_snapshot")),
            "{errors:?}"
        );
        assert!(
            !errors.iter().any(|error| error["type"] == json!("corrupt_ledger")),
            "{errors:?}"
        );
    };

    assert_corrupt_snapshot(json!({"days": "not-an-object"}));
    assert_corrupt_snapshot(json!({
        "days": {"2026-09-02": {"byProviderModel": "not-an-object"}},
    }));
}
