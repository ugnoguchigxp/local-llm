use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerHealth<'a> {
    pub ready: bool,
    pub service: &'a str,
    pub version: &'a str,
    pub model_loaded: bool,
    pub active_requests: usize,
    pub queue_depth: usize,
}
