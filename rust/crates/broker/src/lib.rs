//! Broker control-channel kernel (S3a).
//!
//! This crate implements the wire contract frozen in `docs/rust-contract.md` §7:
//! one-line JSON frames bounded by 1 MiB, HMAC-SHA256, a ±300 s timestamp
//! window, one-shot `request_id` replay protection, the fixed action whitelist
//! and the POSIX AF_UNIX endpoint policy. The management action semantics
//! (projection publish, Key lifecycle, host evidence) are implemented by the
//! S3b slice behind [`management::ManagementHandler`].

pub mod actions;
pub mod budget;
pub mod codec;
pub mod error;
pub mod hmac;
pub mod instance;
pub mod management;
pub mod peer;
pub mod publish;
pub mod replay;
pub mod request;
pub mod server;
pub mod socket_policy;
pub mod transport;
