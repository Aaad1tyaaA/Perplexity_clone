// Sift frontend: streams NDJSON events from /ask and renders sources, a cited answer and related questions.
// Threads live in localStorage, so nothing about a user is stored on the server.

const $ = (sel, root = document) => root.querySelector(sel);
const home = $("#home"), thread = $("#thread"), turnsEl = $("#turns");
const STORE = "sift.threads";

let current = null;      // { id, title, turns: [{ q, sources, answer, related }] }
let busy = false;

// ---------- storage ----------
function loadThreads() {
    try { return JSON.parse(localStorage.getItem(STORE)) || []; } catch { return []; }
}
function saveThread(t) {
    const all = loadThreads().filter(x => x.id !== t.id);
    all.unshift(t);
    try { localStorage.setItem(STORE, JSON.stringify(all.slice(0, 30))); } catch { /* quota: not critical */ }
}

// ---------- views ----------
function showHome() {
    current = null;
    history.replaceState(null, "", "/");
    thread.classList.add("hidden");
    home.classList.remove("hidden");
    $("#new-thread").classList.add("hidden");
    document.title = "Sift — answers with sources";
    renderRecent();
    $("#home-input").focus();
}

function showThread(t) {
    current = t;
    history.replaceState(null, "", `/?t=${encodeURIComponent(t.id)}`);
    home.classList.add("hidden");
    thread.classList.remove("hidden");
    $("#new-thread").classList.remove("hidden");
    document.title = `${t.title} · Sift`;
    turnsEl.innerHTML = "";
    t.turns.forEach(turn => renderTurn(turn, true));
}

function renderRecent() {
    const list = $("#recent-list");
    const threads = loadThreads();
    list.innerHTML = "";
    threads.slice(0, 8).forEach(t => {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = t.title;
        b.addEventListener("click", () => showThread(t));
        const n = document.createElement("span");
        n.className = "count";
        n.textContent = `${t.turns.length} ${t.turns.length === 1 ? "question" : "questions"}`;
        li.append(b, n);
        list.appendChild(li);
    });
    $("#recent").classList.toggle("hidden", !threads.length);
}

// ---------- rendering ----------
function domain(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; }
}

function renderSources(ol, sources) {
    ol.innerHTML = "";
    ol.classList.remove("loading");
    sources.forEach((s, i) => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = s.url; a.target = "_blank"; a.rel = "noopener";
        a.title = s.snippet || s.title;
        const head = document.createElement("span");
        head.className = "src-head";
        if (s.favicon) {
            const img = document.createElement("img");
            img.src = s.favicon; img.alt = ""; img.loading = "lazy";
            img.onerror = () => img.remove();
            head.appendChild(img);
        }
        const d = document.createElement("span");
        d.textContent = domain(s.url);
        const num = document.createElement("span");
        num.className = "num";
        num.textContent = i + 1;
        head.append(d, num);
        const title = document.createElement("span");
        title.className = "src-title";
        title.textContent = s.title;
        a.append(head, title);
        li.appendChild(a);
        ol.appendChild(li);
    });
}

function renderAnswer(el, markdown, sources) {
    // [3] -> superscript link to source 3. Unknown numbers are left as plain text.
    const linked = markdown.replace(/\[(\d{1,2})\]/g, (m, n) => {
        const s = sources[n - 1];
        if (!s) return m;
        const title = s.title.replace(/"/g, "&quot;");
        return `<a class="cite" href="${encodeURI(s.url)}" target="_blank" rel="noopener" title="${title}">${n}</a>`;
    });
    el.innerHTML = DOMPurify.sanitize(marked.parse(linked), { ADD_ATTR: ["target"] });
}

function renderRelated(block, ul, related) {
    ul.innerHTML = "";
    related.forEach(q => {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = q;
        b.addEventListener("click", () => ask(q));
        li.appendChild(b);
        ul.appendChild(li);
    });
    block.classList.toggle("hidden", !related.length);
}

function renderTurn(turn, finished) {
    const node = $("#turn-tpl").content.firstElementChild.cloneNode(true);
    $(".question", node).textContent = turn.q;
    const ol = $(".sources", node), answer = $(".answer", node);
    if (turn.sources) renderSources(ol, turn.sources);
    else ol.classList.add("loading");
    if (turn.answer) renderAnswer(answer, turn.answer, turn.sources || []);
    if (turn.error) showError(answer, turn.error);
    if (finished) {
        renderRelated($(".related-block", node), $(".related", node), turn.related || []);
        enableCopy(node, turn);
    }
    turnsEl.appendChild(node);
    return node;
}

function showError(el, message) {
    const p = document.createElement("p");
    p.className = "error";
    p.textContent = message;
    el.appendChild(p);
}

function enableCopy(node, turn) {
    const btn = $(".copy", node);
    if (!turn.answer) return;
    btn.classList.remove("hidden");
    btn.onclick = async () => {
        try { await navigator.clipboard.writeText(turn.answer); btn.textContent = "Copied"; } catch { btn.textContent = "Couldn't copy"; }
        setTimeout(() => (btn.textContent = "Copy"), 1500);
    };
}

// ---------- asking ----------
async function ask(query) {
    query = query.trim();
    if (!query || busy) return;
    busy = true;
    setComposerBusy(true);

    if (!current) {
        current = { id: `t${Date.now()}`, title: query, turns: [] };
        showThread(current);
    }
    const historyForServer = current.turns.filter(t => t.answer).slice(-3).map(t => ({ q: t.q, a: t.answer }));
    const turn = { q: query, sources: null, answer: "", related: [] };
    current.turns.push(turn);
    const node = renderTurn(turn, false);
    node.scrollIntoView({ behavior: "smooth", block: "start" });

    const answerEl = $(".answer", node);
    answerEl.classList.add("writing");
    let frame = 0;
    const paint = () => { frame = 0; renderAnswer(answerEl, turn.answer, turn.sources || []); };

    try {
        const res = await fetch("/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query, history: historyForServer }),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${res.status})`);
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let nl;
            while ((nl = buffer.indexOf("\n")) >= 0) {
                const line = buffer.slice(0, nl);
                buffer = buffer.slice(nl + 1);
                if (!line.trim()) continue;
                const ev = JSON.parse(line);
                if (ev.type === "sources") { turn.sources = ev.data; renderSources($(".sources", node), ev.data); }
                else if (ev.type === "chunk") { turn.answer += ev.data; if (!frame) frame = requestAnimationFrame(paint); }
                else if (ev.type === "related") { turn.related = ev.data; }
                else if (ev.type === "error") { turn.error = ev.data; }
            }
        }
    } catch (err) {
        turn.error = err.message || "Couldn't reach the server.";
    }

    if (frame) cancelAnimationFrame(frame);
    answerEl.classList.remove("writing");
    paint();
    if (!turn.sources) { $(".sources", node).classList.remove("loading"); $(".sources-block", node).classList.add("hidden"); }
    if (turn.error) showError(answerEl, turn.error);
    renderRelated($(".related-block", node), $(".related", node), turn.related);
    enableCopy(node, turn);
    if (turn.answer) saveThread(current);

    busy = false;
    setComposerBusy(false);
}

function setComposerBusy(on) {
    document.querySelectorAll(".btn-go").forEach(b => (b.disabled = on));
    $("#follow-input").placeholder = on ? "Writing the answer…" : "Ask a follow-up";
}

// ---------- wiring ----------
function wireComposer(form, input) {
    const submit = () => { const q = input.value; input.value = ""; autoGrow(input); ask(q); };
    form.addEventListener("submit", e => { e.preventDefault(); submit(); });
    input.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    });
    input.addEventListener("input", () => autoGrow(input));
}
function autoGrow(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
}

wireComposer($("#home-form"), $("#home-input"));
wireComposer($("#follow-form"), $("#follow-input"));
document.querySelectorAll(".examples button").forEach(b => b.addEventListener("click", () => ask(b.textContent)));
$("#new-thread").addEventListener("click", showHome);
$("#brand").addEventListener("click", e => { e.preventDefault(); if (!busy) showHome(); });

const startId = new URLSearchParams(location.search).get("t");
const start = startId && loadThreads().find(t => t.id === startId);
start ? showThread(start) : showHome();
