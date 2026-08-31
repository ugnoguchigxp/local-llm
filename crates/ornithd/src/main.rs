mod auto_revert;
mod process;

use std::{
    net::IpAddr,
    path::PathBuf,
    process::Command,
    sync::{Arc, atomic::AtomicBool},
};

use anyhow::{Context, Result, bail};
use auto_revert::{LongTaskMonitor, default_desired_state_path, set_desired_profile_standard};
use clap::{Parser, ValueEnum};
use local_model_contract::manifest::verify_artifacts;
use process::{ProcessOutcome, supervise};

#[derive(Debug, Clone, Copy, Eq, PartialEq, ValueEnum)]
enum Profile {
    Standard,
    Long,
}

impl Profile {
    fn context_window(self) -> usize {
        match self {
            Self::Standard => 32_768,
            Self::Long => 131_072,
        }
    }

    fn cache_type(self) -> &'static str {
        match self {
            Self::Standard => "auto",
            Self::Long => "f8e4m3",
        }
    }
}

#[derive(Debug, Parser)]
#[command(version, about = "Rust/Metal Ornith server launcher")]
struct Config {
    #[arg(long, env = "ORNITHD_MISTRALRS_BIN")]
    mistralrs_bin: PathBuf,
    #[arg(long, env = "ORNITHD_MODEL_FILE")]
    model_file: PathBuf,
    #[arg(long, env = "ORNITHD_MODEL_MANIFEST")]
    manifest: Option<PathBuf>,
    #[arg(long, env = "ORNITHD_HOST", default_value = "127.0.0.1")]
    host: String,
    #[arg(long, env = "ORNITHD_PORT", default_value_t = 44_448)]
    port: u16,
    #[arg(long, env = "ORNITHD_PROFILE", value_enum, default_value = "standard")]
    profile: Profile,
    #[arg(long, env = "ORNITHD_MTP", default_value_t = false)]
    mtp: bool,
    #[arg(long, env = "ORNITHD_MTP_N_PREDICT", default_value_t = 2)]
    mtp_n_predict: usize,
    #[arg(long, env = "ORNITHD_MAX_SEQS", default_value_t = 1)]
    max_seqs: usize,
    /// Idle grace period after a completed 128k task before returning to 32k.
    #[arg(long, env = "ORNITHD_LONG_IDLE_SECONDS", default_value_t = 30)]
    long_idle_seconds: u64,
    /// ContextStill desired-state file. If omitted, the macOS default is used.
    #[arg(long, env = "ORNITHD_DESIRED_STATE_FILE")]
    desired_state_file: Option<PathBuf>,
}

fn main() -> Result<()> {
    let config = Config::parse();
    validate_config(&config)?;

    let stop_requested = Arc::new(AtomicBool::new(false));
    let signal_flag = Arc::clone(&stop_requested);
    ctrlc::set_handler(move || {
        signal_flag.store(true, std::sync::atomic::Ordering::SeqCst);
    })
    .context("failed to install shutdown signal handler")?;

    let mut profile = config.profile;
    loop {
        let monitor = (profile == Profile::Long)
            .then(|| LongTaskMonitor::new(&config.host, config.port, config.long_idle_seconds));
        match supervise(
            build_mistral_command(&config, profile),
            monitor,
            Arc::clone(&stop_requested),
        )? {
            ProcessOutcome::Shutdown => return Ok(()),
            ProcessOutcome::Exited(status) => {
                bail!("mistralrs exited unexpectedly with status {status}")
            }
            ProcessOutcome::AutoRevert => {
                let desired_state_file = config
                    .desired_state_file
                    .clone()
                    .or_else(default_desired_state_path);
                if let Some(path) = desired_state_file.as_deref()
                    && let Err(error) = set_desired_profile_standard(path)
                {
                    eprintln!(
                        "{{\"event\":\"ornithd.auto_revert.desired_state_error\",\"error\":{:?}}}",
                        error.to_string()
                    );
                }
                profile = Profile::Standard;
                eprintln!(
                    "{{\"event\":\"ornithd.auto_revert\",\"fromContextWindow\":131072,\"toContextWindow\":32768}}"
                );
            }
        }
    }
}

fn validate_config(config: &Config) -> Result<()> {
    if !(5..=3_600).contains(&config.long_idle_seconds) {
        bail!("long idle seconds must be between 5 and 3600");
    }
    if config.port == 0 {
        bail!("port 0 is not supported because the metrics port must be stable");
    }
    if !config.host.eq_ignore_ascii_case("localhost")
        && !config
            .host
            .parse::<IpAddr>()
            .is_ok_and(|address| address.is_loopback())
    {
        bail!("host must be a loopback address or localhost");
    }
    if !config.mistralrs_bin.is_file() {
        bail!(
            "mistralrs binary is missing: {}",
            config.mistralrs_bin.display()
        );
    }
    if !config.model_file.is_file() {
        bail!(
            "Ornith GGUF/UQFF artifact is missing: {}",
            config.model_file.display()
        );
    }
    if let Some(manifest) = &config.manifest {
        let model_root = config
            .model_file
            .parent()
            .context("Ornith model file has no parent directory")?;
        verify_artifacts(model_root, manifest)?;
    }
    Ok(())
}

fn build_mistral_command(config: &Config, profile: Profile) -> Command {
    let context_window = profile.context_window();
    let mut command = Command::new(&config.mistralrs_bin);
    command
        .args(["serve", "-f"])
        .arg(&config.model_file)
        .arg("--host")
        .arg(&config.host)
        .arg("--port")
        .arg(config.port.to_string())
        .args(["--no-ui", "--disable-access-log", "--paged-attn", "on"])
        .arg("--pa-context-len")
        .arg(context_window.to_string())
        .arg("--max-seq-len")
        .arg(context_window.to_string())
        .arg("--pa-cache-type")
        .arg(profile.cache_type())
        .arg("--max-seqs")
        .arg(config.max_seqs.max(1).to_string())
        .args(["--prefix-cache-n", "4"]);
    if config.mtp {
        command
            .arg("--mtp")
            .arg("--mtp-n-predict")
            .arg(config.mtp_n_predict.max(1).to_string());
    }
    eprintln!(
        "{{\"event\":\"ornithd.spawn\",\"profile\":\"{}\",\"contextWindow\":{},\"mtp\":{}}}",
        match profile {
            Profile::Standard => "standard",
            Profile::Long => "long",
        },
        context_window,
        config.mtp
    );
    command
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profiles_map_to_fixed_context_windows() {
        assert_eq!(Profile::Standard.context_window(), 32_768);
        assert_eq!(Profile::Long.context_window(), 131_072);
        assert_eq!(Profile::Standard.cache_type(), "auto");
        assert_eq!(Profile::Long.cache_type(), "f8e4m3");
    }
}
