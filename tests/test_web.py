"""Tests for web.py functions."""

from unittest.mock import patch, MagicMock
from web import format_results, search_duckduckgo, fetch_page


class TestFormatResults:
    def test_normal_results(self):
        results = [
            {"title": "Python Docs", "url": "https://docs.python.org", "snippet": "Official docs"},
            {"title": "Tutorial", "url": "https://example.com", "snippet": "A tutorial"},
        ]
        output = format_results(results)
        assert "Python Docs" in output
        assert "https://docs.python.org" in output
        assert "Official docs" in output
        assert "Tutorial" in output

    def test_empty_results(self):
        assert format_results([]) == "No results found."

    def test_missing_fields(self):
        results = [{"title": "No URL", "url": "", "snippet": ""}]
        output = format_results(results)
        assert "No URL" in output

    def test_includes_engine_tag(self):
        output = format_results([{"title": "Test", "url": "http://x", "snippet": "s"}])
        assert "Search results" in output


class TestSearchDuckDuckGo:
    @patch("web.requests.post")
    def test_happy_path(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.text = """
        <html><body>
        <div class="result">
            <a class="result__a" href="https://example.com">Example</a>
            <a class="result__snippet">A snippet</a>
        </div>
        </body></html>
        """
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp
        results = search_duckduckgo("test query")
        assert len(results) >= 1
        assert results[0]["title"] == "Example"

    @patch("web.requests.post")
    def test_error_returns_error_result(self, mock_post):
        mock_post.side_effect = Exception("Network error")
        results = search_duckduckgo("test")
        assert len(results) == 1
        assert "Error" in results[0]["title"]


def _ok_response(text):
    """Build a non-redirect mock response (is_redirect must be False for the loop)."""
    r = MagicMock()
    r.text = text
    r.is_redirect = False
    r.is_permanent_redirect = False
    r.raise_for_status = MagicMock()
    return r


class TestFetchPage:
    @patch("web._is_safe_url", return_value=(True, ""))
    @patch("web.requests.get")
    def test_extracts_text(self, mock_get, _safe):
        mock_get.return_value = _ok_response(
            "<html><body><p>Hello World</p><script>evil()</script></body></html>")
        result = fetch_page("https://example.com")
        assert "Hello World" in result
        assert "evil" not in result

    @patch("web._is_safe_url", return_value=(True, ""))
    @patch("web.requests.get")
    def test_truncates_long_content(self, mock_get, _safe):
        mock_get.return_value = _ok_response(f"<html><body><p>{'x' * 10000}</p></body></html>")
        result = fetch_page("https://example.com", max_chars=100)
        assert "truncated" in result
        assert len(result) < 200

    @patch("web._is_safe_url", return_value=(True, ""))
    @patch("web.requests.get")
    def test_error_returns_message(self, mock_get, _safe):
        mock_get.side_effect = Exception("Connection refused")
        result = fetch_page("https://example.com")
        assert "Fetch error" in result


class TestFetchSSRF:
    """fetch_page must refuse non-public / non-http(s) targets (no network needed —
    literal IPs and bad schemes resolve locally)."""

    def test_refuses_link_local_metadata(self):
        assert "Refused" in fetch_page("http://169.254.169.254/latest/meta-data/")

    def test_refuses_loopback(self):
        assert "Refused" in fetch_page("http://127.0.0.1:8080/admin")

    def test_refuses_non_http_scheme(self):
        assert "Refused" in fetch_page("file:///etc/passwd")
        assert "Refused" in fetch_page("ftp://internal/secret")
