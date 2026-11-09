"""Sift: a Perplexity-style answer engine.

Pipeline per question: Tavily web search -> keep the relevant sources (by Tavily's
relevance score) -> Gemini streams an answer that cites them as [1], [2] ...
-> a cheap second call suggests follow-up questions.

The response is streamed as NDJSON events so the page can show sources first,
then the answer as it's written: {"type": "sources" | "chunk" | "related" | "error" | "done"}.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field
from tavily import TavilyClient

HERE = Path(__file__).parent
STATIC = HERE / "static"
load_dotenv(HERE / ".env")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
RELATED_MODEL = os.getenv("GEMINI_RELATED_MODEL", "gemini-3.5-flash-lite")
MAX_SOURCES = 6
MIN_SCORE = 0.3          # Tavily relevance below this is usually off-topic
SOURCE_CHARS = 4000      # page text per source sent to Gemini


class Turn(BaseModel):
    q: str
    a: str


class AskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    history: list[Turn] = Field(default_factory=list, max_length=6)


app = FastAPI(title="Sift")


def keys() -> tuple[str, str]:
    gemini, tavily = os.getenv("GEMINI_API_KEY", ""), os.getenv("TAVILY_API_KEY", "")
    if not gemini or not tavily:
        raise HTTPException(500, "The server is missing its GEMINI_API_KEY or TAVILY_API_KEY.")
    return gemini, tavily


def search(tavily_key: str, query: str) -> list[dict]:
    response = TavilyClient(api_key=tavily_key).search(
        query, max_results=10, include_favicon=True, include_raw_content="text"
    )
    return rank(response.get("results", []))


def rank(results: list[dict]) -> list[dict]:
    """Drop weak matches, best first, capped. If everything is weak, keep the top few anyway."""
    by_score = sorted(results, key=lambda r: r.get("score") or 0, reverse=True)
    strong = [r for r in by_score if (r.get("score") or 0) >= MIN_SCORE]
    return [
        {
            "title": r.get("title") or r.get("url", ""),
            "url": r.get("url", ""),
            "favicon": r.get("favicon") or "",
            "snippet": (r.get("content") or "")[:300],
            "text": (r.get("raw_content") or r.get("content") or "")[:SOURCE_CHARS],
            "score": round(r.get("score") or 0, 3),
        }
        for r in (strong or by_score[:3])[:MAX_SOURCES]
    ]


def build_prompt(query: str, sources: list[dict], history: list[Turn]) -> str:
    context = "\n\n".join(f"[{i}] {s['title']} ({s['url']})\n{s['text']}" for i, s in enumerate(sources, 1))
    earlier = "\n".join(f"Q: {t.q}\nA: {t.a[:1500]}" for t in history[-3:])
    return (
        "You are an answer engine. Answer the question using the numbered sources below.\n"
        "- Cite every factual claim with the source number in square brackets, like [1] or [2][4].\n"
        "- Only cite numbers that exist. If the sources don't cover something, say so briefly.\n"
        "- Lead with the direct answer in one or two sentences, then add detail. Use Markdown: short "
        "paragraphs, bullet lists or a small table where it helps. No preamble, no closing summary.\n\n"
        + (f"EARLIER IN THIS CONVERSATION:\n{earlier}\n\n" if earlier else "")
        + f"SOURCES:\n{context}\n\nQUESTION: {query}"
    )


def stream_answer(client: genai.Client, prompt: str):
    """Yield text chunks. Falls back to the second model if the first is overloaded before any text arrives."""
    for model in dict.fromkeys([MODEL, FALLBACK_MODEL]):
        try:
            for chunk in client.models.generate_content_stream(model=model, contents=prompt):
                if chunk.text:
                    yield chunk.text
            return
        except genai_errors.APIError as e:
            if e.code != 503 or model == FALLBACK_MODEL:
                raise


def related_questions(client: genai.Client, query: str, answer: str) -> list[str]:
    prompt = (
        "Suggest 3 short follow-up questions a curious reader would ask next. "
        "One per line, no numbering, each under 12 words.\n\n"
        f"QUESTION: {query}\n\nANSWER:\n{answer[:3000]}"
    )
    try:
        text = client.models.generate_content(model=RELATED_MODEL, contents=prompt).text or ""
    except Exception:
        return []  # nice-to-have: never fail the answer over it
    lines = [line.strip(" -•*0123456789.").strip() for line in text.splitlines()]
    return [line for line in lines if line.endswith("?")][:3]


def friendly(e: Exception) -> str:
    if isinstance(e, genai_errors.APIError):
        if e.code == 429:
            return "The answer model is rate-limited right now. Try again in a minute."
        if e.code == 503:
            return "The answer model is overloaded right now. Try again in a moment."
        return f"Gemini error: {e.message or e}"
    if isinstance(e, HTTPException):
        return e.detail
    return f"Something went wrong: {e}"


def event(kind: str, data) -> str:
    return json.dumps({"type": kind, "data": data}) + "\n"


@app.post("/ask")
def ask(req: AskRequest):
    gemini_key, tavily_key = keys()
    query = req.query.strip()

    def events():
        try:
            sources = search(tavily_key, query)
        except Exception as e:
            yield event("error", f"Web search failed: {e}")
            return
        yield event("sources", [{k: s[k] for k in ("title", "url", "favicon", "snippet", "score")} for s in sources])
        if not sources:
            yield event("error", "The search found nothing relevant. Try rephrasing the question.")
            return

        client = genai.Client(api_key=gemini_key)
        answer = []
        try:
            for text in stream_answer(client, build_prompt(query, sources, req.history)):
                answer.append(text)
                yield event("chunk", text)
        except Exception as e:
            yield event("error", friendly(e))
            return
        yield event("related", related_questions(client, query, "".join(answer)))
        yield event("done", None)

    # no-transform/X-Accel-Buffering: stop proxies from buffering the stream into one blob
    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
