use std::{
    process::{Child, Command, ExitStatus, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

use anyhow::{Context, Result};

use crate::auto_revert::LongTaskMonitor;

const POLL_INTERVAL: Duration = Duration::from_millis(500);
const TERMINATION_TIMEOUT: Duration = Duration::from_secs(20);

pub(crate) enum ProcessOutcome {
    Shutdown,
    Exited(ExitStatus),
    AutoRevert,
}

struct ManagedChild(Child);

impl Drop for ManagedChild {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

pub(crate) fn supervise(
    mut command: Command,
    mut monitor: Option<LongTaskMonitor>,
    stop_requested: Arc<AtomicBool>,
) -> Result<ProcessOutcome> {
    let mut child = ManagedChild(
        command
            .stdin(Stdio::null())
            .spawn()
            .context("failed to spawn mistralrs")?,
    );

    loop {
        if stop_requested.load(Ordering::SeqCst) {
            terminate(&mut child.0)?;
            return Ok(ProcessOutcome::Shutdown);
        }
        if let Some(status) = child
            .0
            .try_wait()
            .context("failed to query mistralrs status")?
        {
            return Ok(ProcessOutcome::Exited(status));
        }
        if monitor.as_mut().is_some_and(LongTaskMonitor::should_revert) {
            terminate(&mut child.0)?;
            return Ok(ProcessOutcome::AutoRevert);
        }
        thread::sleep(POLL_INTERVAL);
    }
}

fn terminate(child: &mut Child) -> Result<()> {
    if child
        .try_wait()
        .context("failed to query mistralrs before termination")?
        .is_some()
    {
        return Ok(());
    }

    let pid = child.id().to_string();
    let signal_delivered = Command::new("/bin/kill")
        .args(["-TERM", &pid])
        .status()
        .is_ok_and(|status| status.success());
    if !signal_delivered {
        return force_stop(child);
    }
    let deadline = Instant::now() + TERMINATION_TIMEOUT;
    while Instant::now() < deadline {
        if child
            .try_wait()
            .context("failed while waiting for mistralrs termination")?
            .is_some()
        {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }

    force_stop(child)
}

fn force_stop(child: &mut Child) -> Result<()> {
    if let Err(error) = child.kill()
        && child
            .try_wait()
            .context("failed to query mistralrs after kill failure")?
            .is_none()
    {
        return Err(error).context("failed to force-stop mistralrs");
    }
    child.wait().context("failed to reap mistralrs")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminate_stops_and_reaps_child() {
        let mut child = Command::new("/bin/sleep").arg("30").spawn().unwrap();
        terminate(&mut child).unwrap();
        assert!(child.try_wait().unwrap().is_some());
    }

    #[test]
    fn managed_child_drop_prevents_an_orphan() {
        let pid = {
            let child = Command::new("/bin/sleep").arg("30").spawn().unwrap();
            let pid = child.id();
            let _managed = ManagedChild(child);
            pid
        };
        let status = Command::new("/bin/kill")
            .args(["-0", &pid.to_string()])
            .stderr(Stdio::null())
            .status()
            .unwrap();
        assert!(!status.success());
    }
}
