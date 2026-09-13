//! The last scan of each root, kept so the next launch can draw it at once while a fresh scan
//! runs behind it; and the short list of roots scanned recently.
//!
//! Everything lives under ~/Library/Application Support/Disk Usage Visualiser/. One JSON file per
//! root, named by a hash of the path (a path is not a safe file name). Writes are atomic
//! (temp file + rename) so a crash mid-write cannot leave a half-cached scan.

use crate::scan::Node;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Scan {
    pub root: String,
    pub root_name: String,
    /// RFC 3339, local time.
    pub finished: String,
    pub duration_ms: u64,
    pub items: u64,
    pub size: u64,
    pub apparent: u64,
    pub denied: u64,
    pub tree: Node,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Recent {
    pub path: String,
    pub name: String,
    pub size: u64,
    pub items: u64,
    pub when: String,
}

pub const RECENT_MAX: usize = 5;

pub fn dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    Path::new(&home).join("Library/Application Support/Disk Usage Visualiser")
}

fn hash(s: &str) -> String {
    // FNV-1a, 64-bit. Not security, just a stable short name.
    let mut h: u64 = 0xcbf29ce484222325;
    for b in s.bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{h:016x}")
}

pub fn scan_path(root: &str) -> PathBuf {
    dir().join("scans").join(format!("{}.json", hash(root)))
}

fn write_atomic(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, bytes)?;
    fs::rename(tmp, path)
}

pub fn save_scan(s: &Scan) -> Result<(), String> {
    let bytes = serde_json::to_vec(s).map_err(|e| e.to_string())?;
    write_atomic(&scan_path(&s.root), &bytes).map_err(|e| e.to_string())
}

pub fn load_scan(root: &str) -> Option<Scan> {
    let bytes = fs::read(scan_path(root)).ok()?;
    let s: Scan = serde_json::from_slice(&bytes).ok()?;
    if s.root == root { Some(s) } else { None }
}

pub fn scan_file_size(root: &str) -> u64 {
    fs::metadata(scan_path(root)).map(|m| m.len()).unwrap_or(0)
}

fn recent_path() -> PathBuf {
    dir().join("recent.json")
}

pub fn load_recent() -> Vec<Recent> {
    fs::read(recent_path()).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

/// Move (or add) `r` to the front and keep the newest RECENT_MAX.
pub fn push_recent(r: Recent) -> Vec<Recent> {
    let mut list: Vec<Recent> = load_recent().into_iter().filter(|x| x.path != r.path).collect();
    list.insert(0, r);
    list.truncate(RECENT_MAX);
    if let Ok(bytes) = serde_json::to_vec_pretty(&list) {
        let _ = write_atomic(&recent_path(), &bytes);
    }
    list
}

/// The saved root to open with next time. Falls back to "/".
pub fn load_root() -> String {
    fs::read_to_string(dir().join("root.txt")).map(|s| s.trim().to_string()).ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "/".into())
}

pub fn save_root(root: &str) {
    let _ = write_atomic(&dir().join("root.txt"), root.as_bytes());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hash_is_stable_and_distinct() {
        assert_eq!(hash("/"), hash("/"));
        assert_ne!(hash("/"), hash("/Users"));
        assert_eq!(hash("/").len(), 16);
    }

    #[test]
    fn scan_roundtrip() {
        let s = Scan {
            root: "/tmp/duv-test-root".into(),
            root_name: "x".into(),
            finished: "2026-09-13T10:00:00+02:00".into(),
            duration_ms: 1,
            items: 2,
            size: 3,
            apparent: 4,
            denied: 0,
            tree: Node { name: "x".into(), size: 3, ..Default::default() },
        };
        let json = serde_json::to_string(&s).unwrap();
        let back: Scan = serde_json::from_str(&json).unwrap();
        assert_eq!(back.tree.size, 3);
        assert!(json.contains("\"n\":\"x\""), "compact field names keep a 300k-node file small");
    }
}
