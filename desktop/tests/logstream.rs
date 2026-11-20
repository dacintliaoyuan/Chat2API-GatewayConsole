use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

use um_desktop::logstream::{LevelFilter, LogStream};

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
fn a_missing_file_is_an_error_with_the_path_not_an_empty_window() {
    let dir = TempDir::new("logstream-missing");
    let path = dir.path().join("missing.log");
    let error = LogStream::open(&path).expect_err("missing log must fail");
    assert!(error.contains("missing.log"), "{error}");
    assert!(error.contains("读取日志失败"), "{error}");

    append(&path, "appeared\n");
    let stream = LogStream::open(&path).expect("open appeared log");
    assert_eq!(texts(&stream), vec!["appeared"]);
}

#[test]
fn a_directory_is_an_error_not_an_empty_window() {
    let dir = TempDir::new("logstream-directory");
    let error = LogStream::open(dir.path()).expect_err("a directory must not open as a log");
    assert!(error.contains("logstream-directory"), "{error}");
    assert!(
        error.contains("读取日志失败") || error.contains("不是文件"),
        "{error}"
    );
}

#[test]
fn a_poll_error_keeps_the_previous_window_for_the_next_retry() {
    let dir = TempDir::new("logstream-poll-error");
    let path = dir.path().join("service.log");
    append(&path, "a\n");
    let mut stream = LogStream::open(&path).expect("open log");
    std::fs::remove_file(&path).unwrap();

    let error = stream.poll().expect_err("deleted log must fail");
    assert!(error.contains("service.log"), "{error}");
    assert_eq!(
        texts(&stream),
        vec!["a"],
        "a failed poll must keep the shown lines"
    );

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
    assert_eq!(texts(&stream).last().unwrap(), "after-50");
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
    assert_eq!(
        std::fs::read_to_string(&path).unwrap().trim_end(),
        raw,
        "the service log itself must not be rewritten"
    );
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
    assert!(
        p95 <= std::time::Duration::from_millis(500),
        "p95 read latency {p95:?} exceeded 500ms"
    );
}
