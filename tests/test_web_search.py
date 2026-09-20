import io
import json
from types import SimpleNamespace

from bot.utils.web_search import SearxngSearch


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_searxng_search_normalizes_and_formats_results(monkeypatch):
    payload = {
        "results": [
            {
                "title": "官方招生簡章",
                "url": "https://example.edu/admission.pdf",
                "content": "報名期限與招生名額",
            }
        ]
    }

    def fake_urlopen(request, timeout):
        assert request.full_url.startswith("http://search.local/search?")
        assert "format=json" in request.full_url
        assert timeout == 4
        return FakeResponse(payload)

    monkeypatch.setattr("bot.utils.web_search.urlopen", fake_urlopen)
    search = SearxngSearch("http://search.local", timeout=4, max_results=3)

    results = search.search("招生簡章")

    assert len(results) == 1
    assert results[0].title == "官方招生簡章"
    assert "[1] 官方招生簡章" in search.format_results(results)
    assert "https://example.edu/admission.pdf" in search.format_results(results)


def test_search_failure_returns_empty_results(monkeypatch):
    def fake_urlopen(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("bot.utils.web_search.urlopen", fake_urlopen)

    assert SearxngSearch("http://search.local").search("test") == []
