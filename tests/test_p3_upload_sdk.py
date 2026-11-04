"""Exercise the real SDK serialization boundary without network or credentials."""
import json

import pytest
from slack_sdk.web.async_client import AsyncWebClient

from slack_codex_bridge.model import BridgeError
from slack_codex_bridge.slack import Web


@pytest.mark.parametrize("method,arguments", [
    ("files.getUploadURLExternal", {"filename": "P3-artifact.txt", "length": 66}),
    ("files.completeUploadExternal", {"files": [{"id": "F123ABC", "title": "P3-artifact.txt"}],
                                      "channel_id": "D123ABC", "thread_ts": "100.000001"}),
])
async def test_file_methods_use_official_sdk_parameter_serialization(cfg, method, arguments):
    client = AsyncWebClient(token="synthetic", retry_handlers=[])
    requests = []

    async def request(*, http_verb, api_url, req_args):
        requests.append((http_verb, api_url, req_args))
        assert http_verb == "POST" and api_url.endswith("/" + method)
        assert not req_args.get("json")
        params = req_args["params"]
        if method == "files.getUploadURLExternal":
            assert params["filename"] == arguments["filename"] and params["length"] == 66
        else:
            assert json.loads(params["files"]) == arguments["files"]
            assert params["channel_id"] == "D123ABC" and params["thread_ts"] == "100.000001"
        return {"status_code": 200, "headers": {}, "data": {"ok": True}}

    client._request = request
    assert await Web(cfg, "synthetic", client).call(method, write=True, **arguments) == {"ok": True}
    assert len(requests) == 1


@pytest.mark.parametrize("status,body,code", [
    (200, {"ok": False, "error": "invalid_arguments"}, "slack_write_invalid_arguments"),
    (429, {"ok": False, "error": "ratelimited"}, "slack_rate_limited"),
])
async def test_file_sdk_errors_keep_one_attempt_and_cooldown(cfg, status, body, code):
    client = AsyncWebClient(token="synthetic", retry_handlers=[])
    calls = []

    async def request(**kwargs):
        calls.append(kwargs)
        return {"status_code": status, "headers": {"Retry-After": "27"}, "data": body}

    client._request = request
    web = Web(cfg, "synthetic", client)
    with pytest.raises(BridgeError, match=code) as caught:
        await web.call("files.getUploadURLExternal", write=True, filename="fixture.txt", length=66)
    assert not caught.value.unknown and len(calls) == 1
    if status == 429:
        with pytest.raises(BridgeError, match="slack_rate_limited"):
            await web.call("files.getUploadURLExternal", write=True, filename="fixture.txt", length=66)
        assert len(calls) == 1
