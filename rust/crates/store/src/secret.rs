//! POSIX secret protection: `aesgcm-file-key-v1`.
//!
//! The Rust cloud store writes the SAME scheme `share/secret_protection.py` owns:
//! AES-256-GCM with a 32-byte key file, AAD `aesgcm-file-key-v1`, blob layout
//! `nonce || ciphertext || tag`. A DPAPI row (the Windows-only scheme) is
//! never guessed at: it is `SecretUndecryptable` on this platform, exactly like
//! Python's POSIX dispatch. A missing/damaged/loosely-permissioned key file
//! fails closed and never degrades to a hash-only row.

use std::path::{Path, PathBuf};

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use sha2::{Digest, Sha256};

use crate::error::StoreError;

pub const SECRET_ALGO_AESGCM_FILE_KEY: &str = "aesgcm-file-key-v1";
pub const SECRET_ALGO_DPAPI_USER: &str = "dpapi-user-scope-v1";
pub const KEY_FILE_ENV: &str = "CHAT2API_SHARE_SECRET_KEY_FILE";
pub const KEY_SIZE: usize = 32;
pub const HEX_KEY_SIZE: usize = KEY_SIZE * 2;
pub const NONCE_SIZE: usize = 12;
pub const TAG_SIZE: usize = 16;
pub const MAX_KEY_FILE_BYTES: usize = 1024;

fn aad() -> &'static [u8] {
    SECRET_ALGO_AESGCM_FILE_KEY.as_bytes()
}

/// Default key file: `<repo>/data/share-secret.key` (never inside a release).
pub fn default_key_file() -> PathBuf {
    match std::env::var_os(KEY_FILE_ENV) {
        Some(value) => PathBuf::from(value),
        None => Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("..")
            .join("data")
            .join("share-secret.key"),
    }
}

/// Read and validate the AES-GCM key file: 32 raw bytes or 64 hex chars,
/// POSIX mode 0600 and owned by the current uid.
pub fn load_key_file(path: Option<&Path>) -> Result<Vec<u8>, StoreError> {
    let target = path.map(Path::to_path_buf).unwrap_or_else(default_key_file);
    let raw = read_key_bytes(&target)?;
    decode_key(&raw, &target)
}

fn read_key_bytes(target: &Path) -> Result<Vec<u8>, StoreError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

        let file = std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc_o_nofollow())
            .open(target)
            .map_err(|_| {
                secret_error(format!(
                    "share secret key file unavailable: {}",
                    target.display()
                ))
            })?;
        let metadata = file.metadata().map_err(|_| {
            secret_error(format!(
                "share secret key file unavailable: {}",
                target.display()
            ))
        })?;
        if !metadata.is_file() {
            return Err(secret_error(format!(
                "share secret key file is not a regular file: {}",
                target.display()
            )));
        }
        if metadata.uid() != current_uid() {
            return Err(secret_error(format!(
                "share secret key file is not owned by the current uid: {}",
                target.display()
            )));
        }
        if metadata.mode() & 0o777 != 0o600 {
            return Err(secret_error(format!(
                "share secret key file must have mode 0600 (found {:04o}): {}",
                metadata.mode() & 0o777,
                target.display()
            )));
        }
    }
    let mut file = std::fs::File::open(target).map_err(|_| {
        secret_error(format!(
            "share secret key file unavailable: {}",
            target.display()
        ))
    })?;
    let mut raw = Vec::new();
    std::io::Read::read_to_end(&mut file, &mut raw).map_err(|_| {
        secret_error(format!(
            "share secret key file unreadable: {}",
            target.display()
        ))
    })?;
    if raw.len() > MAX_KEY_FILE_BYTES {
        return Err(secret_error(format!(
            "share secret key file is unexpectedly large: {}",
            target.display()
        )));
    }
    Ok(raw)
}

#[cfg(unix)]
fn libc_o_nofollow() -> i32 {
    0o40000
}

#[cfg(unix)]
fn current_uid() -> u32 {
    // SAFETY: `geteuid` has no preconditions.
    unsafe { libc::geteuid() }
}

fn decode_key(raw: &[u8], target: &Path) -> Result<Vec<u8>, StoreError> {
    if raw.len() == KEY_SIZE {
        return Ok(raw.to_vec());
    }
    let text = raw
        .iter()
        .copied()
        .skip_while(u8::is_ascii_whitespace)
        .take_while(|byte| !byte.is_ascii_whitespace())
        .collect::<Vec<u8>>();
    if text.len() == HEX_KEY_SIZE {
        let material = decode_hex(&text).ok_or_else(|| {
            secret_error(format!(
                "share secret key file is not valid hexadecimal: {}",
                target.display()
            ))
        })?;
        if material.len() != KEY_SIZE {
            return Err(secret_error(format!(
                "share secret key file must decode to exactly {KEY_SIZE} bytes: {}",
                target.display()
            )));
        }
        return Ok(material);
    }
    Err(secret_error(format!(
        "share secret key file must hold 32 raw bytes or 64 hexadecimal characters: {}",
        target.display()
    )))
}

fn decode_hex(text: &[u8]) -> Option<Vec<u8>> {
    if text.len() % 2 != 0 {
        return None;
    }
    let mut output = Vec::with_capacity(text.len() / 2);
    for pair in text.chunks(2) {
        let high = hex_value(pair[0])?;
        let low = hex_value(pair[1])?;
        output.push(high << 4 | low);
    }
    Some(output)
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn secret_error(message: String) -> StoreError {
    // `share/store.py` wraps a protection failure in a plain StoreError; the
    // message stays the Python one so a caller cannot mistake it for a
    // hash-only degradation.
    StoreError::Other(message)
}

/// Seal one secret exactly like `protect_secret_aesgcm`.
pub fn protect_with_key(secret: &str, key: &[u8]) -> Result<Vec<u8>, StoreError> {
    let nonce = random_nonce()?;
    protect_with_nonce(secret, key, &nonce)
}

/// Seal one secret with an explicit nonce (fixture comparison only).
pub fn protect_with_nonce(
    secret: &str,
    key: &[u8],
    nonce: &[u8; NONCE_SIZE],
) -> Result<Vec<u8>, StoreError> {
    let cipher = cipher(key)?;
    let sealed = cipher
        .encrypt(
            Nonce::from_slice(nonce),
            Payload {
                msg: secret.as_bytes(),
                aad: aad(),
            },
        )
        .map_err(|_| secret_error("share secret protection failed".to_string()))?;
    let mut blob = Vec::with_capacity(NONCE_SIZE + sealed.len());
    blob.extend_from_slice(nonce);
    blob.extend_from_slice(&sealed);
    Ok(blob)
}

/// Open one `aesgcm-file-key-v1` blob strictly by its marker.
pub fn unprotect_with_key(blob: &[u8], algo: &str, key: &[u8]) -> Result<String, StoreError> {
    if algo != SECRET_ALGO_AESGCM_FILE_KEY {
        return Err(StoreError::SecretUndecryptable(format!(
            "unknown share secret algorithm {algo:?}"
        )));
    }
    if blob.len() < NONCE_SIZE + TAG_SIZE {
        return Err(StoreError::SecretUndecryptable(
            "share secret ciphertext is truncated".to_string(),
        ));
    }
    let cipher = cipher(key)?;
    let plaintext = cipher
        .decrypt(
            Nonce::from_slice(&blob[..NONCE_SIZE]),
            Payload {
                msg: &blob[NONCE_SIZE..],
                aad: aad(),
            },
        )
        .map_err(|_| {
            StoreError::SecretUndecryptable(
                "share secret ciphertext failed authentication (wrong key or damaged blob)"
                    .to_string(),
            )
        })?;
    String::from_utf8(plaintext).map_err(|_| {
        StoreError::SecretUndecryptable("share secret plaintext is not UTF-8".to_string())
    })
}

fn cipher(key: &[u8]) -> Result<Aes256Gcm, StoreError> {
    if key.len() != KEY_SIZE {
        return Err(secret_error(format!(
            "AES-GCM share secret key must be {KEY_SIZE} bytes"
        )));
    }
    Aes256Gcm::new_from_slice(key)
        .map_err(|_| secret_error("AES-GCM share secret key is invalid".to_string()))
}

fn random_nonce() -> Result<[u8; NONCE_SIZE], StoreError> {
    let mut nonce = [0u8; NONCE_SIZE];
    getrandom::getrandom(&mut nonce)
        .map_err(|_| secret_error("share secret protection failed".to_string()))?;
    Ok(nonce)
}

/// SHA-256 hex digest, exactly Python's `_hash`.
pub fn secret_hash(secret: &str) -> String {
    let digest = Sha256::digest(secret.as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Constant-time digest comparison (Python's `hmac.compare_digest`).
pub fn secret_eq(secret: &str, digest: &str) -> bool {
    let expected = secret_hash(secret);
    if expected.len() != digest.len() {
        return false;
    }
    let mut difference = 0u8;
    for (left, right) in expected.bytes().zip(digest.bytes()) {
        difference |= left ^ right;
    }
    difference == 0
}

/// Display mask: `um-share-****` + the last four characters.
pub fn mask_secret(secret: &str) -> String {
    let tail: String = secret
        .chars()
        .rev()
        .take(4)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    if secret.chars().count() >= 4 {
        format!("um-share-****{tail}")
    } else {
        "um-share-****".to_string()
    }
}
