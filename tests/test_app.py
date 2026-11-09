import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


def result(url, score, text="body"):
    return {"title": url, "url": url, "score": score, "content": text, "raw_content": text * 2}


def test_rank_drops_weak_sources_and_sorts_best_first():
    ranked = app.rank([result("a", 0.4), result("b", 0.1), result("c", 0.9)])
    assert [r["url"] for r in ranked] == ["c", "a"]


def test_rank_keeps_top_three_when_everything_is_weak():
    ranked = app.rank([result(str(i), 0.1 + i / 100) for i in range(5)])
    assert [r["url"] for r in ranked] == ["4", "3", "2"]


def test_rank_caps_sources_and_trims_text():
    ranked = app.rank([result(str(i), 0.9, "x" * 10_000) for i in range(10)])
    assert len(ranked) == app.MAX_SOURCES
    assert len(ranked[0]["text"]) == app.SOURCE_CHARS


def test_prompt_numbers_sources_and_includes_history():
    sources = [{"title": "T1", "url": "u1", "text": "one"}, {"title": "T2", "url": "u2", "text": "two"}]
    prompt = app.build_prompt("q?", sources, [app.Turn(q="earlier q", a="earlier a")])
    assert "[1] T1 (u1)\none" in prompt and "[2] T2 (u2)\ntwo" in prompt
    assert "earlier q" in prompt and prompt.endswith("QUESTION: q?")


def fake_client(chunks=("Hello ", "[1]"), related="Next one?\nAnother?\nnot a question"):
    models = SimpleNamespace(
        generate_content_stream=lambda model, contents: (SimpleNamespace(text=c) for c in chunks),
        generate_content=lambda model, contents: SimpleNamespace(text=related),
    )
    return SimpleNamespace(models=models)


def events(response):
    return [json.loads(line) for line in response.iter_lines() if line]


@pytest.fixture
def with_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("TAVILY_API_KEY", "t")


def test_ask_streams_sources_then_answer_then_related(with_keys):
    sources = app.rank([result("https://a.com", 0.9)])
    with patch.object(app, "search", return_value=sources), \
         patch.object(app.genai, "Client", return_value=fake_client()):
        with client.stream("POST", "/ask", json={"query": "hi"}) as r:
            evs = events(r)
    assert [e["type"] for e in evs] == ["sources", "chunk", "chunk", "related", "done"]
    assert evs[0]["data"][0]["url"] == "https://a.com" and "text" not in evs[0]["data"][0]
    assert evs[3]["data"] == ["Next one?", "Another?"]


def test_search_failure_becomes_error_event(with_keys):
    with patch.object(app, "search", side_effect=RuntimeError("tavily down")):
        with client.stream("POST", "/ask", json={"query": "hi"}) as r:
            evs = events(r)
    assert evs == [{"type": "error", "data": "Web search failed: tavily down"}]


def test_no_sources_is_an_error(with_keys):
    with patch.object(app, "search", return_value=[]):
        with client.stream("POST", "/ask", json={"query": "hi"}) as r:
            assert [e["type"] for e in events(r)] == ["sources", "error"]


def test_missing_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert client.post("/ask", json={"query": "hi"}).status_code == 500


def test_overloaded_model_falls_back():
    from google.genai import errors
    calls = []

    def stream(model, contents):
        calls.append(model)
        if model == app.MODEL:
            raise errors.APIError(503, {"error": {"message": "busy"}})
        return iter([SimpleNamespace(text="ok")])

    c = SimpleNamespace(models=SimpleNamespace(generate_content_stream=stream))
    assert list(app.stream_answer(c, "p")) == ["ok"]
    assert calls == [app.MODEL, app.FALLBACK_MODEL]


def test_empty_query_rejected():
    assert client.post("/ask", json={"query": ""}).status_code == 422


def test_home_page_served():
    assert client.get("/").status_code == 200
