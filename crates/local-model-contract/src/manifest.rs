use std::{
    fs::{self, File},
    io::Read,
    path::{Component, Path},
};

use anyhow::{Context, Result, bail};
use serde::Deserialize;
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactManifest {
    pub schema_version: u32,
    pub model_id: String,
    pub revision: String,
    pub runtime: String,
    pub files: Vec<ArtifactFile>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ArtifactFile {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
}

pub fn verify_artifacts(root: &Path, manifest_path: &Path) -> Result<ArtifactManifest> {
    let manifest: ArtifactManifest = serde_json::from_slice(
        &fs::read(manifest_path)
            .with_context(|| format!("failed to read manifest {}", manifest_path.display()))?,
    )
    .with_context(|| format!("invalid manifest JSON {}", manifest_path.display()))?;
    if manifest.schema_version != 1 {
        bail!(
            "unsupported artifact manifest schema {}",
            manifest.schema_version
        );
    }
    if manifest.files.is_empty() {
        bail!("artifact manifest contains no files");
    }
    for expected in &manifest.files {
        let relative = Path::new(&expected.path);
        if relative.is_absolute()
            || relative
                .components()
                .any(|component| !matches!(component, Component::Normal(_)))
        {
            bail!("unsafe artifact path in manifest: {}", expected.path);
        }
        let path = root.join(relative);
        let metadata = fs::metadata(&path)
            .with_context(|| format!("artifact is missing: {}", path.display()))?;
        if metadata.len() != expected.bytes {
            bail!(
                "artifact byte length mismatch for {}: expected {}, got {}",
                path.display(),
                expected.bytes,
                metadata.len()
            );
        }
        let actual = sha256(&path)?;
        if actual != expected.sha256.to_ascii_lowercase() {
            bail!(
                "artifact SHA-256 mismatch for {}: expected {}, got {}",
                path.display(),
                expected.sha256,
                actual
            );
        }
    }
    Ok(manifest)
}

fn sha256(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_parent_directory_paths() {
        let root = Path::new("/tmp");
        let relative = Path::new("../secret");
        assert!(
            relative
                .components()
                .any(|component| !matches!(component, Component::Normal(_)))
        );
        assert!(root.join(relative).is_absolute());
    }
}
