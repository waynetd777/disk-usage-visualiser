use serde::Serialize;
use std::{fs, os::unix::fs::MetadataExt, path::Path, time::UNIX_EPOCH};

#[derive(Serialize)]
pub struct FileEntry {
    name: String,
    path: String,
    size: u64,
    disk_size: u64,
    modified: Option<u64>,
    kind: String,
}

// Read metadata only: opening a cloud placeholder's contents could download it.
fn list_files(path: &Path) -> Result<Vec<FileEntry>, String> {
    let mut files = Vec::new();
    for entry in
        fs::read_dir(path).map_err(|e| format!("Could not read {}: {e}", path.display()))?
    {
        let entry = entry.map_err(|e| e.to_string())?;
        let metadata = entry
            .metadata()
            .map_err(|e| format!("{}: {e}", entry.path().display()))?;
        if metadata.is_dir() {
            continue;
        }
        let file_path = entry.path();
        let kind = if metadata.file_type().is_symlink() {
            "Alias".to_string()
        } else {
            file_path
                .extension()
                .and_then(|s| s.to_str())
                .filter(|s| !s.is_empty())
                .map(|s| format!("{} document", s.to_uppercase()))
                .unwrap_or_else(|| "File".into())
        };
        files.push(FileEntry {
            name: entry.file_name().to_string_lossy().into_owned(),
            path: file_path.to_string_lossy().into_owned(),
            size: metadata.len(),
            disk_size: metadata.blocks() * 512,
            modified: metadata
                .modified()
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs()),
            kind,
        });
    }
    Ok(files)
}

#[tauri::command]
pub async fn get_files(path: String) -> Result<Vec<FileEntry>, String> {
    tauri::async_runtime::spawn_blocking(move || list_files(Path::new(&path)))
        .await
        .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lists_direct_files_and_links_without_following_them() {
        let dir = std::env::temp_dir().join(format!("du-files-{}", std::process::id()));
        fs::create_dir_all(dir.join("child")).unwrap();
        fs::write(dir.join("example.txt"), "hello").unwrap();
        fs::write(dir.join("child/nested.txt"), "nested").unwrap();
        std::os::unix::fs::symlink(dir.join("missing"), dir.join("link")).unwrap();
        let entries = list_files(&dir).unwrap();
        assert_eq!(entries.len(), 2);
        let file = entries.iter().find(|f| f.name == "example.txt").unwrap();
        assert_eq!(file.size, 5);
        assert_eq!(file.kind, "TXT document");
        assert!(file.modified.is_some());
        assert_eq!(
            entries.iter().find(|f| f.name == "link").unwrap().kind,
            "Alias"
        );
        fs::remove_dir_all(&dir).unwrap();
        assert!(list_files(&dir).is_err());
    }
}
