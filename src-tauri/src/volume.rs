//! The volume a path lives on: its name, capacity and free space.

use serde::Serialize;
use std::ffi::CString;
use std::path::Path;

#[derive(Serialize, Clone, Debug, Default)]
pub struct Volume {
    pub name: String,
    pub total: u64,
    pub free: u64,
    pub used: u64,
}

/// statfs: total and available bytes for the filesystem holding `path`.
pub fn stats(path: &str) -> Option<(u64, u64)> {
    let c = CString::new(path).ok()?;
    let mut st: libc::statfs = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::statfs(c.as_ptr(), &mut st) };
    if rc != 0 {
        return None;
    }
    let bsize = st.f_bsize as u64;
    Some((st.f_blocks * bsize, st.f_bavail * bsize))
}

/// The boot volume's display name. macOS lists it under /Volumes as a symlink to `/`, which is
/// the cheapest place to read the name without loading Foundation.
pub fn boot_volume_name() -> String {
    if let Ok(rd) = std::fs::read_dir("/Volumes") {
        for e in rd.flatten() {
            if let Ok(target) = std::fs::canonicalize(e.path()) {
                if target == Path::new("/") {
                    return e.file_name().to_string_lossy().into_owned();
                }
            }
        }
    }
    "Macintosh HD".into()
}

/// What to call a scan root in the UI: the volume name for `/`, the folder's own name otherwise.
pub fn root_name(root: &str) -> String {
    if root == "/" {
        boot_volume_name()
    } else {
        Path::new(root).file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| root.to_string())
    }
}

pub fn info(root: &str) -> Volume {
    let (total, free) = stats(root).unwrap_or((0, 0));
    Volume { name: root_name(root), total, free, used: total.saturating_sub(free) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn root_has_stats() {
        let (total, free) = stats("/").unwrap();
        assert!(total > 0 && free <= total);
    }

    #[test]
    fn names() {
        assert_eq!(root_name("/Users/x/Projects"), "Projects");
        assert!(!root_name("/").is_empty());
    }
}
