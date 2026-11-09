# Sift — a Perplexity-style answer engine

Ask a question. Sift searches the web, reads the pages, and streams back an answer where every claim is cited to its source.

Built with FastAPI, the Tavily search API and Google Gemini, with a plain HTML/JS frontend. Deployed on Vercel.

## How it works

```
question ─► Tavily search (10 results, full page text)
         ─► rank by relevance, drop weak matches, keep the top 6
         ─► Gemini writes the answer, citing sources as [1], [2] …   (streamed)
         ─► a lighter Gemini model suggests 3 follow-up questions
```

`POST /ask` returns a stream of newline-delimited JSON events, so the page can show the sources first and then the answer as it is written:

| Event | Data |
|---|---|
| `sources` | Title, URL, favicon, snippet and relevance score for each source |
| `chunk` | The next piece of answer text (Markdown) |
| `related` | Up to 3 follow-up questions |
| `error` | A readable message (search failed, model overloaded, …) |
| `done` | End of the answer |

Follow-up questions in a thread send the previous answers along, so "what about X?" makes sense. Threads are kept in the browser's local storage; nothing about the user is stored on the server.

## Features

- **Cited answers:** the `[n]` markers in the answer become clickable source links
- **Streaming:** sources appear within a few seconds, then the answer types out
- **Follow-ups and related questions** in a single thread
- **Resilient:** falls back to a second Gemini model when the first is overloaded, and shows clear errors instead of crashing

## Run it locally

```bash
python -m venv .venv
.venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt pytest httpx
```

Create `.env`:

```
GEMINI_API_KEY=your-gemini-key      # https://aistudio.google.com/apikey
TAVILY_API_KEY=your-tavily-key      # https://app.tavily.com
```

```bash
uvicorn app:app --reload
python -m pytest -q
```

## History

This started as a Flutter app talking to a FastAPI WebSocket server, which ranked sources with a local sentence-transformers model. To deploy it on Vercel's serverless platform, I replaced the WebSocket with a streamed HTTP response, used Tavily's own relevance scores instead of a 2 GB embedding model, and rebuilt the UI for the web.
