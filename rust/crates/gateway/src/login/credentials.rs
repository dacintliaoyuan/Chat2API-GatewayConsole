//! UMPASS form credentials, independent of browser-owned saved passwords.
use std::sync::Arc;

use zeroize::Zeroizing;

pub const WINDOWS_CREDENTIAL_TARGET: &str = "Chat2API-GatewayConsole/UM/local";

/// Deliberately has no Debug/Serialize implementation.
pub struct UmpassCredentials {
    pub account: Zeroizing<String>,
    pub password: Zeroizing<String>,
}

impl UmpassCredentials {
    pub fn new(account: String, password: String) -> Result<Self, CredentialSourceError> {
        let account = Zeroizing::new(account);
        let password = Zeroizing::new(password);
        if account.trim().is_empty()
            || password.is_empty()
            || account.contains('\0')
            || password.contains('\0')
        {
            return Err(CredentialSourceError::InvalidRecord);
        }
        Ok(Self { account, password })
    }
}

/// Only stable categories/codes can reach status; never include API buffers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialSourceError {
    IncompleteEnvironment,
    InvalidRecord,
    Windows(u32),
}

impl CredentialSourceError {
    pub fn reason(self) -> String {
        match self {
            Self::IncompleteEnvironment => "umpass_credentials_environment_incomplete".to_string(),
            Self::InvalidRecord => "umpass_credentials_invalid_record".to_string(),
            Self::Windows(code) => format!("umpass_credentials_windows_error:{code}"),
        }
    }
}

pub trait UmpassCredentialSource: Send + Sync {
    fn load(&self) -> Result<Option<UmpassCredentials>, CredentialSourceError>;
}

pub struct NoStoredCredentials;

impl UmpassCredentialSource for NoStoredCredentials {
    fn load(&self) -> Result<Option<UmpassCredentials>, CredentialSourceError> {
        Ok(None)
    }
}

pub fn local_source() -> Arc<dyn UmpassCredentialSource> {
    #[cfg(windows)]
    {
        Arc::new(windows::WindowsCredentialSource)
    }
    #[cfg(not(windows))]
    {
        Arc::new(NoStoredCredentials)
    }
}

pub fn resolve(
    account: &str,
    password: &str,
    source: &dyn UmpassCredentialSource,
) -> Result<Option<UmpassCredentials>, CredentialSourceError> {
    match (account.is_empty(), password.is_empty()) {
        (true, true) => source.load(),
        (false, false) => {
            UmpassCredentials::new(account.to_string(), password.to_string()).map(Some)
        }
        _ => Err(CredentialSourceError::IncompleteEnvironment),
    }
}

#[cfg(windows)]
mod windows {
    use super::*;
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_NOT_FOUND};
    use windows_sys::Win32::Security::Credentials::{
        CredFree, CredReadW, CREDENTIALW, CRED_MAX_CREDENTIAL_BLOB_SIZE, CRED_TYPE_GENERIC,
    };

    pub struct WindowsCredentialSource;

    impl UmpassCredentialSource for WindowsCredentialSource {
        fn load(&self) -> Result<Option<UmpassCredentials>, CredentialSourceError> {
            read_target(WINDOWS_CREDENTIAL_TARGET)
        }
    }

    struct Allocation(*mut CREDENTIALW);
    impl Drop for Allocation {
        fn drop(&mut self) {
            // CredReadW returns one owned allocation; erase its secret blob
            // before releasing it, including on validation failures.
            unsafe {
                let credential = &*self.0;
                if !credential.CredentialBlob.is_null() {
                    for index in 0..credential.CredentialBlobSize as usize {
                        std::ptr::write_volatile(credential.CredentialBlob.add(index), 0);
                    }
                }
                CredFree(self.0.cast());
            }
        }
    }

    fn read_target(target: &str) -> Result<Option<UmpassCredentials>, CredentialSourceError> {
        let target: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
        let mut pointer = std::ptr::null_mut();
        // Target is NUL-terminated and the out pointer lives through the call.
        if unsafe { CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut pointer) } == 0 {
            let code = unsafe { GetLastError() };
            return if code == ERROR_NOT_FOUND {
                Ok(None)
            } else {
                Err(CredentialSourceError::Windows(code))
            };
        }
        if pointer.is_null() {
            return Err(CredentialSourceError::InvalidRecord);
        }
        let allocation = Allocation(pointer);
        // All pointers are owned by the live CredReadW allocation.
        let record = unsafe { &*allocation.0 };
        if record.Type != CRED_TYPE_GENERIC
            || record.UserName.is_null()
            || record.CredentialBlob.is_null()
            || record.CredentialBlobSize == 0
            || record.CredentialBlobSize > CRED_MAX_CREDENTIAL_BLOB_SIZE
            || record.CredentialBlobSize % 2 != 0
        {
            return Err(CredentialSourceError::InvalidRecord);
        }
        let mut length = 0;
        // WinCred guarantees a NUL-terminated UserName. Bound the scan by its
        // documented maximum rather than accepting an unbounded record.
        while length <= 513 && unsafe { *record.UserName.add(length) } != 0 {
            length += 1;
        }
        if length > 513 {
            return Err(CredentialSourceError::InvalidRecord);
        }
        let account =
            String::from_utf16(unsafe { std::slice::from_raw_parts(record.UserName, length) })
                .map_err(|_| CredentialSourceError::InvalidRecord)?;
        // Blob alignment is not part of its byte-pointer contract.
        let bytes = unsafe {
            std::slice::from_raw_parts(record.CredentialBlob, record.CredentialBlobSize as usize)
        };
        let units = Zeroizing::new(
            bytes
                .chunks_exact(2)
                .map(|b| u16::from_le_bytes([b[0], b[1]]))
                .collect::<Vec<_>>(),
        );
        let password =
            String::from_utf16(&units).map_err(|_| CredentialSourceError::InvalidRecord)?;
        UmpassCredentials::new(account, password).map(Some)
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use windows_sys::Win32::Security::Credentials::{
            CredDeleteW, CredWriteW, CRED_PERSIST_SESSION,
        };

        struct Fixture(Vec<u16>);
        impl Drop for Fixture {
            fn drop(&mut self) {
                // Only delete this test's uniquely named synthetic record.
                unsafe {
                    CredDeleteW(self.0.as_ptr(), CRED_TYPE_GENERIC, 0);
                }
            }
        }

        #[test]
        fn wincred_round_trip_missing_and_corrupt_record_are_distinct() {
            let target = format!(
                "Chat2API-GatewayConsole/test-fixture/{}",
                std::process::id()
            );
            let fixture = Fixture(target.encode_utf16().chain(Some(0)).collect());
            assert!(matches!(read_target(&target), Ok(None)));
            let mut account: Vec<u16> = "fixture-user".encode_utf16().chain(Some(0)).collect();
            let mut blob: Vec<u8> = " 密码 fixture "
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect();
            let mut record = CREDENTIALW {
                Type: CRED_TYPE_GENERIC,
                TargetName: fixture.0.as_ptr() as *mut _,
                UserName: account.as_mut_ptr(),
                CredentialBlob: blob.as_mut_ptr(),
                CredentialBlobSize: blob.len() as u32,
                Persist: CRED_PERSIST_SESSION,
                ..CREDENTIALW::default()
            };
            assert_ne!(unsafe { CredWriteW(&record, 0) }, 0);
            let value = read_target(&target)
                .ok()
                .flatten()
                .expect("synthetic record");
            assert_eq!(&*value.account, "fixture-user");
            assert_eq!(&*value.password, " 密码 fixture ");
            record.CredentialBlobSize = 1;
            assert_ne!(unsafe { CredWriteW(&record, 0) }, 0);
            assert!(matches!(
                read_target(&target),
                Err(CredentialSourceError::InvalidRecord)
            ));
            drop(fixture);
            assert!(matches!(read_target(&target), Ok(None)));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Inaccessible;
    impl UmpassCredentialSource for Inaccessible {
        fn load(&self) -> Result<Option<UmpassCredentials>, CredentialSourceError> {
            Err(CredentialSourceError::Windows(5))
        }
    }

    #[test]
    fn environment_pair_takes_precedence_without_trimming_password_or_mixing_sources() {
        let value = resolve("fixture", " password ", &Inaccessible)
            .ok()
            .flatten()
            .unwrap();
        assert_eq!(&*value.password, " password ");
        assert!(matches!(
            resolve("fixture", "", &Inaccessible),
            Err(CredentialSourceError::IncompleteEnvironment)
        ));
        assert!(matches!(
            resolve("", "password", &Inaccessible),
            Err(CredentialSourceError::IncompleteEnvironment)
        ));
        assert!(matches!(
            resolve("", "", &Inaccessible),
            Err(CredentialSourceError::Windows(5))
        ));
        assert!(matches!(resolve("", "", &NoStoredCredentials), Ok(None)));
        assert!(matches!(
            resolve("fixture", "bad\0password", &NoStoredCredentials),
            Err(CredentialSourceError::InvalidRecord)
        ));
    }
}
