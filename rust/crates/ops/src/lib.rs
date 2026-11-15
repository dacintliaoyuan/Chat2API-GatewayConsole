//! Rust S8 read-only parity for the share ledger, Cloudflare checks and
//! deployment tools.
//!
//! Production keeps using the Python tools; this crate only consumes the same
//! CLI/JSON contracts. It never writes a ledger, changes a tunnel, runs a backup
//! or switches a deployment.

pub mod ledger;

pub use ledger::{
    read_share_requests, read_share_usage, Clock, LedgerError, RequestOptions, SystemClock,
    UsageOptions,
};
