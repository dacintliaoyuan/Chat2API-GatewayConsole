//! Rust gateway: 8003 read-only surface, share-key gate and request rejection.
//!
//! The default candidate mode binds only an ephemeral loopback port. The explicit
//! production mode binds only 8000/8002/8003 on loopback; both modes share the
//! same routes, authentication, ledger and error contracts. It ports the frozen
//! behavior from `docs/rust-contract.md` §5 and the Python
//! `chat2api/chat2api.py` + `chat2api/share_auth.py` implementations. Chat
//! forwarding is S1b; Messages forwarding is S2a; Responses and embeddings are
//! S2b.

pub mod body;
pub mod chat;
pub mod config;
pub mod embeddings;
pub mod error;
pub mod event_log;
pub mod gate;
pub mod http;
pub mod instance;
pub mod ledger;
pub mod login;
pub mod messages;
pub mod nonstream;
pub mod readonly;
pub mod reauth;
pub mod responses;
pub mod retry;
pub mod server;
pub mod tls;
pub mod upstream;

pub use body::{read_json_body, BodyPolicy};
pub use config::{AuthMode, BindMode, GatewayConfig, Role};
pub use error::GatewayError;
pub use gate::{GateState, RefusalReason, ShareGate};
pub use readonly::ProxyState;
pub use server::{AppState, GatewayServer, ServerError, ServerHandle};
