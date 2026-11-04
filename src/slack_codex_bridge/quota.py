"""Display backend account buckets without guessing absent windows."""

import math
import time
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8), "Asia/Singapore")


def display_time(value):
    if type(value) not in {int, float} or not math.isfinite(value):
        return "unavailable"
    try:
        return datetime.fromtimestamp(value, SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
    except (OverflowError, OSError, ValueError):
        return "unavailable"


def quota_text(backend, cfg):
    limits = getattr(backend, "limits", {})
    if not limits:
        return "Account limits: unavailable (no windows inferred)."
    lines = ["Account limits (shared account; Asia/Singapore):"]
    for bucket, observed, generation in limits.values():
        stale = (not backend.connected or generation != backend.rpc.generation
                 or time.time() - observed > cfg.quota_stale)
        windows = []
        for key in ("primary", "secondary"):
            w = bucket.get(key)
            if not w:
                continue
            percent = w.get("usedPercent")
            remaining = (f"{max(0, min(100, 100 - percent)):g}% remaining"
                         if type(percent) in {int, float} and math.isfinite(percent) else "remaining unavailable")
            duration = w.get("windowDurationMins")
            label = f"{duration} min" if type(duration) in {int, float} and duration > 0 else "window unavailable"
            windows.append(f"{label}: {remaining}; reset {display_time(w.get('resetsAt'))}")
        lines.append(f"{bucket['name']} [{('STALE' if stale else 'current')}, observed {display_time(observed)}]: "
                     + (" | ".join(windows) or "windows not provided"))
    return "\n".join(lines)
