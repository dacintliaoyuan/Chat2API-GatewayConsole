use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT: AtomicU64 = AtomicU64::new(0);

pub struct TempDir {
    path: PathBuf,
}

impl TempDir {
    pub fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "um-desktop-{label}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir_all(&path).expect("create temp dir");
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

/// Reads one file's DACL and reports `(ace_count, every_ace_is_the_current_user)`.
#[cfg(windows)]
#[allow(dead_code)]
pub fn acl_ace_count(path: &Path) -> (u32, bool) {
    use std::ffi::c_void;
    use std::mem::{offset_of, size_of};
    use std::os::windows::ffi::OsStrExt;

    use windows::core::{BOOL, PCWSTR};
    use windows::Win32::Foundation::{LocalFree, ERROR_SUCCESS, HLOCAL};
    use windows::Win32::Security::Authorization::{GetNamedSecurityInfoW, SE_FILE_OBJECT};
    use windows::Win32::Security::{
        AclSizeInformation, EqualSid, GetAce, GetAclInformation, GetSecurityDescriptorDacl,
        GetTokenInformation, TokenUser, ACL_SIZE_INFORMATION, DACL_SECURITY_INFORMATION,
        PSECURITY_DESCRIPTOR, PSID, TOKEN_QUERY, TOKEN_USER,
    };
    use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

    unsafe {
        let mut token = windows::Win32::Foundation::HANDLE::default();
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token).expect("token");
        let mut size = 0u32;
        let _ = GetTokenInformation(token, TokenUser, None, 0, &mut size);
        let mut user_buffer = vec![0u8; size as usize];
        GetTokenInformation(
            token,
            TokenUser,
            Some(user_buffer.as_mut_ptr().cast::<c_void>()),
            size,
            &mut size,
        )
        .expect("token user");
        let _ = windows::Win32::Foundation::CloseHandle(token);
        let current = (*user_buffer.as_ptr().cast::<TOKEN_USER>()).User.Sid;

        let wide: Vec<u16> = path
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let mut dacl = std::ptr::null_mut();
        let mut descriptor = PSECURITY_DESCRIPTOR::default();
        let result = GetNamedSecurityInfoW(
            PCWSTR(wide.as_ptr()),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            Some(&mut dacl),
            None,
            &mut descriptor,
        );
        assert_eq!(result, ERROR_SUCCESS);
        let mut present = BOOL::default();
        let mut dacl_from_sd = std::ptr::null_mut();
        let mut defaulted = BOOL::default();
        GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl_from_sd, &mut defaulted)
            .expect("descriptor dacl");
        assert!(present.as_bool());
        let mut info = ACL_SIZE_INFORMATION::default();
        GetAclInformation(
            dacl_from_sd,
            (&mut info as *mut ACL_SIZE_INFORMATION).cast::<c_void>(),
            size_of::<ACL_SIZE_INFORMATION>() as u32,
            AclSizeInformation,
        )
        .expect("acl info");
        let mut only_current_user = true;
        for index in 0..info.AceCount {
            let mut ace = std::ptr::null_mut();
            GetAce(dacl_from_sd, index, &mut ace).expect("ace");
            let sid = PSID((ace.cast::<u8>()).add(offset_of!(
                windows::Win32::Security::ACCESS_ALLOWED_ACE,
                SidStart
            )) as *mut c_void);
            only_current_user &= EqualSid(sid, current).is_ok();
        }
        let _ = LocalFree(Some(HLOCAL(descriptor.0)));
        (info.AceCount, only_current_user)
    }
}
