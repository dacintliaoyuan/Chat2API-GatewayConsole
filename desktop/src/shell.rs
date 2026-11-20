//! Window/tray state transitions for the desktop shell.
//!
//! Closing the main window hides it to the tray; the tray restores the same
//! window; "退出 App" quits the app. This ticket does not stop the gateway
//! (#124), so the state machine only tracks the window and the quit flag.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShellEvent {
    CloseRequested,
    TrayShow,
    TrayQuit,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShellAction {
    HideWindow,
    ShowWindow,
    Quit,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShellState {
    visible: bool,
    quitting: bool,
}

impl ShellState {
    pub fn new() -> Self {
        Self {
            visible: true,
            quitting: false,
        }
    }

    pub fn visible(&self) -> bool {
        self.visible
    }

    pub fn quitting(&self) -> bool {
        self.quitting
    }

    pub fn apply(&mut self, event: ShellEvent) -> ShellAction {
        if self.quitting {
            return ShellAction::Quit;
        }
        match event {
            ShellEvent::CloseRequested => {
                self.visible = false;
                ShellAction::HideWindow
            }
            ShellEvent::TrayShow => {
                self.visible = true;
                ShellAction::ShowWindow
            }
            ShellEvent::TrayQuit => {
                self.quitting = true;
                ShellAction::Quit
            }
        }
    }
}

impl Default for ShellState {
    fn default() -> Self {
        Self::new()
    }
}
