use std::collections::HashSet;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

use chrono::{Local, TimeZone};
use um_desktop::logstream::{
    format_log_time, plan_log_page, read_history, LevelFilter, LiveLog, LogReadError, LogStream,
    LOG_ROW_HEIGHT,
};

mod common;
use common::TempDir;

fn append(path: &Path, text: &str) {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .expect("open log for append");
    file.write_all(text.as_bytes()).expect("append log");
}

fn texts(stream: &LogStream) -> Vec<String> {
    stream
        .entries()
        .into_iter()
        .map(|entry| entry.text)
        .collect()
}

#[test]
fn open_loads_recent_lines_and_starts_at_the_file_end() {
    let dir = TempDir::new("logstream-open");
    let path = dir.path().join("service.log");
    append(&path, "line-1\nline-2\nline-3\n");

    let stream = LogStream::open(&path).expect("open log");
    assert_eq!(texts(&stream), vec!["line-1", "line-2", "line-3"]);
    assert_eq!(stream.offset(), path.metadata().unwrap().len());
    let ids: Vec<_> = stream.lines().iter().map(|line| line.id).collect();
    assert_ne!(ids[0], ids[1], "line ids must be stable and distinct");
    assert_eq!(ids[0].offset, 0);
    assert_eq!(ids[1].offset, 7);
}

#[test]
fn poll_appends_only_new_complete_lines_without_duplicates() {
    let dir = TempDir::new("logstream-append");
    let path = dir.path().join("service.log");
    append(&path, "a\n");
    let mut stream = LogStream::open(&path).expect("open log");

    append(&path, "b\nc\n");
    let first = stream.poll().expect("first poll");
    assert_eq!(
        first
            .appended
            .iter()
            .map(|line| line.entry.text.as_str())
            .collect::<Vec<_>>(),
        vec!["b", "c"]
    );
    assert_eq!(first.total, 3);
    assert!(!first.reset);

    let second = stream.poll().expect("second poll");
    assert!(
        second.appended.is_empty(),
        "a repeated poll must not repeat lines"
    );
    assert_eq!(second.total, 3);
    assert_eq!(texts(&stream), vec!["a", "b", "c"]);
}

#[test]
fn a_partial_line_waits_for_its_newline_and_is_not_split() {
    let dir = TempDir::new("logstream-partial");
    let path = dir.path().join("service.log");
    append(&path, "a\n");
    let mut stream = LogStream::open(&path).expect("open log");
    let offset = stream.offset();

    append(&path, "par");
    let first = stream.poll().expect("poll partial");
    assert!(first.appended.is_empty(), "a half-written line must wait");
    assert_eq!(
        stream.offset(),
        offset,
        "the offset must not advance past a partial line"
    );

    append(&path, "tial\n");
    let second = stream.poll().expect("poll completed line");
    assert_eq!(
        second
            .appended
            .iter()
            .map(|line| line.entry.text.as_str())
            .collect::<Vec<_>>(),
        vec!["partial"]
    );
    assert_eq!(texts(&stream), vec!["a", "partial"]);
}

#[test]
fn the_recent_window_is_bounded_and_keeps_the_newest_lines() {
    let dir = TempDir::new("logstream-bounded");
    let path = dir.path().join("service.log");
    for index in 1..=100 {
        append(&path, &format!("line-{index}\n"));
    }
    let stream = LogStream::open_with_limits(&path, 10, 64 * 1024).expect("open log");
    let rows = texts(&stream);
    assert_eq!(rows.len(), 10);
    assert_eq!(rows[0], "line-91");
    assert_eq!(rows[9], "line-100");
}

#[test]
fn level_filter_matches_exact_levels_and_keyword_is_case_insensitive() {
    let dir = TempDir::new("logstream-filter");
    let path = dir.path().join("service.log");
    append(
        &path,
        "{\"level\":\"info\",\"event\":\"started\"}\n\
         {\"level\":\"warn\",\"event\":\"retrying\"}\n\
         {\"level\":\"error\",\"event\":\"failed\"}\n\
         raw text\n",
    );
    let stream = LogStream::open(&path).expect("open log");
    let entries = stream.entries();

    let errors = um_desktop::logstream::filter_entries(&entries, LevelFilter::Error, "");
    assert_eq!(errors.len(), 1);
    assert!(errors[0].text.contains("failed"), "{}", errors[0].text);

    let failed = um_desktop::logstream::filter_entries(&entries, LevelFilter::All, "FAIL");
    assert_eq!(failed.len(), 1);
    assert!(failed[0].text.contains("failed"), "{}", failed[0].text);

    let none = um_desktop::logstream::filter_entries(&entries, LevelFilter::Warn, "started");
    assert!(none.is_empty());

    let all = um_desktop::logstream::filter_entries(&entries, LevelFilter::All, "");
    assert_eq!(all.len(), 4);
}

#[test]
fn truncation_resets_the_window_and_reads_the_new_content() {
    let dir = TempDir::new("logstream-truncate");
    let path = dir.path().join("service.log");
    append(&path, "a\nb\nc\n");
    let mut stream = LogStream::open(&path).expect("open log");

    std::fs::write(&path, "x\n").unwrap();
    let outcome = stream.poll().expect("poll truncation");
    assert!(outcome.reset, "truncation must reset the window");
    assert_eq!(outcome.total, 1);
    assert_eq!(texts(&stream), vec!["x"]);

    let again = stream.poll().expect("poll after truncation");
    assert!(
        again.appended.is_empty(),
        "a reset must not repeat the new content"
    );
    assert_eq!(again.total, 1);
}

#[test]
fn rotation_to_a_longer_file_resets_instead_of_appending_old_content() {
    let dir = TempDir::new("logstream-rotate");
    let path = dir.path().join("service.log");
    append(&path, "old-1\nold-2\n");
    let mut stream = LogStream::open(&path).expect("open log");

    std::fs::write(&path, "new-1\nnew-2\nnew-3\nnew-4\n").unwrap();
    let outcome = stream.poll().expect("poll rotation");
    assert!(outcome.reset, "a rotated file must reset the window");
    assert_eq!(texts(&stream), vec!["new-1", "new-2", "new-3", "new-4"]);
    assert_eq!(stream.offset(), path.metadata().unwrap().len());
}

#[test]
fn replacement_with_the_same_length_resets_by_content_not_by_length() {
    let dir = TempDir::new("logstream-replace");
    let path = dir.path().join("service.log");
    append(&path, "old-1\nold-2\n");
    let mut stream = LogStream::open(&path).expect("open log");

    std::fs::write(&path, "new-1\nnew-2\n").unwrap();
    let outcome = stream.poll().expect("poll replacement");
    assert!(
        outcome.reset,
        "a replaced file with the same length must reset"
    );
    assert_eq!(texts(&stream), vec!["new-1", "new-2"]);
}

#[test]
fn a_missing_file_is_the_empty_state_without_a_read_failure() {
    let dir = TempDir::new("logstream-missing");
    let path = dir.path().join("missing.log");
    let error = LogStream::open(&path).expect_err("a missing log has no window yet");
    assert_eq!(error, LogReadError::Missing);

    append(&path, "appeared\n");
    let stream = LogStream::open(&path).expect("open appeared log");
    assert_eq!(texts(&stream), vec!["appeared"]);
}

#[test]
fn a_directory_is_a_read_failure_not_an_empty_window() {
    let dir = TempDir::new("logstream-directory");
    let error = LogStream::open(dir.path()).expect_err("a directory must not open as a log");
    let LogReadError::Failed(reason) = &error else {
        panic!("a directory must be a read failure, got {error:?}");
    };
    assert!(reason.contains("logstream-directory"), "{reason}");
    assert!(
        reason.contains("读取日志失败") || reason.contains("不是文件"),
        "{reason}"
    );
}

#[test]
fn a_poll_error_keeps_the_previous_window_for_the_next_retry() {
    let dir = TempDir::new("logstream-poll-error");
    let path = dir.path().join("service.log");
    append(&path, "a\n");
    let mut stream = LogStream::open(&path).expect("open log");
    // A directory in place of the file is a real read failure, not a missing
    // file: the previous window must survive for the retry.
    std::fs::remove_file(&path).unwrap();
    std::fs::create_dir(&path).unwrap();

    let error = stream.poll().expect_err("an unreadable log must fail");
    let LogReadError::Failed(reason) = &error else {
        panic!("an unreadable log must be a read failure, got {error:?}");
    };
    assert!(reason.contains("service.log"), "{reason}");
    assert_eq!(
        texts(&stream),
        vec!["a"],
        "a failed poll must keep the shown lines"
    );

    std::fs::remove_dir(&path).unwrap();
    append(&path, "b\n");
    let outcome = stream.poll().expect("poll recreated log");
    assert!(outcome.reset, "a recreated file must reset the window");
    assert_eq!(texts(&stream), vec!["b"]);
}

#[test]
fn a_pause_and_backfill_reads_every_line_since_the_last_offset() {
    let dir = TempDir::new("logstream-backfill");
    let path = dir.path().join("service.log");
    append(&path, "first\n");
    let mut stream = LogStream::open(&path).expect("open log");

    for index in 1..=50 {
        append(&path, &format!("after-{index}\n"));
    }
    let outcome = stream.poll().expect("backfill");
    assert_eq!(outcome.appended.len(), 50);
    assert_eq!(outcome.total, 51);
    assert_eq!(outcome.trimmed, 0);
    let expected = std::iter::once("first".to_string())
        .chain((1..=50).map(|index| format!("after-{index}")))
        .collect::<Vec<_>>();
    assert_eq!(texts(&stream), expected, "backfill must keep file order");
}

#[test]
fn line_ids_are_stable_across_reopens_and_content_changes_at_a_new_offset() {
    let dir = TempDir::new("logstream-ids");
    let path = dir.path().join("service.log");
    append(&path, "same\n");
    let first = LogStream::open(&path).expect("open log");
    let first_id = first.lines()[0].id;
    drop(first);

    let second = LogStream::open(&path).expect("reopen log");
    assert_eq!(
        second.lines()[0].id,
        first_id,
        "reopening must keep the line id"
    );
    drop(second);

    append(&path, "same\n");
    let third = LogStream::open(&path).expect("open grown log");
    assert_eq!(third.lines().len(), 2);
    assert_ne!(
        third.lines()[0].id,
        third.lines()[1].id,
        "the same content at a new offset is a new line"
    );
}

#[test]
fn sensitive_values_and_control_characters_are_removed_from_display_text() {
    let dir = TempDir::new("logstream-sensitive");
    let path = dir.path().join("service.log");
    let raw = "bad\u{1b}[31m sk-standalone Authorization: Bearer sk-abc123 api_key=secret password=hunter2 token: tok";
    append(&path, &format!("{raw}\n"));
    let stream = LogStream::open(&path).expect("open log");
    let text = &stream.lines()[0].entry.text;
    for secret in ["standalone", "abc123", "secret", "hunter2", "tok"] {
        assert!(!text.contains(secret), "{secret} leaked in {text}");
    }
    assert!(text.contains("[redacted]"), "{text}");
    assert!(!text.contains('\u{1b}'), "raw escape byte leaked in {text}");
    assert!(text.contains("\\u{001b}"), "{text}");
    let detail = &stream.lines()[0].detail;
    assert!(!detail.contains('\u{1b}'), "raw escape byte leaked in {detail}");
    assert!(detail.contains("\\u{001b}"), "{detail}");
    assert_eq!(
        std::fs::read_to_string(&path).unwrap().trim_end(),
        raw,
        "the service log itself must not be rewritten"
    );
}

fn local_at(year: i32, month: u32, day: u32, hour: u32, minute: u32, second: u32) -> i64 {
    Local
        .with_ymd_and_hms(year, month, day, hour, minute, second)
        .single()
        .expect("unambiguous local time")
        .timestamp_millis()
}

#[test]
fn same_day_lines_show_only_the_time_and_cross_day_lines_show_the_date() {
    let now = Local
        .with_ymd_and_hms(2026, 11, 21, 10, 0, 0)
        .single()
        .expect("local now");

    let same_day = format_log_time(Some(local_at(2026, 11, 21, 9, 5, 7)), now);
    assert_eq!(same_day.label, "09:05:07");
    assert!(same_day.precise.starts_with("2026-11-21 09:05:07"), "{}", same_day.precise);

    let previous_day = format_log_time(Some(local_at(2026, 11, 20, 23, 59, 59)), now);
    assert_eq!(previous_day.label, "11-20 23:59:59");
    assert!(
        previous_day.precise.starts_with("2026-11-20 23:59:59"),
        "{}",
        previous_day.precise
    );

    let unknown = format_log_time(None, now);
    assert_eq!(unknown.label, "时间未知");
    assert_eq!(unknown.precise, "时间未知");
}

#[test]
fn paused_follow_keeps_the_reading_position_and_counts_only_new_matching_lines() {
    let dir = TempDir::new("logstream-paused");
    let path = dir.path().join("service.log");
    for index in 1..=20 {
        append(&path, &format!("line-{index}\n"));
    }
    let mut stream = LogStream::open(&path).expect("open log");
    let previous: Vec<_> = stream.lines().iter().cloned().collect();
    append(&path, "new-1\nnew-2\n");
    let outcome = stream.poll().expect("poll appended");
    let entries: Vec<_> = stream.lines().iter().cloned().collect();

    let plan = plan_log_page(
        &previous,
        &entries,
        &outcome.appended,
        outcome.reset,
        outcome.trimmed,
        false,
        false,
        -100.0,
        3,
        LevelFilter::All,
        "",
    );
    assert_eq!(plan.added, 2);
    assert_eq!(plan.new_count, 5);
    assert_eq!(
        plan.scroll_y, None,
        "paused reading must not jump when new lines arrive"
    );
    assert_eq!(plan.rows.len(), 22);
    assert!(!plan.rebuild);

    let resumed = plan_log_page(
        &previous,
        &entries,
        &outcome.appended,
        outcome.reset,
        outcome.trimmed,
        false,
        true,
        -100.0,
        plan.new_count,
        LevelFilter::All,
        "",
    );
    assert_eq!(resumed.new_count, 0, "resuming follow clears the new count");
    assert_eq!(
        resumed.scroll_y, None,
        "resuming follow jumps to the newest row"
    );
}

#[test]
fn paused_follow_keeps_the_same_row_when_the_front_is_trimmed() {
    let dir = TempDir::new("logstream-paused-trim");
    let path = dir.path().join("service.log");
    append(&path, "a\nb\nc\n");
    let mut stream = LogStream::open_with_limits(&path, 3, 64 * 1024).expect("open log");
    let previous: Vec<_> = stream.lines().iter().cloned().collect();
    append(&path, "d\n");
    let outcome = stream.poll().expect("poll appended");
    assert_eq!(outcome.trimmed, 1);
    let entries: Vec<_> = stream.lines().iter().cloned().collect();

    let plan = plan_log_page(
        &previous,
        &entries,
        &outcome.appended,
        outcome.reset,
        outcome.trimmed,
        false,
        false,
        -2.0 * LOG_ROW_HEIGHT,
        0,
        LevelFilter::All,
        "",
    );
    assert_eq!(plan.rows.len(), 3);
    assert_eq!(
        plan.scroll_y,
        Some(-LOG_ROW_HEIGHT),
        "the row that was at the top must stay at the top after a trim"
    );
    assert_eq!(plan.new_count, 1);
}

#[test]
fn paused_filtered_follow_keeps_the_same_row_when_the_front_is_trimmed() {
    let dir = TempDir::new("logstream-paused-filtered-trim");
    let path = dir.path().join("service.log");
    for (level, index) in [("info", 1), ("error", 2), ("error", 3), ("error", 4), ("info", 5)] {
        append(
            &path,
            &format!("{{\"level\":\"{level}\",\"event\":\"line-{index}\"}}\n"),
        );
    }
    let mut stream = LogStream::open_with_limits(&path, 5, 64 * 1024).expect("open log");
    let previous: Vec<_> = stream.lines().iter().cloned().collect();
    append(&path, "{\"level\":\"error\",\"event\":\"line-6\"}\n");
    let outcome = stream.poll().expect("poll appended");
    assert_eq!(outcome.trimmed, 1);
    let entries: Vec<_> = stream.lines().iter().cloned().collect();

    let plan = plan_log_page(
        &previous,
        &entries,
        &outcome.appended,
        outcome.reset,
        outcome.trimmed,
        false,
        false,
        -2.0 * LOG_ROW_HEIGHT,
        0,
        LevelFilter::Error,
        "",
    );
    assert_eq!(plan.rows.len(), 4);
    assert_eq!(
        plan.scroll_y,
        Some(-2.0 * LOG_ROW_HEIGHT),
        "the filtered row that was at the top must stay at the top after a trim"
    );
    assert_eq!(plan.new_count, 1);
}

#[test]
fn a_filter_change_rebuilds_only_matching_rows_and_clears_the_new_count() {
    let dir = TempDir::new("logstream-filter-change");
    let path = dir.path().join("service.log");
    for index in 1..=10 {
        append(&path, &format!("info-{index}\n"));
        append(&path, &format!("{{\"level\":\"error\",\"event\":\"error-{index}\"}}\n"));
    }
    let mut stream = LogStream::open(&path).expect("open log");
    let previous: Vec<_> = stream.lines().iter().cloned().collect();
    append(&path, "{\"level\":\"error\",\"event\":\"error-new\"}\n");
    let outcome = stream.poll().expect("poll appended");
    let entries: Vec<_> = stream.lines().iter().cloned().collect();

    let plan = plan_log_page(
        &previous,
        &entries,
        &outcome.appended,
        outcome.reset,
        outcome.trimmed,
        true,
        false,
        -100.0,
        5,
        LevelFilter::Error,
        "",
    );
    assert_eq!(plan.rows.len(), 11);
    assert!(plan.rows.iter().all(|line| line.entry.level == "error"));
    assert_eq!(plan.added, 1);
    assert_eq!(plan.new_count, 0);
    assert_eq!(plan.scroll_y, Some(0.0));
}

#[test]
fn a_recovered_file_backfills_without_duplicating_the_kept_window() {
    let dir = TempDir::new("logstream-recovered");
    let path = dir.path().join("service.log");
    append(&path, "a\n");
    let mut stream = LogStream::open(&path).expect("open log");
    std::fs::remove_file(&path).unwrap();
    let error = stream.poll().expect_err("a vanished log has no window to read");
    assert_eq!(error, LogReadError::Missing);
    assert_eq!(texts(&stream), vec!["a"]);

    append(&path, "a\nb\n");
    let outcome = stream.poll().expect("poll recovered log");
    assert_eq!(
        outcome
            .appended
            .iter()
            .map(|line| line.entry.text.as_str())
            .collect::<Vec<_>>(),
        vec!["a", "b"]
    );
    assert_eq!(texts(&stream), vec!["a", "b"]);
}

#[test]
fn history_pages_are_contiguous_and_do_not_repeat_lines() {
    let dir = TempDir::new("logstream-history-pages");
    let path = dir.path().join("service.log");
    let now = Local::now();
    for index in 0..300 {
        let time = now - chrono::Duration::seconds((300 - index) as i64);
        append(
            &path,
            &format!(
                "{{\"time\":\"{}\",\"level\":\"info\",\"event\":\"line-{index}\"}}\n",
                time.to_rfc3339()
            ),
        );
    }
    let since = (now - chrono::Duration::days(1)).timestamp_millis();
    let mut before = None;
    let mut seen = HashSet::new();
    let mut pages = 0;
    loop {
        let page = read_history(&path, before, since, LevelFilter::All, "").expect("history page");
        assert!(!page.lines.is_empty(), "page {pages} must not be empty");
        for line in &page.lines {
            assert!(
                seen.insert(line.id),
                "line {:?} repeated across history pages",
                line.id
            );
        }
        pages += 1;
        match page.older {
            Some(older) => before = Some(older),
            None => break,
        }
        assert!(pages < 10, "history pagination did not terminate");
    }
    assert_eq!(pages, 3);
    assert_eq!(seen.len(), 300);
}

#[test]
fn history_filter_pages_do_not_repeat_matching_lines() {
    let dir = TempDir::new("logstream-history-filter");
    let path = dir.path().join("service.log");
    let now = Local::now();
    for index in 0..300 {
        let level = if index % 2 == 0 { "error" } else { "info" };
        let time = now - chrono::Duration::seconds((300 - index) as i64);
        append(
            &path,
            &format!(
                "{{\"time\":\"{}\",\"level\":\"{level}\",\"event\":\"line-{index}\"}}\n",
                time.to_rfc3339()
            ),
        );
    }
    let since = (now - chrono::Duration::days(1)).timestamp_millis();
    let mut before = None;
    let mut seen = HashSet::new();
    let mut pages = 0;
    loop {
        let page = read_history(&path, before, since, LevelFilter::Error, "").expect("history page");
        for line in &page.lines {
            assert_eq!(line.entry.level, "error");
            assert!(
                seen.insert(line.id),
                "line {:?} repeated across history pages",
                line.id
            );
        }
        pages += 1;
        match page.older {
            Some(older) => before = Some(older),
            None => break,
        }
        assert!(pages < 10, "history pagination did not terminate");
    }
    assert_eq!(seen.len(), 150);
}

#[test]
fn history_excludes_lines_outside_the_period_and_undated_lines() {
    let dir = TempDir::new("logstream-history-period");
    let path = dir.path().join("service.log");
    let now = Local::now();
    let today = now - chrono::Duration::hours(1);
    let old = now - chrono::Duration::days(40);
    append(
        &path,
        &format!(
            "{{\"time\":\"{}\",\"level\":\"info\",\"event\":\"today\"}}\n",
            today.to_rfc3339()
        ),
    );
    append(
        &path,
        &format!(
            "{{\"time\":\"{}\",\"level\":\"info\",\"event\":\"old\"}}\n",
            old.to_rfc3339()
        ),
    );
    append(&path, "raw undated\n");

    let since = (now - chrono::Duration::days(30)).timestamp_millis();
    let page = read_history(&path, None, since, LevelFilter::All, "").expect("history page");
    assert_eq!(page.lines.len(), 1);
    assert!(page.lines[0].entry.text.contains("today"), "{}", page.lines[0].entry.text);
}

#[test]
fn a_history_cursor_past_the_file_is_an_error_not_an_empty_page() {
    let dir = TempDir::new("logstream-history-cursor");
    let path = dir.path().join("service.log");
    append(&path, "line-1\n");
    let length = path.metadata().unwrap().len();

    let error = read_history(&path, Some(length + 1), 0, LevelFilter::All, "")
        .expect_err("a cursor past the file must fail");
    assert!(error.to_string().contains("轮换"), "{error}");
}

#[test]
fn expanded_detail_keeps_the_full_sanitized_text_for_copy() {
    let dir = TempDir::new("logstream-detail");
    let path = dir.path().join("service.log");
    let raw = "{\"time\":\"2026-11-20T10:00:00+08:00\",\"level\":\"error\",\"event\":\"request_summary\",\"request_id\":\"req-1\",\"protocol\":\"chat\",\"final\":\"502\",\"tries\":3,\"authorization\":\"Bearer sk-abc123\",\"api_key\":\"secret\"}";
    append(&path, &format!("{raw}\n"));
    let stream = LogStream::open(&path).expect("open log");
    let line = &stream.lines()[0];

    assert!(line.entry.text.contains("request_summary req-1 chat final=502 tries=3"));
    assert!(
        line.detail.contains("\"request_id\":\"req-1\""),
        "the expanded text must keep the whole record: {}",
        line.detail
    );
    assert!(line.detail.len() > line.entry.text.len());
    for secret in ["abc123", "secret"] {
        assert!(!line.detail.contains(secret), "{secret} leaked in {}", line.detail);
    }
    assert!(line.detail.contains("[redacted]"), "{}", line.detail);
}

#[cfg(windows)]
#[test]
fn replacement_with_the_same_head_and_length_resets_by_file_identity() {
    let dir = TempDir::new("logstream-replace-identity");
    let path = dir.path().join("service.log");
    let head = "x".repeat(300);
    append(&path, &format!("{head}\nold\n"));
    let mut stream = LogStream::open(&path).expect("open log");
    assert_eq!(texts(&stream), vec![head.clone(), "old".to_string()]);

    std::fs::remove_file(&path).unwrap();
    append(&path, &format!("{head}\nnew\n"));
    let outcome = stream.poll().expect("poll replacement");
    assert!(
        outcome.reset,
        "a replaced file must reset even when its head and length match"
    );
    assert_eq!(texts(&stream), vec![head, "new".to_string()]);
}

#[test]
fn append_to_poll_latency_p95_stays_under_500_ms() {
    let dir = TempDir::new("logstream-latency");
    let path = dir.path().join("service.log");
    append(&path, "");
    let mut stream = LogStream::open(&path).expect("open log");
    let mut samples = Vec::with_capacity(100);
    for index in 0..100 {
        append(&path, &format!("latency-{index}\n"));
        let started = std::time::Instant::now();
        let outcome = stream.poll().expect("poll latency line");
        assert_eq!(outcome.appended.len(), 1);
        samples.push(started.elapsed());
    }
    samples.sort();
    let p95 = samples[(samples.len() * 95 / 100).min(samples.len() - 1)];
    println!(
        "append-to-poll read latency p95 over {} samples: {p95:?}",
        samples.len()
    );
    assert!(
        p95 <= std::time::Duration::from_millis(500),
        "p95 read latency {p95:?} exceeded 500ms"
    );
}

#[test]
fn a_live_log_reports_a_missing_file_once_as_the_empty_state() {
    let dir = TempDir::new("logstream-live-missing");
    let path = dir.path().join("gateway.log");
    let mut live = LiveLog::new(&path);

    let tick = live
        .read(false)
        .expect("a missing log must yield the empty state");
    assert_eq!(tick.failure, None, "a missing log is not a failure");
    assert!(tick.reset, "the empty state must replace the page rows");
    assert!(tick.entries.is_empty());
    assert!(
        live.read(false).is_none(),
        "the empty state must be reported once, not every tick"
    );
    assert!(
        live.read(true).is_some(),
        "an explicit refresh must re-read the empty state"
    );

    append(&path, "{\"event\":\"first\"}\n");
    let tick = live.read(false).expect("the appeared log must open");
    assert_eq!(tick.failure, None);
    assert_eq!(tick.entries.len(), 1);
    assert!(tick.entries[0].entry.text.contains("first"));
    assert!(tick.reset);
    assert!(live.read(false).is_none(), "a quiet log must not rebuild");
}

#[test]
fn a_live_log_reports_a_read_failure_once_until_the_file_recovers() {
    let dir = TempDir::new("logstream-live-failure");
    let path = dir.path().join("gateway.log");
    std::fs::create_dir(&path).unwrap();
    let mut live = LiveLog::new(&path);

    let tick = live.read(false).expect("a directory must fail");
    let failure = tick.failure.clone().expect("a directory is a read failure");
    assert!(failure.contains("gateway.log"), "{failure}");
    assert!(tick.entries.is_empty());
    assert!(
        live.read(false).is_none(),
        "the same failure must be reported once"
    );
    assert!(
        live.read(true).is_some(),
        "an explicit refresh must retry and report the failure again"
    );

    std::fs::remove_dir(&path).unwrap();
    append(&path, "{\"event\":\"recovered\"}\n");
    let tick = live.read(false).expect("the recovered log must open");
    assert_eq!(tick.failure, None);
    assert!(tick.reset);
    assert_eq!(tick.entries.len(), 1);
    assert!(tick.entries[0].entry.text.contains("recovered"));
}
