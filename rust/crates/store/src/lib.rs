//! S3b: the share store, Key lifecycle, security journal, projection
//! publishing and the broker's 8003 start identity.
//!
//! The crate owns the Rust side of `share/store.py` (schema 3, Key lifecycle,
//! security journal), `share/broker_protocol.py:parse_projection_payload` (the
//! shared projection parser), `share/runtime.py` (the atomic protected writer)
//! and the publish/host semantics of `share/management.py` and
//! `share/broker_host.py`. It never opens a second SQLite file, never starts a
//! second broker and never launches a second 8003 process.

pub mod error;
pub mod ids;
pub mod projection;
pub mod schema;
pub mod secret;
pub mod store;

pub use error::StoreError;
pub use projection::{
    fingerprint, parse_projection_payload, AllowPath, DenyAll, PosixAcl, ProjectionAcl,
    ProjectionError, ProtectedProjectionWriter,
};
pub use store::{KeyRow, OperationRow, ShareStore};
