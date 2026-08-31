use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Instant,
};

use axum::{
    Json, Router,
    extract::State,
    http::HeaderMap,
    routing::{get, post},
};
use local_model_contract::{auth::authorize, error::ApiError, health::WorkerHealth};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{config::Config, engine::EmbeddingEngine};

#[derive(Clone)]
pub struct AppState {
    engine: Arc<Mutex<EmbeddingEngine>>,
    model_dir: String,
    api_key: Option<String>,
    request_timeout: std::time::Duration,
    max_batch_size: usize,
    active: Arc<AtomicUsize>,
    queued: Arc<AtomicUsize>,
}

#[derive(Debug, Deserialize)]
pub struct EmbedRequest {
    texts: Value,
    #[serde(rename = "type", default = "default_embed_type")]
    embed_type: String,
    #[serde(default = "default_true")]
    normalize: bool,
    #[serde(default = "default_priority")]
    priority: String,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum EmbeddingInput {
    One(String),
    Many(Vec<String>),
}

#[derive(Debug, Deserialize)]
pub struct OpenAiEmbeddingRequest {
    input: EmbeddingInput,
    #[serde(default = "default_model")]
    model: String,
    #[serde(default = "default_priority")]
    priority: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct EmbedResponse {
    embeddings: Vec<Vec<f32>>,
    dimension: usize,
    count: usize,
    #[serde(rename = "type")]
    embed_type: String,
    normalize: bool,
    queue_wait_ms: f64,
    encode_ms: f64,
}

pub fn router(config: &Config, engine: EmbeddingEngine) -> Router {
    let state = AppState {
        engine: Arc::new(Mutex::new(engine)),
        model_dir: config.model_dir.to_string_lossy().into_owned(),
        api_key: config.api_key.clone(),
        request_timeout: config.request_timeout(),
        max_batch_size: config.max_batch_size.max(1),
        active: Arc::new(AtomicUsize::new(0)),
        queued: Arc::new(AtomicUsize::new(0)),
    };
    Router::new()
        .route("/health", get(health))
        .route("/status", get(status))
        .route("/embed", post(embed))
        .route("/v1/embeddings", post(openai_embed))
        .with_state(state)
}

async fn health(State(state): State<AppState>) -> Json<Value> {
    Json(json!({
        "ready": true,
        "service": "embeddingd",
        "version": env!("CARGO_PKG_VERSION"),
        "modelLoaded": true,
        "modelDir": state.model_dir,
        "activeRequests": state.active.load(Ordering::Relaxed),
        "inFlight": state.active.load(Ordering::Relaxed),
        "queueDepth": state.queued.load(Ordering::Relaxed),
        "queueSize": state.queued.load(Ordering::Relaxed),
        "cancelledCount": 0
    }))
}

async fn status(State(state): State<AppState>) -> Json<Value> {
    let health = WorkerHealth {
        ready: true,
        service: "embeddingd",
        version: env!("CARGO_PKG_VERSION"),
        model_loaded: true,
        active_requests: state.active.load(Ordering::Relaxed),
        queue_depth: state.queued.load(Ordering::Relaxed),
    };
    Json(json!({
        "service": "embeddingd",
        "health": health,
        "auth": { "required": state.api_key.as_ref().is_some_and(|value| !value.is_empty()) },
        "api": { "embed": "/embed", "embeddings": "/v1/embeddings" }
    }))
}

async fn embed(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<EmbedRequest>,
) -> Result<Json<EmbedResponse>, ApiError> {
    authorize(&headers, state.api_key.as_deref())?;
    validate_priority(&request.priority)?;
    if !matches!(request.embed_type.as_str(), "query" | "passage") {
        return Err(ApiError::bad_request("type must be 'query' or 'passage'"));
    }
    let texts = parse_text_array(request.texts, state.max_batch_size)?;
    let prefixed = texts
        .iter()
        .map(|text| format!("{}: {text}", request.embed_type))
        .collect::<Vec<_>>();
    let outcome = run_embedding(&state, prefixed, request.normalize).await?;
    let dimension = outcome.embeddings.first().map_or(0, Vec::len);
    Ok(Json(EmbedResponse {
        count: outcome.embeddings.len(),
        embeddings: outcome.embeddings,
        dimension,
        embed_type: request.embed_type,
        normalize: request.normalize,
        queue_wait_ms: outcome.queue_wait_ms,
        encode_ms: outcome.encode_ms,
    }))
}

async fn openai_embed(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<OpenAiEmbeddingRequest>,
) -> Result<Json<Value>, ApiError> {
    authorize(&headers, state.api_key.as_deref())?;
    validate_priority(&request.priority)?;
    let raw = match request.input {
        EmbeddingInput::One(text) => vec![text],
        EmbeddingInput::Many(texts) => texts,
    };
    let texts = validate_texts(raw, state.max_batch_size)?;
    let usage_tokens = texts
        .iter()
        .map(|text| (text.len() / 4).max(1))
        .sum::<usize>();
    let prefixed = texts
        .iter()
        .map(|text| format!("query: {text}"))
        .collect::<Vec<_>>();
    let outcome = run_embedding(&state, prefixed, true).await?;
    let data = outcome
        .embeddings
        .into_iter()
        .enumerate()
        .map(|(index, embedding)| {
            json!({
                "object": "embedding",
                "index": index,
                "embedding": embedding
            })
        })
        .collect::<Vec<_>>();
    Ok(Json(json!({
        "object": "list",
        "data": data,
        "model": request.model,
        "usage": { "prompt_tokens": usage_tokens, "total_tokens": usage_tokens }
    })))
}

struct EmbedOutcome {
    embeddings: Vec<Vec<f32>>,
    queue_wait_ms: f64,
    encode_ms: f64,
}

async fn run_embedding(
    state: &AppState,
    texts: Vec<String>,
    normalize: bool,
) -> Result<EmbedOutcome, ApiError> {
    state.queued.fetch_add(1, Ordering::Relaxed);
    let queued_at = Instant::now();
    let engine = Arc::clone(&state.engine);
    let queued = Arc::clone(&state.queued);
    let active = Arc::clone(&state.active);
    let work = tokio::task::spawn_blocking(move || {
        let mut engine = engine
            .lock()
            .map_err(|_| anyhow::anyhow!("embedding engine lock poisoned"))?;
        queued.fetch_sub(1, Ordering::Relaxed);
        active.fetch_add(1, Ordering::Relaxed);
        let encode_started = Instant::now();
        let result = engine.embed(&texts, normalize);
        let encode_ms = encode_started.elapsed().as_secs_f64() * 1_000.0;
        active.fetch_sub(1, Ordering::Relaxed);
        result.map(|embeddings| EmbedOutcome {
            embeddings,
            queue_wait_ms: encode_started.duration_since(queued_at).as_secs_f64() * 1_000.0,
            encode_ms,
        })
    });
    tokio::time::timeout(state.request_timeout, work)
        .await
        .map_err(|_| ApiError::unavailable("embedding request timed out"))?
        .map_err(|error| ApiError::internal(format!("embedding worker failed: {error}")))?
        .map_err(|error| ApiError::internal(format!("embedding inference failed: {error}")))
}

fn parse_text_array(value: Value, max_batch_size: usize) -> Result<Vec<String>, ApiError> {
    let values = value
        .as_array()
        .ok_or_else(|| ApiError::bad_request("texts must be an array"))?;
    let mut texts = Vec::with_capacity(values.len());
    for (index, value) in values.iter().enumerate() {
        let text = value
            .as_str()
            .ok_or_else(|| ApiError::bad_request(format!("texts[{index}] must be a string")))?;
        texts.push(text.to_string());
    }
    validate_texts(texts, max_batch_size)
}

fn validate_texts(texts: Vec<String>, max_batch_size: usize) -> Result<Vec<String>, ApiError> {
    if texts.is_empty() {
        return Err(ApiError::bad_request("input must not be empty"));
    }
    if texts.len() > max_batch_size {
        return Err(ApiError::bad_request(format!(
            "batch size {} exceeds maximum {max_batch_size}",
            texts.len()
        )));
    }
    texts
        .into_iter()
        .enumerate()
        .map(|(index, text)| {
            let text = text.trim();
            if text.is_empty() {
                Err(ApiError::bad_request(format!(
                    "texts[{index}] must be non-empty"
                )))
            } else {
                Ok(text.to_string())
            }
        })
        .collect()
}

fn validate_priority(priority: &str) -> Result<(), ApiError> {
    if matches!(priority, "high" | "normal" | "low") {
        Ok(())
    } else {
        Err(ApiError::bad_request(
            "priority must be high, normal, or low",
        ))
    }
}

fn default_embed_type() -> String {
    "passage".to_string()
}
fn default_priority() -> String {
    "normal".to_string()
}
fn default_model() -> String {
    "multilingual-e5-small".to_string()
}
fn default_true() -> bool {
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trims_text_without_dropping_indices() {
        let texts = validate_texts(vec![" one ".into(), "二".into()], 2).unwrap();
        assert_eq!(texts, ["one", "二"]);
        assert!(
            validate_texts(vec!["ok".into(), " ".into()], 2)
                .unwrap_err()
                .to_string()
                .contains("texts[1]")
        );
    }
}
