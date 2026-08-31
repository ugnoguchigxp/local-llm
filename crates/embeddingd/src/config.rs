use std::{net::IpAddr, path::PathBuf, time::Duration};

use clap::Parser;

#[derive(Debug, Clone, Parser)]
#[command(version, about = "Always-ready multilingual-e5 embedding daemon")]
pub struct Config {
    #[arg(long, env = "EMBEDDINGD_HOST", default_value = "127.0.0.1")]
    pub host: IpAddr,
    #[arg(long, env = "EMBEDDINGD_PORT", default_value_t = 44_512)]
    pub port: u16,
    #[arg(
        long,
        env = "LOCAL_LLM_EMBEDDING_MODEL_DIR",
        default_value = "embedding/models/multilingual-e5-small-onnx-qint8"
    )]
    pub model_dir: PathBuf,
    #[arg(long, env = "LOCAL_LLM_EMBEDDING_MANIFEST")]
    pub manifest: Option<PathBuf>,
    #[arg(long, env = "EMBEDDINGD_REQUEST_TIMEOUT_SECONDS", default_value_t = 30)]
    request_timeout_seconds: u64,
    #[arg(long, env = "EMBEDDINGD_MAX_BATCH_SIZE", default_value_t = 64)]
    pub max_batch_size: usize,
    #[arg(long, env = "LOCAL_LLM_API_KEY")]
    pub api_key: Option<String>,
    #[arg(long, env = "EMBEDDINGD_INTRA_THREADS", default_value_t = 4)]
    pub intra_threads: usize,
}

impl Config {
    pub fn request_timeout(&self) -> Duration {
        Duration::from_secs(self.request_timeout_seconds.max(1))
    }
}
