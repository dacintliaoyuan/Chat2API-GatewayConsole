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
pub trait UpstreamIo: Read + Write + Send {}
impl<T: Read + Write + Send> UpstreamIo for T {}

/// One upstream byte stream behind the transport seam.
pub type UpstreamStream = Box<dyn UpstreamIo>;
