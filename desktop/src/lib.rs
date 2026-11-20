//! Desktop shell core logic for issues #123/#124.
//!
//! The crate is intentionally Windows-first: the Slint window, the Win32 tray,
//! the named-mutex single instance, the gateway process supervision and the ACL
//! hardening of the saved settings live behind `#[cfg(windows)]`, while the read
//! seams and the state machines stay platform independent so `cargo test` can
//! exercise them.

pub mod account;
pub mod config;
pub mod gateway;
pub mod instance;
pub mod logs;
pub mod overview;
pub mod shell;
pub mod tail;
