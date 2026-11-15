//! S5a: the candidate 8002 console read-only core.
//!
//! The candidate binds only an ephemeral loopback port and never switches
//! 8002. It serves the six frozen read faces from `usagesrv.py` and refuses
//! the non-UM read faces in the cloud role with
//! `503 capability_not_assembled`. No SQLite, ledger or log is written by a
//! read face.

pub mod actions;
pub mod body;
pub mod cloud_ops;
pub mod config;
pub mod display;
pub mod error;
pub mod lock;
pub mod metering;
pub mod operation_manager;
pub mod operations;
pub mod runtime;
pub mod server;
pub mod session;
pub mod share_auth;
pub mod share_read;
pub mod share_write;
pub mod summary;

pub use config::{ConsoleConfig, EntryKind, Role};
pub use error::ConsoleError;
pub use runtime::{
    LinuxPidFileConfig, LinuxPidFileFacts, ProcessProbe, RuntimeError, RuntimeFacts,
    RuntimeSnapshot,
};
pub use server::{AppState, ConsoleServer, ServerError, ServerHandle};
pub use session::SessionState;
pub use share_read::{ShareReadSeams, ShareReadStore, UnwiredSeams};
pub use share_write::{ShareWriteSeams, ShareWriteStore, UnwiredWriteSeams};
pub use summary::{build_cloud_summary, SummaryCache};
