import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from ledger.slack_client import SlackCallDebugClient


def test_slack_api_failure_logs_attempt_and_extended_response_without_message_body(caplog):
    response = SimpleNamespace(http_verb="POST", api_url="https://slack.test/api/chat.postMessage",
        status_code=400, headers={"x-slack-req-id": "request-123", "authorization": "do-not-log"},
        data={"ok": False, "error": "invalid_blocks", "needed": "chat:write",
              "response_metadata": {"messages": ["block validation failed"]}})
    client = MagicMock()
    client.chat_postMessage.side_effect = SlackApiError("Slack request failed", response)
    slack = SlackCallDebugClient(client)

    with caplog.at_level(logging.ERROR, logger="ledger.slack_client"):
        with pytest.raises(SlackApiError):
            slack.chat_postMessage(channel="D123", text="private conversation text")

    entry = caplog.text
    assert "method=chat_postMessage" in entry
    assert '"channel": "D123"' in entry
    assert "private conversation text" not in entry
    assert "redacted text; 25 characters" in entry
    assert "invalid_blocks" in entry and "block validation failed" in entry
    assert "request-123" in entry
    assert "do-not-log" not in entry


def test_slack_debug_client_forwards_attributes_and_mutable_settings():
    client = MagicMock()
    client.timeout = 10
    client.api_test.return_value = {"ok": True}
    slack = SlackCallDebugClient(client)

    slack.timeout = 1
    assert slack.timeout == 1
    assert slack.api_test() == {"ok": True}
    client.api_test.assert_called_once_with()
