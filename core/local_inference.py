"""Local model policy, independent of external agent runtimes such as Muse."""

import os


def local_inference_enabled() -> bool:
    return os.getenv("LOCAL_INFERENCE_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
