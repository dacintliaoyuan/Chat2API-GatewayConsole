//! Desktop shell core logic for issue #123.
//!
//! The crate is intentionally Windows-first: the Slint window, the Win32 tray,
//! the named-mutex single instance and the ACL hardening of the saved settings
//! live behind `#[cfg(windows)]`, while the read seams and the state machines
//! stay platform independent so `cargo test` can exercise them.

pub mod config;
pub mod instance;
pub mod logs;
pub mod overview;
pub mod shell;
