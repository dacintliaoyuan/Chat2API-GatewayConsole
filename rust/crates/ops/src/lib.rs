//! Rust S8 read-only parity for the share ledger, Cloudflare checks and
//! deployment tools.
//!
//! Production keeps using the Python tools; this crate only consumes the same
//! CLI/JSON contracts. It never writes a ledger, changes a tunnel, runs a backup
//! or switches a deployment.

pub mod deploy;
pub mod ingress;
pub mod ledger;
pub mod public_probe;

pub use deploy::{
    check_deployment_assembly, deployment_assembly_problems, deployment_paths, run_backup_restore,
    run_deploy_script, DeploymentError, DeploymentPaths,
};
pub use ingress::check_ingress;
pub use ledger::{
    read_share_requests, read_share_usage, Clock, LedgerError, RequestOptions, SystemClock,
    UsageOptions,
};
pub use public_probe::{
    aggregate_dns, bounded_check_deadline, check_exit_code, check_public_connection, public_host,
    public_origin, public_source, summarize_check, DnsResolution, DnsSource, ProbeFailure,
    ProbeTransport, TlsCheck,
};
