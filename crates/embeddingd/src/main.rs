mod app;
mod config;
mod engine;

use anyhow::{Context, Result};
use clap::Parser;
use config::Config;
use engine::EmbeddingEngine;
use local_model_contract::manifest::verify_artifacts;
use tracing::info;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "embeddingd=info".into()),
        )
        .json()
        .with_current_span(false)
        .init();
    let config = Config::parse();
    if let Some(manifest) = &config.manifest {
        let verified = verify_artifacts(&config.model_dir, manifest)?;
        info!(
            event = "embeddingd.artifacts_verified",
            model_id = verified.model_id,
            revision = verified.revision,
            runtime = verified.runtime
        );
    }
    let engine = EmbeddingEngine::load(&config.model_dir, config.intra_threads)?;
    let address = (config.host, config.port);
    let listener = tokio::net::TcpListener::bind(address)
        .await
        .with_context(|| format!("failed to bind {}:{}", config.host, config.port))?;
    info!(
        event = "embeddingd.ready",
        host = %config.host,
        port = config.port,
        model_dir = %config.model_dir.display()
    );
    axum::serve(listener, app::router(&config, engine))
        .with_graceful_shutdown(shutdown_signal())
        .await
        .context("embedding HTTP server failed")?;
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        if let Ok(mut signal) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            signal.recv().await;
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! {
        () = ctrl_c => {},
        () = terminate => {},
    }
}
