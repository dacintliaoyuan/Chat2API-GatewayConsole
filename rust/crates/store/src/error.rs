//! Store error classes with the Python class names and messages.
//!
//! `share/store.py` maps each predictable failure to an HTTP code through its
//! exception class; the Rust store keeps the same names so a caller can map the
//! same way and a test can compare the outcome without guessing.

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoreError {
    Conflict(String),
    NotFound(String),
    Disabled(String),
    VersionMismatch(String),
    MigrationConflict(String),
    SecretUnavailable(String),
    SecretUndecryptable(String),
    Other(String),
}

impl StoreError {
    /// The Python exception class name (`Conflict`, `NotFound`, ...).
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Conflict(_) => "Conflict",
            Self::NotFound(_) => "NotFound",
            Self::Disabled(_) => "Disabled",
            Self::VersionMismatch(_) => "VersionMismatch",
            Self::MigrationConflict(_) => "MigrationConflict",
            Self::SecretUnavailable(_) => "SecretUnavailable",
            Self::SecretUndecryptable(_) => "SecretUndecryptable",
            Self::Other(_) => "StoreError",
        }
    }

    pub fn message(&self) -> &str {
        match self {
            Self::Conflict(message)
            | Self::NotFound(message)
            | Self::Disabled(message)
            | Self::VersionMismatch(message)
            | Self::MigrationConflict(message)
            | Self::SecretUnavailable(message)
            | Self::SecretUndecryptable(message)
            | Self::Other(message) => message,
        }
    }
}

impl fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.message())
    }
}

impl std::error::Error for StoreError {}

impl From<rusqlite::Error> for StoreError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Other(error.to_string())
    }
}
