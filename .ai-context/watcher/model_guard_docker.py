"""
model_guard_docker.py
Small wrapper used by init.sh / restore.sh to verify models from a transient
check container that does not import the full watcher stack. Reuses the same
logic as model_guard.py so the checks stay in sync.
"""
from __future__ import annotations

import argparse
import json
import sys

from model_guard import assert_models_available


def main() -> int:
    parser = argparse.ArgumentParser(description="Check models from a bare container.")
    parser.add_argument("--resolved", required=True)
    args = parser.parse_args()

    with open(args.resolved) as f:
        resolved = json.load(f)

    # When called from init.sh / restore.sh we only need to check one project.
    # The env var PROJECT_KEY is set by the caller.
    project_key = resolved.get("name") or "unknown"
    project = resolved
    config = {"llm": project.get("llm", {})}

    if not assert_models_available(config):
        print(f"[check-models] Project '{project_key}': required model(s) not available.")
        return 1
    print(f"[check-models] Project '{project_key}': all required models available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
