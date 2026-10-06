"""Slack WebClient proxy that records useful, privacy-aware API failures."""
from functools import wraps
import json
import logging

from slack_sdk.errors import SlackApiError


log = logging.getLogger(__name__)
_SECRET_KEYS = {"token", "access_token", "authorization", "password", "secret", "cookie",
                "response_url", "trigger_id"}
_PRIVATE_CALL_KEYS = {"text", "blocks", "view", "file", "files", "attachments", "metadata",
                      "private_metadata", "initial_comment"}
_RESPONSE_HEADERS = {"content_type", "date", "retry_after", "x_slack_req_id",
                     "x_oauth_scopes", "x_accepted_oauth_scopes"}


def _key_name(value):
    return str(value).lower().replace("-", "_")


def _sanitize(value, *, response=False, key=None):
    normalized = _key_name(key) if key is not None else ""
    if normalized in _SECRET_KEYS:
        return "[redacted]"
    if not response and normalized in _PRIVATE_CALL_KEYS:
        if isinstance(value, str):
            return f"[redacted text; {len(value)} characters]"
        if isinstance(value, (list, tuple, dict)):
            return f"[redacted {type(value).__name__}; {len(value)} entries]"
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, response=response, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, response=response) for item in value]
    if isinstance(value, bytes):
        return f"[binary response; {len(value)} bytes]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class SlackCallDebugClient:
    """Forward WebClient attributes and log the method/request/response on API errors."""

    def __init__(self, client):
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name):
        attribute = getattr(self._client, name)
        if not callable(attribute) or name.startswith("_"):
            return attribute

        @wraps(attribute)
        def call(*args, **kwargs):
            try:
                return attribute(*args, **kwargs)
            except SlackApiError as exc:
                response = exc.response
                data = getattr(response, "data", response)
                headers = getattr(response, "headers", {}) or {}
                diagnostic = {
                    "http_verb": getattr(response, "http_verb", None),
                    "api_url": getattr(response, "api_url", None),
                    "status_code": getattr(response, "status_code", None),
                    "headers": {key: value for key, value in headers.items()
                                if _key_name(key) in _RESPONSE_HEADERS},
                    "data": _sanitize(data, response=True),
                }
                request = {"args": _sanitize(args), "kwargs": _sanitize(kwargs)}
                log.error("Slack API call failed method=%s request=%s response=%s",
                    name, json.dumps(request, ensure_ascii=False, default=str),
                    json.dumps(diagnostic, ensure_ascii=False, default=str))
                raise

        return call

    def __setattr__(self, name, value):
        if name == "_client":
            object.__setattr__(self, name, value)
        else:
            setattr(self._client, name, value)
