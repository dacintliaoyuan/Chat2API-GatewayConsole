use std::cell::Cell;

use um_desktop::instance::{activate_existing, ExistingWindow};

struct FakeWindow {
    result: bool,
    calls: Cell<u32>,
}

impl FakeWindow {
    fn new(result: bool) -> Self {
        Self {
            result,
            calls: Cell::new(0),
        }
    }
}

impl ExistingWindow for FakeWindow {
    fn activate(&self) -> bool {
        self.calls.set(self.calls.get() + 1);
        self.result
    }
}

#[test]
fn duplicate_open_activates_the_existing_window() {
    let window = FakeWindow::new(true);
    assert!(activate_existing(&window));
    assert_eq!(window.calls.get(), 1);
}

#[test]
fn a_missing_existing_window_is_reported() {
    let window = FakeWindow::new(false);
    assert!(!activate_existing(&window));
    assert_eq!(window.calls.get(), 1);
}

#[cfg(windows)]
mod windows_mutex {
    use std::sync::atomic::{AtomicU64, Ordering};

    use um_desktop::instance::{open_or_activate, InstanceError};

    use super::FakeWindow;

    static NEXT: AtomicU64 = AtomicU64::new(0);

    fn unique_name(label: &str) -> String {
        format!(
            "Local\\UmDesktopTest-{label}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        )
    }

    #[test]
    fn second_open_is_already_running_and_activates_the_first_window() {
        let name = unique_name("busy");
        let first = open_or_activate(&name, &FakeWindow::new(true)).expect("first instance");
        let window = FakeWindow::new(true);
        match open_or_activate(&name, &window) {
            Err(InstanceError::AlreadyRunning { activated: true }) => {}
            other => panic!("second open must activate the first window, got {other:?}"),
        }
        assert_eq!(window.calls.get(), 1);
        drop(first);
    }

    #[test]
    fn dropping_the_guard_allows_a_new_instance() {
        let name = unique_name("release");
        let first = open_or_activate(&name, &FakeWindow::new(true)).expect("first instance");
        drop(first);
        let _second = open_or_activate(&name, &FakeWindow::new(true)).expect("after release");
    }

    #[test]
    fn duplicate_open_reports_when_activation_failed() {
        let name = unique_name("no-window");
        let _first = open_or_activate(&name, &FakeWindow::new(true)).expect("first instance");
        let window = FakeWindow::new(false);
        match open_or_activate(&name, &window) {
            Err(InstanceError::AlreadyRunning { activated: false }) => {}
            other => panic!("duplicate without a window must report it, got {other:?}"),
        }
        assert_eq!(window.calls.get(), 1);
    }
}
