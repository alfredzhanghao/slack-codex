"""Prove a detached child survives completion of the actual Windows scheduled task."""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from slack_codex_bridge.model import now_ms

DATA = Path(__file__).resolve().parents[1] / ".work-p1" / "acceptance"


def completion_observed(marker, run_id, request_id):
    try:
        value = json.loads(marker.read_text(encoding="utf-8-sig"))
        return value.get("run_id") == run_id and value.get("request_id") == request_id
    except (OSError, ValueError, AttributeError):
        # A concurrent marker replacement or a partial legacy writer is not proof
        # of completion, but must not kill the bounded observer either.
        return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("request_id")
    args = parser.parse_args(argv)
    if not all(re.fullmatch(r"[A-Za-z0-9-]{1,64}", v) for v in (args.run_id, args.request_id)):
        return 1
    result = {"run_id": args.run_id, "request_id": args.request_id, "status": "failed",
              "pid": os.getpid(), "code": "scheduled_task_completion_not_observed"}
    deadline = now_ms() + 45000
    while now_ms() < deadline:
        marker = DATA / "canary-task-completed.json"
        if completion_observed(marker, args.run_id, args.request_id):
            result.update(status="passed", code=None, task_completed_before_child_exit=True,
                          observed_ms=now_ms())
            break
        time.sleep(0.1)
    path = DATA / "canary-result.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result), encoding="utf-8")
    temporary.replace(path)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(1)
