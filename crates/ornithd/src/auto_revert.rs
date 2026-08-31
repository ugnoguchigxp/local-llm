use std::{
    fs::{self, File},
    io::{Read, Write},
    net::{TcpStream, ToSocketAddrs},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use anyhow::{Context, Result};
use serde_json::Value;

const METRICS_TIMEOUT: Duration = Duration::from_secs(2);
const MAX_METRICS_BYTES: u64 = 1024 * 1024;
const DESIRED_STATE_UPDATE_ATTEMPTS: usize = 3;

struct RemoveOnDrop(PathBuf);

impl Drop for RemoveOnDrop {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

#[derive(Debug, Default, Clone, Copy)]
struct Metrics {
    http_in_flight: f64,
    waiting: f64,
    completed: f64,
}

pub(crate) struct LongTaskMonitor {
    host: String,
    port: u16,
    idle_grace: Duration,
    task_observed: bool,
    completed: f64,
    idle_since: Option<Instant>,
}

impl LongTaskMonitor {
    pub(crate) fn new(host: &str, port: u16, idle_grace_seconds: u64) -> Self {
        Self {
            host: host.to_owned(),
            port,
            idle_grace: Duration::from_secs(idle_grace_seconds),
            task_observed: false,
            completed: 0.0,
            idle_since: None,
        }
    }

    pub(crate) fn should_revert(&mut self) -> bool {
        let Some(body) = fetch_metrics(&self.host, self.port) else {
            return false;
        };
        self.observe(parse_metrics(&body), Instant::now())
    }

    fn observe(&mut self, metrics: Metrics, now: Instant) -> bool {
        let active = metrics.http_in_flight + metrics.waiting > 0.0;
        let completion_advanced = metrics.completed > self.completed;
        self.completed = metrics.completed;
        if completion_advanced {
            self.task_observed = true;
        }

        if active {
            self.idle_since = None;
            return false;
        }
        if self.task_observed {
            let idle_since = self.idle_since.get_or_insert(now);
            return now.saturating_duration_since(*idle_since) >= self.idle_grace;
        }
        false
    }
}

fn fetch_metrics(host: &str, port: u16) -> Option<String> {
    let addresses = (host, port).to_socket_addrs().ok()?;
    let mut stream = addresses
        .filter_map(|address| TcpStream::connect_timeout(&address, METRICS_TIMEOUT).ok())
        .next()?;
    stream.set_read_timeout(Some(METRICS_TIMEOUT)).ok()?;
    stream.set_write_timeout(Some(METRICS_TIMEOUT)).ok()?;
    write!(
        stream,
        "GET /metrics HTTP/1.0\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
    )
    .ok()?;

    let mut response = String::new();
    stream
        .take(MAX_METRICS_BYTES)
        .read_to_string(&mut response)
        .ok()?;
    let (headers, body) = response.split_once("\r\n\r\n")?;
    if headers.lines().next()?.split_ascii_whitespace().nth(1) != Some("200") {
        return None;
    }
    Some(body.to_owned())
}

pub(crate) fn default_desired_state_path() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    Some(
        PathBuf::from(home)
            .join("Library/Application Support/contextStill/run/local-model-desired.json"),
    )
}

pub(crate) fn set_desired_profile_standard(path: &Path) -> Result<()> {
    if !path.exists() {
        return Ok(());
    }
    let parent = path.parent().context("desired state path has no parent")?;
    let temporary = parent.join(format!(".local-model-desired.{}.tmp", std::process::id()));
    for _ in 0..DESIRED_STATE_UPDATE_ATTEMPTS {
        let _temporary_cleanup = RemoveOnDrop(temporary.clone());
        let original = fs::read(path)
            .with_context(|| format!("failed to read desired state {}", path.display()))?;
        let permissions = fs::metadata(path)
            .with_context(|| format!("failed to stat desired state {}", path.display()))?
            .permissions();
        let mut document: Value = serde_json::from_slice(&original)
            .with_context(|| format!("failed to parse desired state {}", path.display()))?;
        let object = document
            .as_object_mut()
            .context("local-model desired state must be a JSON object")?;
        object.insert("profile".to_owned(), Value::String("standard".to_owned()));
        object.remove("longProfileLease");

        let encoded =
            serde_json::to_vec_pretty(&document).context("failed to encode desired state")?;
        fs::write(&temporary, encoded)
            .with_context(|| format!("failed to write desired state {}", temporary.display()))?;
        fs::set_permissions(&temporary, permissions).with_context(|| {
            format!(
                "failed to preserve desired state permissions {}",
                temporary.display()
            )
        })?;
        File::open(&temporary)
            .and_then(|file| file.sync_all())
            .with_context(|| format!("failed to sync desired state {}", temporary.display()))?;

        if fs::read(path)
            .with_context(|| format!("failed to re-read desired state {}", path.display()))?
            != original
        {
            continue;
        }
        fs::rename(&temporary, path)
            .with_context(|| format!("failed to replace desired state {}", path.display()))?;
        File::open(parent)
            .and_then(|directory| directory.sync_all())
            .with_context(|| {
                format!(
                    "failed to sync desired state directory {}",
                    parent.display()
                )
            })?;
        return Ok(());
    }
    anyhow::bail!(
        "desired state changed concurrently {} times",
        DESIRED_STATE_UPDATE_ATTEMPTS
    )
}

fn parse_metrics(body: &str) -> Metrics {
    Metrics {
        http_in_flight: metric_sum(body, "http_requests_in_flight"),
        waiting: metric_sum(body, "mistralrs_sequences_waiting"),
        completed: metric_sum(body, "mistralrs_sequences_completed_total"),
    }
}

fn metric_sum(body: &str, metric: &str) -> f64 {
    body.lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.starts_with('#') || !line.starts_with(metric) {
                return None;
            }
            let suffix = &line[metric.len()..];
            if !suffix.starts_with([' ', '{']) {
                return None;
            }
            line.split_ascii_whitespace().nth(1)?.parse::<f64>().ok()
        })
        .sum()
}

#[cfg(test)]
mod tests {
    use std::{os::unix::fs::PermissionsExt, time::SystemTime};

    use super::*;

    #[test]
    fn parses_labelled_and_unlabelled_metrics() {
        let metrics = parse_metrics(
            "# TYPE http_requests_in_flight gauge\n\
             http_requests_in_flight{path=\"/v1/chat/completions\"} 0\n\
             http_requests_in_flight{path=\"/v1/responses\"} 2\n\
             mistralrs_sequences_waiting{model=\"a\"} 1\n\
             mistralrs_sequences_completed_total{model=\"a\"} 2\n\
             mistralrs_sequences_completed_total{model=\"b\"} 3\n",
        );
        assert_eq!(metrics.http_in_flight, 2.0);
        assert_eq!(metrics.waiting, 1.0);
        assert_eq!(metrics.completed, 5.0);
    }

    #[test]
    fn does_not_revert_before_a_task_is_observed() {
        let mut monitor = LongTaskMonitor::new("127.0.0.1", 1, 5);
        let now = Instant::now();
        assert!(!monitor.observe(Metrics::default(), now));
        assert!(!monitor.observe(Metrics::default(), now + Duration::from_secs(30)));
    }

    #[test]
    fn reverts_after_completion_and_idle_grace() {
        let mut monitor = LongTaskMonitor::new("127.0.0.1", 1, 5);
        let now = Instant::now();
        assert!(!monitor.observe(
            Metrics {
                http_in_flight: 1.0,
                ..Metrics::default()
            },
            now
        ));
        assert!(!monitor.observe(
            Metrics {
                completed: 1.0,
                ..Metrics::default()
            },
            now + Duration::from_secs(1)
        ));
        assert!(monitor.observe(
            Metrics {
                completed: 1.0,
                ..Metrics::default()
            },
            now + Duration::from_secs(6)
        ));
    }

    #[test]
    fn request_without_a_completed_sequence_does_not_arm_revert() {
        let mut monitor = LongTaskMonitor::new("127.0.0.1", 1, 5);
        let now = Instant::now();
        assert!(!monitor.observe(
            Metrics {
                http_in_flight: 1.0,
                ..Metrics::default()
            },
            now
        ));
        assert!(!monitor.observe(Metrics::default(), now + Duration::from_secs(30)));
    }

    #[test]
    fn a_new_request_resets_the_idle_grace() {
        let mut monitor = LongTaskMonitor::new("127.0.0.1", 1, 5);
        let now = Instant::now();
        assert!(!monitor.observe(
            Metrics {
                completed: 1.0,
                ..Metrics::default()
            },
            now
        ));
        assert!(!monitor.observe(
            Metrics {
                http_in_flight: 1.0,
                completed: 1.0,
                ..Metrics::default()
            },
            now + Duration::from_secs(4)
        ));
        assert!(!monitor.observe(
            Metrics {
                completed: 2.0,
                ..Metrics::default()
            },
            now + Duration::from_secs(5)
        ));
        assert!(monitor.observe(
            Metrics {
                completed: 2.0,
                ..Metrics::default()
            },
            now + Duration::from_secs(10)
        ));
    }

    #[test]
    fn updates_existing_desired_state_without_dropping_other_fields() {
        let unique = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!(
            "ornithd-auto-revert-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join("desired.json");
        fs::write(
            &path,
            r#"{"schemaVersion":1,"profile":"long","ornith":{"desiredState":"running"},"longProfileLease":{"id":"a"}}"#,
        )
        .unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();

        set_desired_profile_standard(&path).unwrap();

        let document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(document["profile"], "standard");
        assert_eq!(document["ornith"]["desiredState"], "running");
        assert!(document.get("longProfileLease").is_none());
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        fs::remove_dir_all(directory).unwrap();
    }
}
