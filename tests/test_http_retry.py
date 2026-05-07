"""Tests for HTTP retry behavior in API clients.

Phase 0.5: Verifies that transient errors (502, 503, 429, Timeout)
are retried with backoff, and exhausted retries return normally.
"""

import pytest
from unittest.mock import patch, MagicMock


def make_notion_client():
    """Create a NotionClient with mocked config."""
    from taskautomation.notion_client import NotionClient
    client = NotionClient.__new__(NotionClient)
    client.api_token = "test-token"
    client.database_id = "test-db"
    client.headers = {
        "Authorization": "Bearer test-token",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    return client


def make_mock_response(status_code, json_data=None, headers=None):
    """Create a mock response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    resp.text = f"status {status_code}"
    return resp


class TestNotionRetry:
    """Notion _request should retry on transient errors."""

    @patch("time.sleep")  # Skip actual delays
    @patch("taskautomation.notion_client.requests")
    def test_502_then_200(self, mock_requests, mock_sleep):
        """502 followed by 200 should succeed."""
        client = make_notion_client()
        r502 = make_mock_response(502)
        r200 = make_mock_response(200, {"results": []})
        mock_requests.get.side_effect = [r502, r200]

        result = client._request("get", "https://api.notion.com/v1/test")

        assert result.status_code == 200
        assert mock_requests.get.call_count == 2

    @patch("time.sleep")
    @patch("taskautomation.notion_client.requests")
    def test_503_then_200(self, mock_requests, mock_sleep):
        """503 followed by 200 should succeed."""
        client = make_notion_client()
        r503 = make_mock_response(503)
        r200 = make_mock_response(200)
        mock_requests.get.side_effect = [r503, r200]

        result = client._request("get", "https://api.notion.com/v1/test")

        assert result.status_code == 200

    @patch("time.sleep")
    @patch("taskautomation.notion_client.requests")
    def test_429_respects_retry_after(self, mock_requests, mock_sleep):
        """429 should respect Retry-After header."""
        client = make_notion_client()
        r429 = make_mock_response(429, headers={"Retry-After": "5"})
        r200 = make_mock_response(200)
        mock_requests.post.side_effect = [r429, r200]

        result = client._request("post", "https://api.notion.com/v1/test", json={})

        assert result.status_code == 200
        # Verify Retry-After was respected
        mock_sleep.assert_called()
        sleep_arg = mock_sleep.call_args[0][0]
        assert sleep_arg == 5.0

    @patch("time.sleep")
    @patch("taskautomation.notion_client.requests")
    def test_timeout_then_200(self, mock_requests, mock_sleep):
        """Timeout followed by 200 should succeed."""
        import requests
        client = make_notion_client()
        mock_requests.exceptions = requests.exceptions
        mock_requests.get.side_effect = [
            requests.exceptions.Timeout("read timed out"),
            make_mock_response(200),
        ]

        result = client._request("get", "https://api.notion.com/v1/test")

        assert result.status_code == 200

    @patch("time.sleep")
    @patch("taskautomation.notion_client.requests")
    def test_connection_error_then_200(self, mock_requests, mock_sleep):
        """ConnectionError followed by 200 should succeed."""
        import requests
        client = make_notion_client()
        mock_requests.exceptions = requests.exceptions
        mock_requests.get.side_effect = [
            requests.exceptions.ConnectionError("connection reset"),
            make_mock_response(200),
        ]

        result = client._request("get", "https://api.notion.com/v1/test")

        assert result.status_code == 200

    @patch("time.sleep")
    @patch("taskautomation.notion_client.requests")
    def test_exhausted_retries_returns_last_response(self, mock_requests, mock_sleep):
        """After max retries with 502, should return last 502 response (not crash)."""
        client = make_notion_client()
        r502 = make_mock_response(502)
        mock_requests.get.side_effect = [r502] * 4  # _MAX_RETRIES = 4

        result = client._request("get", "https://api.notion.com/v1/test")

        assert result.status_code == 502
        assert mock_requests.get.call_count == 4

    @patch("time.sleep")
    @patch("taskautomation.notion_client.requests")
    def test_exhausted_retries_raises_on_timeout(self, mock_requests, mock_sleep):
        """After max retries with Timeout, should raise the exception."""
        import requests
        client = make_notion_client()
        mock_requests.exceptions = requests.exceptions
        mock_requests.get.side_effect = [
            requests.exceptions.Timeout("read timed out")
        ] * 4

        with pytest.raises(requests.exceptions.Timeout):
            client._request("get", "https://api.notion.com/v1/test")

    @patch("time.sleep")
    @patch("taskautomation.notion_client.requests")
    def test_404_not_retried(self, mock_requests, mock_sleep):
        """404 should NOT be retried — only transient errors."""
        client = make_notion_client()
        r404 = make_mock_response(404)
        mock_requests.get.side_effect = [r404]

        result = client._request("get", "https://api.notion.com/v1/test")

        assert result.status_code == 404
        assert mock_requests.get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("taskautomation.notion_client.requests")
    def test_400_not_retried(self, mock_requests, mock_sleep):
        """400 should NOT be retried."""
        client = make_notion_client()
        r400 = make_mock_response(400)
        mock_requests.get.side_effect = [r400]

        result = client._request("get", "https://api.notion.com/v1/test")

        assert result.status_code == 400
        assert mock_requests.get.call_count == 1
