//! AC1: the real POSIX AF_UNIX adapter.
//!
//! The adapter only moves the endpoint facts: the policy decisions live in
//! [`crate::socket_policy`] and [`crate::peer`]. The parent directory is
//! created 0700 (and an existing wider directory is refused), the socket file is
//! 0600, a live endpoint is refused and only a dead socket file is reclaimed,
//! and the peer identity is `SO_PEERCRED` kernel evidence.

use std::fs;
use std::io;
use std::os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt};
use std::os::unix::io::{AsRawFd, RawFd};
use std::os::unix::net::{UnixListener as StdUnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::peer::PeerCredentials;
use crate::socket_policy::{
    plan_bind_endpoint, plan_connect_endpoint, plan_private_directory, validate_unix_socket_path,
    DirectoryFacts, DirectoryPlan, EndpointFacts, EndpointLiveness, EndpointPlan,
    SocketPolicyError, UNIX_SOCKET_DIRECTORY_MODE, UNIX_SOCKET_FILE_MODE,
};

/// One bound management/instance endpoint owned by this server.
#[derive(Debug)]
pub struct UnixListener {
    listener: StdUnixListener,
    path: PathBuf,
    identity: (u64, u64),
    closed: bool,
}

impl UnixListener {
    /// Bind the endpoint, creating a private parent directory when absent.
    pub fn bind(path: &Path) -> Result<Self, SocketPolicyError> {
        let path = validate_unix_socket_path(path)?;
        let parent = path.parent().ok_or_else(|| {
            SocketPolicyError::InvalidPath("unix socket path has no parent directory".to_string())
        })?;
        ensure_private_directory(parent)?;
        reclaim_stale_socket(&path)?;
        let listener = StdUnixListener::bind(&path).map_err(io_error)?;
        let bound = fs::symlink_metadata(&path).map_err(io_error)?;
        let mode = bound.mode() & 0o777;
        if !bound.file_type().is_socket() || mode != UNIX_SOCKET_FILE_MODE {
            drop(listener);
            let _ = fs::remove_file(&path);
            return Err(SocketPolicyError::AccessDenied(format!(
                "unix socket file must be 0600: {}",
                path.display()
            )));
        }
        let identity = (bound.dev(), bound.ino());
        Ok(Self {
            listener,
            path,
            identity,
            closed: false,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Accept one connection and capture its kernel peer credentials.
    pub fn accept(&self) -> io::Result<UnixConnection> {
        let (stream, _) = self.listener.accept()?;
        Ok(UnixConnection { stream })
    }

    pub fn set_nonblocking(&self, nonblocking: bool) -> io::Result<()> {
        self.listener.set_nonblocking(nonblocking)
    }

    pub fn local_addr(&self) -> io::Result<std::os::unix::net::SocketAddr> {
        self.listener.local_addr()
    }

    /// Stop serving and remove THIS server's socket file. A replacement another
    /// process created at the same path is never unlinked.
    pub fn close(&mut self) -> io::Result<()> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        let result = self.listener.set_nonblocking(true);
        let _ = result;
        if let Ok(metadata) = fs::symlink_metadata(&self.path) {
            if metadata.file_type().is_socket() && (metadata.dev(), metadata.ino()) == self.identity
            {
                fs::remove_file(&self.path)?;
            }
        }
        Ok(())
    }
}

impl Drop for UnixListener {
    fn drop(&mut self) {
        if !self.closed {
            let _ = self.close();
        }
    }
}

/// One connected AF_UNIX peer.
#[derive(Debug)]
pub struct UnixConnection {
    stream: UnixStream,
}

impl UnixConnection {
    /// Connect to a private 0600 endpoint, refusing a wider one first.
    pub fn connect(path: &Path) -> Result<Self, SocketPolicyError> {
        let path = validate_unix_socket_path(path)?;
        let parent = path.parent().ok_or_else(|| {
            SocketPolicyError::InvalidPath("unix socket path has no parent directory".to_string())
        })?;
        check_private_directory(parent)?;
        let metadata = fs::symlink_metadata(&path).map_err(|error| {
            if error.kind() == io::ErrorKind::NotFound {
                SocketPolicyError::NotASocket(format!(
                    "unix socket endpoint is not a socket: {}",
                    path.display()
                ))
            } else {
                io_error(error)
            }
        })?;
        let facts = EndpointFacts {
            path: path.clone(),
            exists: true,
            is_socket: metadata.file_type().is_socket(),
            mode: metadata.mode() & 0o777,
        };
        if plan_connect_endpoint(&facts)? != EndpointPlan::Connect {
            return Err(SocketPolicyError::AccessDenied(format!(
                "unix socket endpoint is not connectable: {}",
                path.display()
            )));
        }
        let stream = UnixStream::connect(&path).map_err(|error| match error.kind() {
            io::ErrorKind::PermissionDenied => SocketPolicyError::AccessDenied(format!(
                "unix socket endpoint is not reachable: {}",
                path.display()
            )),
            io::ErrorKind::NotFound | io::ErrorKind::ConnectionRefused => {
                SocketPolicyError::NotASocket(format!(
                    "unix socket endpoint is not a socket: {}",
                    path.display()
                ))
            }
            _ => io_error(error),
        })?;
        Ok(Self { stream })
    }

    pub fn stream(&self) -> &UnixStream {
        &self.stream
    }

    pub fn try_clone(&self) -> io::Result<Self> {
        Ok(Self {
            stream: self.stream.try_clone()?,
        })
    }

    pub fn set_read_timeout(&self, timeout: Option<Duration>) -> io::Result<()> {
        self.stream.set_read_timeout(timeout)
    }

    pub fn set_write_timeout(&self, timeout: Option<Duration>) -> io::Result<()> {
        self.stream.set_write_timeout(timeout)
    }

    pub fn shutdown(&self, how: std::net::Shutdown) -> io::Result<()> {
        self.stream.shutdown(how)
    }

    /// `SO_PEERCRED` kernel evidence; `None` is a refusal, never a peer.
    pub fn peer_credentials(&self) -> Option<PeerCredentials> {
        peer_credentials(self.stream.as_raw_fd())
    }
}

impl io::Read for UnixConnection {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        io::Read::read(&mut self.stream, buffer)
    }
}

impl io::Write for UnixConnection {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        io::Write::write(&mut self.stream, buffer)
    }

    fn flush(&mut self) -> io::Result<()> {
        io::Write::flush(&mut self.stream)
    }
}

fn ensure_private_directory(path: &Path) -> Result<(), SocketPolicyError> {
    let metadata = match fs::metadata(path) {
        Ok(metadata) => Some(metadata),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(io_error(error)),
    };
    let facts = match metadata {
        Some(metadata) => DirectoryFacts {
            path: path.to_path_buf(),
            exists: true,
            is_directory: metadata.is_dir(),
            mode: metadata.mode() & 0o777,
        },
        None => DirectoryFacts {
            path: path.to_path_buf(),
            exists: false,
            is_directory: false,
            mode: 0,
        },
    };
    match plan_private_directory(&facts)? {
        DirectoryPlan::Ready => Ok(()),
        DirectoryPlan::Create => {
            let mut builder = fs::DirBuilder::new();
            builder.recursive(true).mode(UNIX_SOCKET_DIRECTORY_MODE);
            builder.create(path).map_err(io_error)?;
            // The umask applies to the create; an existing/wider result is a
            // refusal, exactly like the Python 0700 check after makedirs.
            let created = fs::metadata(path).map_err(io_error)?;
            let facts = DirectoryFacts {
                path: path.to_path_buf(),
                exists: true,
                is_directory: created.is_dir(),
                mode: created.mode() & 0o777,
            };
            match plan_private_directory(&facts)? {
                DirectoryPlan::Ready => Ok(()),
                DirectoryPlan::Create => unreachable!("a created directory exists"),
            }
        }
    }
}

fn check_private_directory(path: &Path) -> Result<(), SocketPolicyError> {
    let metadata = fs::metadata(path).map_err(|error| {
        if error.kind() == io::ErrorKind::NotFound {
            SocketPolicyError::NotASocket(format!(
                "unix socket endpoint is not a socket: {}",
                path.display()
            ))
        } else {
            io_error(error)
        }
    })?;
    let facts = DirectoryFacts {
        path: path.to_path_buf(),
        exists: true,
        is_directory: metadata.is_dir(),
        mode: metadata.mode() & 0o777,
    };
    match plan_private_directory(&facts)? {
        DirectoryPlan::Ready => Ok(()),
        DirectoryPlan::Create => unreachable!("an observed directory exists"),
    }
}

fn reclaim_stale_socket(path: &Path) -> Result<(), SocketPolicyError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(io_error(error)),
    };
    let facts = EndpointFacts {
        path: path.to_path_buf(),
        exists: true,
        is_socket: metadata.file_type().is_socket(),
        mode: metadata.mode() & 0o777,
    };
    let liveness = if facts.is_socket {
        Some(probe_liveness(path))
    } else {
        None
    };
    match plan_bind_endpoint(&facts, liveness)? {
        EndpointPlan::Bind => Ok(()),
        EndpointPlan::Reclaim => fs::remove_file(path).map_err(io_error),
        EndpointPlan::Connect => unreachable!("bind plan never connects"),
    }
}

fn probe_liveness(path: &Path) -> EndpointLiveness {
    match UnixStream::connect(path) {
        Ok(stream) => {
            drop(stream);
            EndpointLiveness::Live
        }
        Err(error) => match error.kind() {
            io::ErrorKind::ConnectionRefused | io::ErrorKind::NotFound => EndpointLiveness::Stale,
            io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => EndpointLiveness::Busy,
            _ => EndpointLiveness::Busy,
        },
    }
}

fn peer_credentials(fd: RawFd) -> Option<PeerCredentials> {
    let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let result = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut credentials as *mut libc::ucred as *mut libc::c_void,
            &mut length,
        )
    };
    if result != 0 || length as usize != std::mem::size_of::<libc::ucred>() {
        return None;
    }
    if credentials.pid <= 0 {
        return None;
    }
    Some(PeerCredentials {
        pid: credentials.pid,
        uid: credentials.uid,
        gid: credentials.gid,
    })
}

fn io_error(error: io::Error) -> SocketPolicyError {
    SocketPolicyError::Io(format!("unix socket operation failed: {error}"))
}
