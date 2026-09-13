//! The scan: a parallel walk of one folder tree that produces a tree of folder sizes.
//!
//! Only folders are kept. The files directly inside a folder are summed into that folder's
//! `files` / `loose_size`, so a full-drive scan of ~1.3M items comes out as a few hundred
//! thousand nodes that serialise in well under a second and draw as a treemap.
//!
//! Two sizes are tracked for every folder:
//!
//!   * `size`     — allocated bytes on disk (`st_blocks * 512`). This is what the treemap draws,
//!                  because the question the app answers is "what is using my disk". A dataless
//!                  file (OneDrive / iCloud "online-only") has a length but no blocks, so it
//!                  counts as nothing here.
//!   * `apparent` — logical bytes (`st_size`). For a cloud folder this is what is in the cloud,
//!                  whether or not it is also on disk, and the UI shows it beside `size`.
//!
//! The walk is one rayon task per directory, which keeps every core busy on an SSD and is
//! naturally work-stealing on a lopsided tree. Progress is a handful of atomics the UI polls;
//! cancellation is an AtomicBool checked before every directory is opened.

use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Node {
    #[serde(rename = "n")]
    pub name: String,
    /// Allocated bytes on disk, whole subtree.
    #[serde(rename = "s")]
    pub size: u64,
    /// Logical bytes, whole subtree.
    #[serde(rename = "a")]
    pub apparent: u64,
    /// Files directly in this folder.
    #[serde(rename = "f")]
    pub files: u64,
    /// Allocated bytes of those files.
    #[serde(rename = "ls")]
    pub loose_size: u64,
    /// Logical bytes of those files.
    #[serde(rename = "la")]
    pub loose_apparent: u64,
    /// Files + folders in the whole subtree, this folder excluded.
    #[serde(rename = "i")]
    pub items: u64,
    /// Under ~/Library/CloudStorage/<provider> or ~/Library/Mobile Documents/<container>.
    #[serde(rename = "c", default, skip_serializing_if = "is_false")]
    pub cloud: bool,
    /// Folders in the subtree that could not be read (permissions), this one included.
    #[serde(rename = "d", default, skip_serializing_if = "is_zero")]
    pub denied: u64,
    #[serde(rename = "k", default)]
    pub kids: Vec<Node>,
}

fn is_false(b: &bool) -> bool {
    !*b
}
fn is_zero(n: &u64) -> bool {
    *n == 0
}

/// Live counters for the scan in flight. Cheap enough to bump per directory.
#[derive(Default)]
pub struct Progress {
    pub items: AtomicU64,
    pub bytes: AtomicU64,
    pub dirs: AtomicU64,
    pub denied: AtomicU64,
    pub current: Mutex<String>,
    pub cancel: AtomicBool,
}

impl Progress {
    pub fn reset(&self) {
        self.items.store(0, Ordering::Relaxed);
        self.bytes.store(0, Ordering::Relaxed);
        self.dirs.store(0, Ordering::Relaxed);
        self.denied.store(0, Ordering::Relaxed);
        self.cancel.store(false, Ordering::Relaxed);
        if let Ok(mut c) = self.current.lock() {
            c.clear();
        }
    }
}

/// Never descended into, whatever the root. `/System/Volumes` is where the Data volume and
/// every firmlink target really live, so walking it from `/` would count the whole disk twice;
/// `/Volumes` is other disks; the rest are virtual.
const SKIP: &[&str] = &["/System/Volumes", "/Volumes", "/dev", "/Network", "/.vol", "/cores", "/private/var/vm"];

fn skipped(p: &Path) -> bool {
    SKIP.iter().any(|s| Path::new(s) == p)
}

/// The top-level folder of a cloud provider: everything under it is "in the cloud".
fn is_cloud_root(p: &Path) -> bool {
    let Some(parent) = p.parent() else { return false };
    let s = parent.to_string_lossy();
    s.ends_with("/Library/CloudStorage") || s.ends_with("/Library/Mobile Documents")
}

pub fn walk(dir: &Path, name: String, cloud: bool, prog: &Progress) -> Option<Node> {
    if prog.cancel.load(Ordering::Relaxed) {
        return None;
    }
    let cloud = cloud || is_cloud_root(dir);
    let rd = match fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => {
            prog.denied.fetch_add(1, Ordering::Relaxed);
            return Some(Node { name, cloud, denied: 1, ..Default::default() });
        }
    };
    prog.dirs.fetch_add(1, Ordering::Relaxed);
    if let Ok(mut c) = prog.current.try_lock() {
        *c = dir.to_string_lossy().into_owned();
    }

    let mut subdirs: Vec<(PathBuf, String)> = Vec::new();
    let (mut files, mut ls, mut la) = (0u64, 0u64, 0u64);
    for e in rd.flatten() {
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_dir() {
            let p = e.path();
            if !skipped(&p) {
                subdirs.push((p, e.file_name().to_string_lossy().into_owned()));
            }
        } else if let Ok(md) = e.metadata() {
            // DirEntry::metadata is an lstat: a symlink counts as itself, never its target.
            files += 1;
            la += md.len();
            ls += md.blocks() * 512;
        }
    }
    prog.items.fetch_add(files + subdirs.len() as u64, Ordering::Relaxed);
    prog.bytes.fetch_add(ls, Ordering::Relaxed);

    let mut kids: Vec<Node> = if subdirs.len() > 1 {
        subdirs.into_par_iter().filter_map(|(p, n)| walk(&p, n, cloud, prog)).collect()
    } else {
        subdirs.into_iter().filter_map(|(p, n)| walk(&p, n, cloud, prog)).collect()
    };
    if prog.cancel.load(Ordering::Relaxed) {
        return None;
    }
    kids.sort_by(|a, b| b.size.cmp(&a.size));

    let mut node = Node {
        name,
        size: ls,
        apparent: la,
        files,
        loose_size: ls,
        loose_apparent: la,
        items: files,
        cloud,
        denied: 0,
        kids,
    };
    for k in &node.kids {
        node.size += k.size;
        node.apparent += k.apparent;
        node.items += k.items + 1;
        node.denied += k.denied;
    }
    Some(node)
}

impl Node {
    /// Follow `rel` (components relative to this node) down the tree.
    pub fn find(&self, rel: &[&str]) -> Option<&Node> {
        let mut n = self;
        for c in rel {
            n = n.kids.iter().find(|k| k.name == *c)?;
        }
        Some(n)
    }
}

/// What the UI draws: the subtree under one folder, with folders too small to see folded into
/// `more` / `more_size` so a 300k-node scan becomes a few thousand blocks.
#[derive(Serialize, Clone, Debug)]
pub struct View {
    pub name: String,
    pub path: String,
    pub size: u64,
    pub apparent: u64,
    pub files: u64,
    pub loose_size: u64,
    pub loose_apparent: u64,
    pub items: u64,
    pub cloud: bool,
    pub denied: u64,
    /// Nesting depth from the scan root (root = 0); drives the block colour.
    pub depth: u32,
    pub kids: Vec<View>,
    /// Child folders not listed because they were below the size cut.
    pub more: u32,
    pub more_size: u64,
    pub more_apparent: u64,
}

pub fn view(node: &Node, path: &str, depth: u32, min_size: u64, max_depth: u32) -> View {
    let mut kids = Vec::new();
    let (mut more, mut more_size, mut more_apparent) = (0u32, 0u64, 0u64);
    for k in &node.kids {
        if k.size >= min_size && depth < max_depth {
            let p = if path == "/" { format!("/{}", k.name) } else { format!("{path}/{}", k.name) };
            kids.push(view(k, &p, depth + 1, min_size, max_depth));
        } else {
            more += 1;
            more_size += k.size;
            more_apparent += k.apparent;
        }
    }
    View {
        name: node.name.clone(),
        path: path.to_string(),
        size: node.size,
        apparent: node.apparent,
        files: node.files,
        loose_size: node.loose_size,
        loose_apparent: node.loose_apparent,
        items: node.items,
        cloud: node.cloud,
        denied: node.denied,
        depth,
        kids,
        more,
        more_size,
        more_apparent,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn walk_sums_sizes_and_counts() {
        let dir = std::env::temp_dir().join(format!("duv-scan-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("a/b")).unwrap();
        fs::create_dir_all(dir.join("c")).unwrap();
        fs::write(dir.join("top.txt"), vec![b'x'; 5000]).unwrap();
        fs::write(dir.join("a/b/deep.txt"), vec![b'x'; 9000]).unwrap();
        let prog = Progress::default();
        let n = walk(&dir, "root".into(), false, &prog).unwrap();
        assert_eq!(n.files, 1);
        assert_eq!(n.apparent, 14000);
        assert!(n.size >= 14000, "allocated size is at least the logical size on APFS");
        assert_eq!(n.kids.len(), 2);
        assert_eq!(n.kids[0].name, "a", "children are sorted by size, largest first");
        assert_eq!(n.items, 5, "top.txt, a, a/b, a/b/deep.txt, c");
        assert_eq!(n.find(&["a", "b"]).unwrap().files, 1);
        let v = view(&n, "/x", 0, 1, 10);
        assert_eq!(v.kids[0].kids[0].path, "/x/a/b");
        let v = view(&n, "/x", 0, u64::MAX, 10);
        assert_eq!((v.kids.len(), v.more), (0, 2));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn cancel_stops_the_walk() {
        let prog = Progress::default();
        prog.cancel.store(true, Ordering::Relaxed);
        assert!(walk(Path::new("/tmp"), "tmp".into(), false, &prog).is_none());
    }

    #[test]
    fn cloud_roots() {
        assert!(is_cloud_root(Path::new("/Users/x/Library/CloudStorage/OneDrive-Personal")));
        assert!(is_cloud_root(Path::new("/Users/x/Library/Mobile Documents/com~apple~CloudDocs")));
        assert!(!is_cloud_root(Path::new("/Users/x/Library/CloudStorage/OneDrive-Personal/Docs")));
        assert!(skipped(Path::new("/System/Volumes")));
        assert!(!skipped(Path::new("/System/Library")));
    }
}
