//! TLS transport for the configured upstream.
//!
//! The production upstream (`https://chat.genai.um.edu.mo`) is HTTPS, so the
//! gateway and the credential probe terminate TLS themselves. The root store is
//! the Mozilla set compiled into `webpki-root-certs`; no system trust store,
//! environment variable or operator file is consulted.

use std::io::{self, Read, Write};
use std::net::TcpStream;
use std::sync::{Arc, Once, OnceLock};

use rustls::pki_types::ServerName;
use rustls::{ClientConfig, ClientConnection, RootCertStore, StreamOwned};

/// One TLS stream over the already-connected TCP socket.
pub type TlsStream = StreamOwned<ClientConnection, TcpStream>;

fn config() -> Arc<ClientConfig> {
    static CONFIG: OnceLock<Arc<ClientConfig>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            static INSTALL_PROVIDER: Once = Once::new();
            INSTALL_PROVIDER.call_once(|| {
                let _ = rustls::crypto::ring::default_provider().install_default();
            });
            let mut roots = RootCertStore::empty();
            roots.add_parsable_certificates(
                webpki_root_certs::TLS_SERVER_ROOT_CERTS.iter().cloned(),
            );
            Arc::new(
                ClientConfig::builder()
                    .with_root_certificates(roots)
                    .with_no_client_auth(),
            )
        })
        .clone()
}

/// Wrap a connected TCP socket in a TLS client session for `host`.
pub fn wrap(stream: TcpStream, host: &str) -> io::Result<TlsStream> {
    let server_name = ServerName::try_from(host.to_string())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "invalid TLS host"))?;
    let connection = ClientConnection::new(config(), server_name)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "TLS setup failed"))?;
    Ok(StreamOwned::new(connection, stream))
}

/// A boxed upstream transport: plain TCP or TLS over TCP.
pub trait UpstreamIo: Read + Write + Send {
    fn set_nonblocking(&self, enabled: bool) -> io::Result<()>;
}

impl UpstreamIo for TcpStream {
    fn set_nonblocking(&self, enabled: bool) -> io::Result<()> {
        TcpStream::set_nonblocking(self, enabled)
    }
}

impl UpstreamIo for TlsStream {
    fn set_nonblocking(&self, enabled: bool) -> io::Result<()> {
        self.sock.set_nonblocking(enabled)
    }
}

/// One upstream byte stream behind the transport seam.
pub type UpstreamStream = Box<dyn UpstreamIo>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_compiled_root_store_is_not_empty() {
        let mut roots = RootCertStore::empty();
        let (added, ignored) = roots
            .add_parsable_certificates(webpki_root_certs::TLS_SERVER_ROOT_CERTS.iter().cloned());
        assert!(added > 100, "added={added} ignored={ignored}");
    }

    #[test]
    #[ignore = "reaches the real production upstream"]
    fn the_real_upstream_completes_a_tls_handshake() {
        let stream = TcpStream::connect("chat.genai.um.edu.mo:443").expect("tcp");
        let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(10)));
        let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(10)));
        let mut tls = wrap(stream, "chat.genai.um.edu.mo").expect("tls wrap");
        tls.write_all(
            b"GET /api/models HTTP/1.1\r\nHost: chat.genai.um.edu.mo\r\nAccept: application/json\r\nConnection: close\r\n\r\n",
        )
        .expect("write");
        tls.flush().expect("flush");
        println!(
            "after flush handshaking={} peer_certs={}",
            tls.conn.is_handshaking(),
            tls.conn
                .peer_certificates()
                .map(|certs| certs.len())
                .unwrap_or(0)
        );
        let mut buf = [0u8; 128];
        let read = tls.read(&mut buf).expect("read");
        let head = String::from_utf8_lossy(&buf[..read]).to_string();
        assert!(head.starts_with("HTTP/1.1"), "{head}");
    }
}
