#!/usr/bin/env python3
"""Search and language-model adapters, so the web check works with any search API and any model, or with neither.

    web_tools.py status                                   which search API and model are configured (from the environment)
    web_tools.py search "query" --domains docs.aws.amazon.com

Search (pick one by setting its key; the first found is used, or set SOLSARCH_SEARCH to choose):
    TAVILY_API_KEY            Tavily
    BRAVE_API_KEY             Brave Search
    SERPER_API_KEY            Serper (Google results)
    GOOGLE_CSE_KEY + GOOGLE_CSE_ID    Google Programmable Search
Language model (optional; used only to choose a sentence the plain rules could not find, and the choice is checked
against the page text, so a wrong or invented answer is rejected):
    OPENAI_API_KEY            OpenAI, or any OpenAI-compatible server with SOLSARCH_LLM_BASE_URL
                              (Ollama, vLLM, LM Studio, Groq, Together, OpenRouter, Mistral, DeepSeek ...)
    ANTHROPIC_API_KEY         Anthropic
    GEMINI_API_KEY            Google Gemini
    SOLSARCH_LLM_MODEL        model name for the chosen provider

Nothing in a plan depends on either: services are chosen by fixed rules. These only help to record the one official
sentence for each service, and every sentence is validated by facts.py (vendor domain, names the service, not a pointer).
Standard library only.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import safe_http  # noqa: F401  (installs the https-only redirect rules)

TIMEOUT = 25
MAX_BYTES = 1500000
USER_AGENT = "SolsArch-Early-Cloud-Architect/1.0 (+https://github.com/SolsArchi/SolsArch-Early-Cloud-Architect)"
GOOD = re.compile(r"\b(is a|is an|are a|provides?|lets? you|enables?|helps?|offers?|allows?|is available|are available|runs?|manages?|stores?|delivers?|supports?)\b", re.I)
RETIRED = re.compile(r"\b(deprecated|retired|end of life|end-of-life|discontinued|no longer (?:available|supported)|sunset)\b", re.I)
RENAMED = re.compile(r"\b(renamed to|now called|now known as|replaced by|has been replaced)\b", re.I)


def http_json(url, payload=None, headers=None, method=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    safe_http.checked(url, allow_loopback_http=True)
    req = urllib.request.Request(url, data=data, method=method or ("POST" if payload is not None else "GET"),
                                 headers=dict({"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"}, **(headers or {})))
    # Bandit B310 note: only https is opened (the address was checked and redirects are limited by safe_http)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:  # nosec B310
        return json.loads(response.read(MAX_BYTES).decode("utf-8", errors="replace"))


def fetch_text(url):
    """The visible text of an https page, or '' when it cannot be read."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        return ""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        # Bandit B310 note: only https is opened (the address was checked and redirects are limited by safe_http)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:  # nosec B310
            raw = response.read(MAX_BYTES).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    return html_to_text(raw)


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>|</(p|li|h[1-6]|div|tr|td|section)>", "\n", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def sentences(text):
    out = []
    for block in text.splitlines():
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", block):
            s = s.strip()
            if 40 <= len(s) <= 400:
                out.append(s)
    return out


# ------------------------------------------------------------------ search
def search_provider():
    chosen = os.environ.get("SOLSARCH_SEARCH", "").lower()
    order = [("tavily", "TAVILY_API_KEY"), ("brave", "BRAVE_API_KEY"), ("serper", "SERPER_API_KEY"), ("google", "GOOGLE_CSE_KEY")]
    for name, env in order:
        if (not chosen or chosen == name) and os.environ.get(env):
            return name
    return None


def search(query, domains=(), limit=5, provider=None, fetcher=None):
    """Return [{url, title, snippet}] limited to the given domains. fetcher(url, payload, headers) is injectable for tests."""
    provider = provider or search_provider()
    if not provider:
        return []
    fetcher = fetcher or (lambda url, payload=None, headers=None: http_json(url, payload, headers))
    scoped = query + "".join(" site:%s" % d for d in list(domains)[:1])
    results = []
    if provider == "tavily":
        body = fetcher("https://api.tavily.com/search", {"api_key": os.environ["TAVILY_API_KEY"], "query": query, "max_results": 10,
                                                         "include_domains": list(domains)}, {})
        results = [{"url": r.get("url"), "title": r.get("title"), "snippet": r.get("content")} for r in body.get("results", [])]
    elif provider == "brave":
        body = fetcher("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": scoped, "count": 10}), None,
                       {"X-Subscription-Token": os.environ["BRAVE_API_KEY"]})
        results = [{"url": r.get("url"), "title": r.get("title"), "snippet": r.get("description")} for r in (body.get("web") or {}).get("results", [])]
    elif provider == "serper":
        body = fetcher("https://google.serper.dev/search", {"q": scoped, "num": 10}, {"X-API-KEY": os.environ["SERPER_API_KEY"]})
        results = [{"url": r.get("link"), "title": r.get("title"), "snippet": r.get("snippet")} for r in body.get("organic", [])]
    elif provider == "google":
        body = fetcher("https://www.googleapis.com/customsearch/v1?" + urllib.parse.urlencode(
            {"key": os.environ["GOOGLE_CSE_KEY"], "cx": os.environ.get("GOOGLE_CSE_ID", ""), "q": scoped, "num": 10}), None, {})
        results = [{"url": r.get("link"), "title": r.get("title"), "snippet": r.get("snippet")} for r in body.get("items", [])]
    kept = []
    for r in results:
        host = urllib.parse.urlparse(r.get("url") or "").hostname or ""
        if r.get("url", "").startswith("https://") and (not domains or any(host == d or host.endswith("." + d) for d in domains)):
            kept.append(r)
    return kept[:limit]


# ------------------------------------------------------------------ language model
def llm_provider():
    chosen = os.environ.get("SOLSARCH_LLM", "").lower()
    if chosen in ("openai", "anthropic", "gemini"):
        return chosen
    for name, env in (("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"), ("gemini", "GEMINI_API_KEY")):
        if os.environ.get(env):
            return name
    if os.environ.get("SOLSARCH_LLM_BASE_URL"):
        return "openai"
    return None


def llm_complete(prompt, provider=None, fetcher=None, max_tokens=60):
    """One short completion at temperature 0. Returns '' when no model is configured or the call fails."""
    provider = provider or llm_provider()
    if not provider:
        return ""
    fetcher = fetcher or (lambda url, payload=None, headers=None: http_json(url, payload, headers))
    model = os.environ.get("SOLSARCH_LLM_MODEL", "")
    try:
        if provider == "anthropic":
            body = fetcher("https://api.anthropic.com/v1/messages",
                           {"model": model or "claude-haiku-4-5", "max_tokens": max_tokens, "temperature": 0, "messages": [{"role": "user", "content": prompt}]},
                           {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"})
            return "".join(b.get("text", "") for b in body.get("content", []))
        if provider == "gemini":
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
            url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (model or "gemini-2.0-flash", key)
            body = fetcher(url, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}}, {})
            return "".join(p.get("text", "") for c in body.get("candidates", []) for p in c.get("content", {}).get("parts", []))
        base = os.environ.get("SOLSARCH_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        body = fetcher(base + "/chat/completions", {"model": model or "gpt-4o-mini", "temperature": 0, "max_tokens": max_tokens,
                                                    "messages": [{"role": "user", "content": prompt}]},
                       {"Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", "none")})
        return (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return ""


# ------------------------------------------------------------------ choosing the sentence
def pick_sentence(item, text, validate, llm=None):
    """Choose one sentence from the page that facts.validate accepts. Plain rules first, then (optionally) a model's pick among the sentences."""
    candidates = [s for s in sentences(text) if validate(item, "https://" + item["domains"][0], s, "confirmed", "") is None]
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda s: (0 if GOOD.search(s) else 1, len(s) > 220, text.find(s)))
    if GOOD.search(ranked[0]) or llm is None:
        return ranked[0]
    listing = "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(ranked[:12]))
    answer = llm("Service: %s\nPick the number of the one sentence that best says what the service is or that it is available. Reply with the number only.\n%s" % (item["service"], listing))
    m = re.search(r"\d+", answer or "")
    if m and 1 <= int(m.group()) <= min(12, len(ranked)):
        return ranked[int(m.group()) - 1]
    return ranked[0]


def verdict_for(sentence, page_text):
    """confirmed unless the page itself says the service was retired or renamed; those need a person."""
    if RETIRED.search(sentence) or RENAMED.search(sentence):
        return "review"
    return "confirmed"


def auto_verify(item, validate, search_fn=search, fetch_fn=fetch_text, llm=None):
    """Try to record one official sentence for a queue item. Returns {'status': ..., ...}."""
    results = search_fn(item["query"], item["domains"])
    for r in results[:4]:
        text = fetch_fn(r["url"])
        if not text:
            continue
        sentence = pick_sentence(item, text, validate, llm)
        if not sentence:
            continue
        verdict = verdict_for(sentence, text)
        if verdict == "review":
            return {"status": "needs_review", "url": r["url"], "quote": sentence, "why": "the page says the service may be retired or renamed"}
        if validate(item, r["url"], sentence, "confirmed", "") is None:
            import hashlib
            return {"status": "found", "url": r["url"], "quote": sentence, "verdict": "confirmed",
                    "page_sha256": hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()}
    return {"status": "not_found", "searched": len(results)}


def status():
    return {"search": search_provider(), "llm": llm_provider(), "llm_model": os.environ.get("SOLSARCH_LLM_MODEL") or None,
            "note": "Set TAVILY_API_KEY, BRAVE_API_KEY, SERPER_API_KEY or GOOGLE_CSE_KEY+GOOGLE_CSE_ID for search; OPENAI_API_KEY, ANTHROPIC_API_KEY or GEMINI_API_KEY (and optionally SOLSARCH_LLM_BASE_URL) for a model."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search and model adapters")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--domains", default="")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    out = status() if args.command == "status" else {"results": search(args.query, [d for d in args.domains.split(",") if d])}
    sys.stdout.write(json.dumps(out, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
