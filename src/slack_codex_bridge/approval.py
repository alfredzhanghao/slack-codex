"""Connection-bound requests and conservative remote response shapes."""

import json
import secrets
import time
from dataclasses import dataclass, field

from .config import safe_text
from .model import BridgeError

COMMAND = "item/commandExecution/requestApproval"
FILES = "item/fileChange/requestApproval"
PERMISSIONS = "item/permissions/requestApproval"
QUESTIONS = "item/tool/requestUserInput"
FORM = "mcpServer/elicitation/request"


@dataclass
class PendingRequest:
    message: dict
    connection: object
    generation: int
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    expires: float = field(default_factory=lambda: time.monotonic() + 600)
    status: str = "pending"

    @property
    def params(self):
        return self.message["params"]

    @property
    def thread_id(self):
        return self.params.get("threadId", "")

    @property
    def turn_id(self):
        return self.params.get("turnId", "")


def presentation(request, items) -> tuple[str, dict[str, dict], list[dict]]:
    """Return complete evidence, exact choices and optional questions; fail closed."""
    method, p = request.message["method"], request.params
    if method not in {COMMAND, FILES, PERMISSIONS, QUESTIONS, FORM}:
        raise BridgeError("request_requires_original_client")
    evidence = {k: v for k, v in p.items() if k != "_meta"}
    forms = []
    if method == COMMAND:
        if not p.get("cwd") or not (p.get("command") or p.get("networkApprovalContext")):
            raise BridgeError("approval_evidence_incomplete")
        allowed = p.get("availableDecisions")
        allowed = ["accept", "decline", "cancel"] if allowed is None else allowed
        choices = {v: {"decision": v} for v in allowed if isinstance(v, str)
                   and v in {"accept", "decline", "cancel"}}
    elif method == FILES:
        item = items.get((request.thread_id, request.turn_id, p.get("itemId")))
        if not item or item.get("type") != "fileChange" or not item.get("changes"):
            raise BridgeError("approval_evidence_incomplete")
        evidence["changes"] = item["changes"]
        # grantRoot requests session-wide access; only the original client presents it.
        choices = {v: {"decision": v} for v in (["decline", "cancel"] if p.get("grantRoot")
                                              else ["accept", "decline", "cancel"])}
    elif method == PERMISSIONS:
        if not p.get("cwd") or not isinstance(p.get("permissions"), dict):
            raise BridgeError("approval_evidence_incomplete")
        choices = {"grant for this turn": {"permissions": p["permissions"], "scope": "turn"},
                   "deny": {"permissions": {}, "scope": "turn"}}
    elif method == QUESTIONS:
        forms = p.get("questions")
        if (not isinstance(forms, list) or not 1 <= len(forms) <= 10
                or any(not q.get("id") or not q.get("question") for q in forms)
                or len({q["id"] for q in forms}) != len(forms)):
            raise BridgeError("request_requires_original_client")
        choices = {}
    else:
        # Other wire formats are displayed by Slack without inventing a response schema.
        if p.get("mode") not in {"form", "openai/form", "openaiForm"}:
            raise BridgeError("request_requires_original_client")
        schema = p.get("requestedSchema") or {}
        properties = schema.get("properties") or {}
        if (schema.get("type") != "object" or not 1 <= len(properties) <= 10
                or any(v.get("type") not in {"string", "boolean", "integer", "number"}
                       for v in properties.values())):
            raise BridgeError("request_requires_original_client")
        forms = [{"id": k, "question": v.get("description") or v.get("title") or k}
                 for k, v in properties.items()]
        choices = {"decline": {"action": "decline"}, "cancel": {"action": "cancel"}}
    text = method + "\n" + json.dumps(evidence, ensure_ascii=False, indent=2)
    if not safe_text(text):
        raise BridgeError("request_exceeds_transport_limit")
    if not request.thread_id or not request.turn_id:
        raise BridgeError("approval_target_incomplete")
    return text, choices, forms


def response(request, items, choice: str, answers: dict | None = None) -> dict:
    _, choices, forms = presentation(request, items)
    if choice in choices:
        return choices[choice]
    if choice != "answer" or not forms or not isinstance(answers, dict):
        raise BridgeError("invalid_approval_response")
    if set(answers) != {q["id"] for q in forms} or not safe_text(json.dumps(answers)):
        raise BridgeError("invalid_approval_response")
    if request.message["method"] == QUESTIONS:
        if any(not isinstance(v, str) or not v.strip() for v in answers.values()):
            raise BridgeError("invalid_approval_response")
        return {"answers": {k: {"answers": [v]} for k, v in answers.items()}}
    schema = request.params["requestedSchema"]
    for key, value in answers.items():
        spec = schema["properties"][key]
        expected = {"string": str, "boolean": bool, "integer": int, "number": (int, float)}[spec["type"]]
        if not isinstance(value, expected) or (spec["type"] in {"integer", "number"} and isinstance(value, bool)):
            raise BridgeError("invalid_approval_response")
        if "enum" in spec and value not in spec["enum"]:
            raise BridgeError("invalid_approval_response")
        if isinstance(value, str) and not spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", 16000):
            raise BridgeError("invalid_approval_response")
        if type(value) in {int, float} and not spec.get("minimum", float("-inf")) <= value <= spec.get("maximum", float("inf")):
            raise BridgeError("invalid_approval_response")
        # Complex constraints are not silently ignored.
        if set(spec) - {"type", "title", "description", "default", "enum", "minLength", "maxLength", "minimum", "maximum",
                        "format", "writeOnly", "readOnly"}:
            raise BridgeError("request_requires_original_client")
    return {"action": "accept", "content": answers}
