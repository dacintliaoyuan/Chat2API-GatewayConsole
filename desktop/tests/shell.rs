use um_desktop::shell::{ShellAction, ShellEvent, ShellState};

#[test]
fn close_request_hides_to_tray_without_quitting() {
    let mut shell = ShellState::new();
    assert!(shell.visible());
    assert!(!shell.quitting());
    assert_eq!(
        shell.apply(ShellEvent::CloseRequested),
        ShellAction::HideWindow
    );
    assert!(!shell.visible());
    assert!(!shell.quitting());
}

#[test]
fn tray_show_restores_the_hidden_window() {
    let mut shell = ShellState::new();
    shell.apply(ShellEvent::CloseRequested);
    assert_eq!(shell.apply(ShellEvent::TrayShow), ShellAction::ShowWindow);
    assert!(shell.visible());
    assert!(!shell.quitting());
}

#[test]
fn tray_quit_quits_the_app_without_touching_the_gateway() {
    let mut shell = ShellState::new();
    assert_eq!(shell.apply(ShellEvent::TrayQuit), ShellAction::Quit);
    assert!(shell.quitting());
}

#[test]
fn quit_is_sticky_even_after_close_requests() {
    let mut shell = ShellState::new();
    shell.apply(ShellEvent::TrayQuit);
    assert_eq!(shell.apply(ShellEvent::CloseRequested), ShellAction::Quit);
    assert_eq!(shell.apply(ShellEvent::TrayShow), ShellAction::Quit);
}

#[test]
fn hidden_window_stays_hidden_when_close_is_requested_again() {
    let mut shell = ShellState::new();
    shell.apply(ShellEvent::CloseRequested);
    assert_eq!(
        shell.apply(ShellEvent::CloseRequested),
        ShellAction::HideWindow
    );
    assert!(!shell.visible());
}
