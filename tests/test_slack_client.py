import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ledger.slack_client import SlackCallDebugClient
from ledger.slack_app import build_app


def test_slack_api_failure_logs_attempt_and_extended_response_without_message_body(caplog):
    response = SimpleNamespace(http_verb="POST", api_url="https://slack.test/api/chat.postMessage",
        status_code=400, headers={"x-slack-req-id": "request-123", "authorization": "do-not-log"},
        data={"ok": False, "error": "invalid_blocks", "needed": "chat:write",
              "response_metadata": {"messages": ["block validation failed"]}})
    slack = SlackCallDebugClient(token="xoxb-test")

    with patch.object(WebClient, "api_call", side_effect=SlackApiError("Slack request failed", response)):
        with caplog.at_level(logging.ERROR, logger="ledger.slack_client"):
            with pytest.raises(SlackApiError):
                slack.chat_postMessage(channel="D123", text="private conversation text")

    entry = caplog.text
    assert "method=chat.postMessage" in entry
    assert '"channel": "D123"' in entry
    assert "private conversation text" not in entry
    assert "redacted text; 25 characters" in entry
    assert "invalid_blocks" in entry and "block validation failed" in entry
    assert "request-123" in entry
    assert "do-not-log" not in entry


def test_slack_debug_client_is_a_webclient_and_preserves_mutable_settings():
    slack = SlackCallDebugClient(token="xoxb-test", timeout=10)

    slack.timeout = 1
    assert slack.timeout == 1
    assert isinstance(slack, WebClient)
    with patch.object(WebClient, "api_call", return_value={"ok": True}) as api_call:
        assert slack.api_test() == {"ok": True}
    api_call.assert_called_once_with("api.test", params={"error": None})


def test_debug_client_is_accepted_by_bolt():
    slack = SlackCallDebugClient(token="xoxb-test")
    app = build_app(None, "xoxb-test", "test-signing-secret", "T1", "UBOT", slack)

    assert app.client is slack
