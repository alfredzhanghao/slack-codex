import time
from types import SimpleNamespace

from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.quota import display_time, quota_text


def backend(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    d.rpc = SimpleNamespace(generation=1, connected=True)
    return d


def test_distinct_buckets_actual_windows_missing_values_and_singapore_time(cfg):
    d = backend(cfg)
    d._limits({"rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 25, "windowDurationMins": 15, "resetsAt": 0}, "secondary": None},
        "other": {"primary": {"usedPercent": None, "windowDurationMins": 60, "resetsAt": None}}
    }})
    text = quota_text(d, cfg)
    assert "15 min: 75% remaining" in text and "60 min: remaining unavailable" in text
    assert "1970-01-01 08:00:00 SGT" in text and "5h" not in text
    assert "codex" in text and "other" in text


def test_partial_events_preserve_other_buckets_and_read_replaces_snapshot(cfg):
    d = backend(cfg)
    d._limits({"rateLimitsByLimitId": {"a": {"primary": {"usedPercent": 10}}, "b": {}}})
    d._notify({"method": "account/rateLimits/updated", "params": {
        "rateLimits": {"limitId": "a", "primary": {"usedPercent": 30}}}}, 1)
    assert set(d.limits) == {"a", "b"} and d.limits["a"][0]["primary"]["usedPercent"] == 30
    d._limits({"rateLimitsByLimitId": {"b": {}}}, replace=True)
    assert set(d.limits) == {"b"}


def test_stale_disconnected_old_generation_or_account_changed_never_looks_live(cfg):
    d = backend(cfg)
    d._limits({"rateLimits": {"limitId": "a", "primary": {"usedPercent": 20}}})
    assert "current" in quota_text(d, cfg)
    bucket = d.limits["a"][0]
    d.limits["a"] = bucket, time.time() - cfg.quota_stale - 1, 1
    assert "STALE" in quota_text(d, cfg)
    d.limits["a"] = bucket, time.time(), 0
    assert "STALE" in quota_text(d, cfg)
    d.limits["a"] = bucket, time.time(), 1
    d._notify({"method": "account/updated", "params": {}}, 1)
    assert "STALE" in quota_text(d, cfg)


def test_percent_clamped_nonfinite_missing_not_inferred(cfg):
    d = backend(cfg)
    for used, expected in [(-5, "100% remaining"), (120, "0% remaining"), (float("nan"), "remaining unavailable")]:
        d._limits({"rateLimits": {"primary": {"usedPercent": used}}}, replace=True)
        assert expected in quota_text(d, cfg)
    assert display_time(float("inf")) == "unavailable"
    d.limits.clear()
    assert "unavailable" in quota_text(d, cfg)


async def test_quota_poll_is_low_frequency_forceable_and_old_events_ignored(cfg):
    d = backend(cfg)
    calls = []

    async def call(method, params):
        calls.append(method)
        return {"rateLimits": {"limitId": "a", "primary": {"usedPercent": 25}}}
    d.rpc.call = call
    await d.refresh_limits()
    await d.refresh_limits()
    assert calls == ["account/rateLimits/read"]
    d._notify({"method": "account/rateLimits/updated", "params": {
        "rateLimits": {"limitId": "a", "primary": {"usedPercent": 99}}}}, 0)
    assert d.limits["a"][0]["primary"]["usedPercent"] == 25
    await d.refresh_limits(force=True)
    assert len(calls) == 2
