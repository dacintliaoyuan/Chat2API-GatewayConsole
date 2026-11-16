//! Upstream URL parsing and TLS selection.
//!
//! The production upstream is `https://chat.genai.um.edu.mo`; the gateway and
//! the credential probe terminate TLS themselves. A plain-HTTP upstream stays
//! plain (the loopback test/candidate path), and a foreign scheme is rejected.

use um_gateway::config::{parse_upstream_base_url, upstream_uses_tls};

#[test]
fn https_names_the_tls_upstream() {
    assert_eq!(
        parse_upstream_base_url("https://chat.genai.um.edu.mo").unwrap(),
        ("chat.genai.um.edu.mo".to_string(), 443)
    );
    assert!(upstream_uses_tls("https://chat.genai.um.edu.mo"));
}

#[test]
fn plain_http_stays_plain() {
    assert_eq!(
        parse_upstream_base_url("http://127.0.0.1:8100").unwrap(),
        ("127.0.0.1".to_string(), 8100)
    );
    assert!(!upstream_uses_tls("http://127.0.0.1:8100"));
}

#[test]
fn a_foreign_scheme_is_rejected() {
    assert!(parse_upstream_base_url("ftp://example.invalid").is_err());
    assert!(parse_upstream_base_url("chat.genai.um.edu.mo").is_err());
}
