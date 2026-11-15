//! Rust S1a candidate: 8003 read-only surface, share-key gate and request
//! rejection.
//!
//! The candidate binds only an ephemeral loopback port and never switches 8003.
//! It ports the frozen behavior from `docs/rust-contract.md` §5 and the Python
//! `chat2api/chat2api.py` + `chat2api/share_auth.py` implementations. Chat,
//! Messages and Responses forwarding is deliberately left to S1b/S2 and answers
//! `501 not_implemented` only after the route, credential and body gates.

pub mod body;
pub mod chat;
pub mod config;
pub mod error;
pub mod gate;
pub mod http;
pub mod ledger;
pub mod readonly;
pub mod server;
pub mod upstream;

pub use body::{read_json_body, BodyPolicy};
pub use config::{AuthMode, GatewayConfig};
pub use error::GatewayError;
pub use gate::{GateState, RefusalReason, ShareGate};
pub use readonly::ProxyState;
pub use server::{AppState, GatewayServer, ServerError, ServerHandle};
