#!/usr/bin/env python3
"""
growth_audit.py - a reusable, five-layer growth audit for any company website.

Usage:
    python growth_audit.py --url helcim.com \
        --icp '$80K/month dental practice switching from Square' \
        --competitors square.com,stripe.com

The audit runs five layers:
    1. Web Conversion          - homepage, pricing, compare/calculator, contact sales, signup
    2. Paid Acquisition        - high-intent keyword searches, ad presence and message match
    3. Organic Search & AEO    - organic rankings, AI answer-engine recommendation, llms.txt, FAQ schema
    4. Lifecycle & Post-Signup - the visible start of the signup flow (no account is created)
    5. Competitive Comparison  - homepage + pricing analysis for each competitor

How it works:
    * Pages are fetched locally with `requests` and parsed with BeautifulSoup into a compact
      "snapshot" (title, headings, CTAs, forms, schema markup, visible text). Fetch failures are
      recorded and the audit continues.
    * Each layer is one Claude API call with the server-side web_search and web_fetch tools
      enabled, so Claude can research beyond the local snapshots (and retry pages the local
      fetch could not reach).
    * A final synthesis call turns the layer notes into the markdown report, which is saved as
      <company>-growth-audit-<YYYY-MM-DD>.md in the current directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-opus-5"

# Server-side refusal fallback: if the model's safety classifiers decline a request, the API
# re-runs it on Anthropic's recommended fallback model instead of returning an empty refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Anthropic-hosted research tools. Claude runs these server-side; no local execution loop needed.
# web_fetch can only fetch URLs that already appear in the conversation, so every prompt below
# spells out the exact URLs we want Claude to be able to open.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 12}
WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10}
RESEARCH_TOOLS = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL]

# The server-side tool loop pauses after ~10 iterations (stop_reason "pause_turn").
# We resume it up to this many times per call before accepting what we have.
MAX_CONTINUATIONS = 5

HTTP_TIMEOUT = 15  # seconds per local page fetch
PAGE_TEXT_CHARS = 6000  # visible-text excerpt kept per page snapshot
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 growth-audit/1.0"
)

# Page types audited in Layer 1, with the link keywords used to discover them on the homepage
# and the fallback paths probed when discovery finds nothing.
PAGE_TYPES = {
    "pricing": {
        "label": "Pricing page",
        "keywords": ["pricing", "fees", "rates", "plans", "cost"],
        "paths": ["/pricing", "/fees", "/plans", "/rates"],
    },
    "compare": {
        "label": "Compare / calculator page",
        "keywords": ["compare", "comparison", "calculator", "savings", " vs", "-vs-", "switch"],
        "paths": ["/compare", "/calculator", "/savings-calculator", "/pricing-calculator", "/switch"],
    },
    "contact_sales": {
        "label": "Contact sales page",
        "keywords": ["contact sales", "talk to sales", "contact-sales", "sales", "book a demo", "demo", "contact"],
        "paths": ["/contact-sales", "/sales", "/contact", "/demo", "/contact-us"],
    },
    "signup": {
        "label": "Signup page",
        "keywords": ["sign up", "signup", "sign-up", "get started", "register", "create account", "start free", "open account"],
        "paths": ["/signup", "/sign-up", "/register", "/get-started", "/start"],
    },
}

# Link/button text that looks like a call to action.
CTA_PATTERN = re.compile(
    r"\b(get started|start|sign ?up|join|try|free|demo|contact|talk|book|call|apply|"
    r"open (an )?account|create|request|quote|calculate|compare|see pricing|pricing|"
    r"switch|learn more|schedule|buy|subscribe|download)\b",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You are a senior growth marketer running a structured growth audit for a B2B company.
You are rigorous and evidence-driven:
- Ground every observation in something you actually saw: a page snapshot provided to you, a search result, or a page you fetched. Cite the URL.
- Clearly separate observation from inference. If you could not observe something (a page failed to load, ads are not visible through your search tool, a flow requires an account), say so plainly instead of guessing.
- Always read findings through the lens of the Ideal Customer Profile (ICP) you are given.
- Be specific and concrete: quote CTA text, headline copy, and numbers exactly.
- Never create accounts, submit forms, or enter personal information.
Write in concise markdown with headings and bullet points."""


# ---------------------------------------------------------------------------
# Progress indicator
# ---------------------------------------------------------------------------


class Progress:
    """Prints a step header and shows a spinner with elapsed time while work is running.

    The spinner only animates on an interactive terminal; in logs/CI it prints plain lines.
    """

    FRAMES = "|/-\\"

    def __init__(self, total_steps: int):
        self.total = total_steps
        self.step = 0
        self.audit_start = time.time()

    def header(self, title: str) -> None:
        self.step += 1
        print(f"\n[{self.step}/{self.total}] {title}", flush=True)

    def info(self, msg: str) -> None:
        print(f"    - {msg}", flush=True)

    def warn(self, msg: str) -> None:
        print(f"    ! {msg}", flush=True)

    def spin(self, label: str) -> "_Spinner":
        return _Spinner(label, self.FRAMES)

    def done(self) -> None:
        mins, secs = divmod(int(time.time() - self.audit_start), 60)
        print(f"\nAudit finished in {mins}m {secs}s.", flush=True)


class _Spinner:
    def __init__(self, label: str, frames: str):
        self.label = label
        self.frames = frames
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0
        self._tty = sys.stdout.isatty()

    def __enter__(self) -> "_Spinner":
        self._start = time.time()
        if self._tty:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            print(f"    ... {self.label}", flush=True)
        return self

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            elapsed = int(time.time() - self._start)
            sys.stdout.write(f"\r    {self.frames[i % len(self.frames)]} {self.label} ({elapsed}s)")
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.2)

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        elapsed = int(time.time() - self._start)
        status = "failed" if exc_type else "done"
        line = f"    {'x' if exc_type else '+'} {self.label} ({status}, {elapsed}s)"
        if self._tty:
            sys.stdout.write("\r" + line + " " * 10 + "\n")
        else:
            print(line)
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def normalize_url(raw: str) -> str:
    """Turn 'helcim.com' or 'www.helcim.com/' into 'https://helcim.com' style base URLs."""
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urlparse(raw)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}".rstrip("/")


def bare_domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def company_name(url: str) -> str:
    """'https://www.helcim.com' -> 'helcim' (used for the report filename)."""
    domain = bare_domain(url)
    parts = domain.split(".")
    # Handle domains like 'company.co.uk' by skipping short second-level labels.
    name = parts[0] if len(parts) <= 2 or len(parts[-2]) > 3 else parts[-3]
    return re.sub(r"[^a-z0-9-]", "", name) or "company"


def same_site(url: str, base: str) -> bool:
    return bare_domain(url).endswith(bare_domain(base))


# ---------------------------------------------------------------------------
# Local page fetching and snapshotting
# ---------------------------------------------------------------------------


@dataclass
class PageSnapshot:
    """A compact, prompt-friendly summary of one fetched page."""

    label: str
    requested_url: str
    ok: bool = False
    error: str | None = None
    final_url: str | None = None
    status_code: int | None = None
    title: str = ""
    meta_description: str = ""
    h1: list[str] = field(default_factory=list)
    h2: list[str] = field(default_factory=list)
    ctas: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)
    schema_types: list[str] = field(default_factory=list)
    has_faq_schema: bool = False
    word_count: int = 0
    text_excerpt: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor text, absolute href)

    def to_prompt(self) -> str:
        """Render the snapshot as markdown for inclusion in a prompt."""
        if not self.ok:
            return (
                f"### {self.label}\n"
                f"- URL: {self.requested_url}\n"
                f"- LOCAL FETCH FAILED: {self.error}\n"
                f"- Try web_fetch on this URL; if that also fails, record the page as unavailable.\n"
            )
        lines = [
            f"### {self.label}",
            f"- URL: {self.requested_url} (final: {self.final_url}, HTTP {self.status_code})",
            f"- Title: {self.title or '(none)'}",
            f"- Meta description: {self.meta_description or '(none)'}",
            f"- H1: {' | '.join(self.h1) or '(none)'}",
            f"- H2 (first 10): {' | '.join(self.h2) or '(none)'}",
            f"- CTA-like links/buttons (in page order): {' | '.join(self.ctas) or '(none found)'}",
            f"- Forms: {' || '.join(self.forms) or '(none)'}",
            f"- Structured data types: {', '.join(self.schema_types) or '(none)'}",
            f"- FAQ schema present: {'yes' if self.has_faq_schema else 'no'}",
            f"- Visible word count: {self.word_count}"
            + (" (very low - page is likely client-side rendered; consider web_fetch)" if self.word_count < 150 else ""),
            f"- Visible text excerpt (first {PAGE_TEXT_CHARS} chars):",
            "```",
            self.text_excerpt,
            "```",
        ]
        return "\n".join(lines) + "\n"


HTTP = requests.Session()
HTTP.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})


def fetch_page(url: str, label: str) -> PageSnapshot:
    """Fetch and snapshot a page. Never raises: failures are recorded on the snapshot."""
    snap = PageSnapshot(label=label, requested_url=url)
    try:
        resp = HTTP.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        snap.error = f"{type(e).__name__}: {e}"
        return snap

    snap.status_code = resp.status_code
    snap.final_url = resp.url
    if resp.status_code >= 400:
        snap.error = f"HTTP {resp.status_code}"
        return snap
    if "html" not in resp.headers.get("Content-Type", "html"):
        snap.error = f"Non-HTML content ({resp.headers.get('Content-Type')})"
        return snap

    try:
        _parse_html(resp.text, resp.url, snap)
        snap.ok = True
    except Exception as e:  # malformed HTML should never stop the audit
        snap.error = f"Parse error: {type(e).__name__}: {e}"
    return snap


def _parse_html(html: str, base_url: str, snap: PageSnapshot) -> None:
    soup = BeautifulSoup(html, "html.parser")

    # Structured data: collect @type values from every JSON-LD block (recursively).
    types: set[str] = set()
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            _collect_ld_types(json.loads(tag.string or ""), types)
        except (json.JSONDecodeError, TypeError):
            continue
    # Microdata (itemtype="https://schema.org/FAQPage") counts too.
    for tag in soup.find_all(attrs={"itemtype": True}):
        types.add(str(tag["itemtype"]).rstrip("/").split("/")[-1])
    snap.schema_types = sorted(types)
    snap.has_faq_schema = any(t.lower() in ("faqpage", "qapage") for t in types)

    snap.title = soup.title.get_text(strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"})
    snap.meta_description = (meta.get("content") or "").strip() if meta else ""

    # Links (kept before we strip nav/footer so discovery can use them).
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(base_url, a["href"])
        if href.startswith("http"):
            snap.links.append((text, href))

    # CTAs: buttons and links whose text reads like a call to action, de-duplicated in order.
    seen: set[str] = set()
    for el in soup.find_all(["a", "button"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        if not text or len(text) > 60 or not CTA_PATTERN.search(text):
            continue
        href = el.get("href")
        entry = f"{text} -> {urljoin(base_url, href)}" if href else f"{text} [button]"
        if text.lower() not in seen:
            seen.add(text.lower())
            snap.ctas.append(entry)
        if len(snap.ctas) >= 25:
            break

    # Forms: summarise visible fields so Claude can judge upfront friction.
    for i, form in enumerate(soup.find_all("form")[:5], 1):
        fields = []
        for inp in form.find_all(["input", "select", "textarea"]):
            if inp.get("type") in ("hidden", "submit", "button"):
                continue
            name = inp.get("name") or inp.get("id") or inp.get("placeholder") or inp.name
            required = "*" if inp.has_attr("required") else ""
            fields.append(f"{name}{required}")
        snap.forms.append(f"form {i}: {len(fields)} fields ({', '.join(fields[:15])})")

    snap.h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")][:5]
    snap.h2 = [h.get_text(" ", strip=True) for h in soup.find_all("h2")][:10]

    # Visible text: drop non-content elements, collapse whitespace.
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    snap.word_count = len(text.split())
    snap.text_excerpt = text[:PAGE_TEXT_CHARS]


def _collect_ld_types(node, types: set[str]) -> None:
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            types.add(t)
        elif isinstance(t, list):
            types.update(x for x in t if isinstance(x, str))
        for v in node.values():
            _collect_ld_types(v, types)
    elif isinstance(node, list):
        for v in node:
            _collect_ld_types(v, types)


def discover_page(
    home: PageSnapshot, base: str, page_type: str, progress: Progress, label: str | None = None
) -> PageSnapshot:
    """Find a page of the given type: first via homepage links, then by probing common paths."""
    spec = PAGE_TYPES[page_type]
    label = label or spec["label"]

    # 1) Homepage links whose anchor text or path matches the page type's keywords.
    candidates: list[str] = []
    for text, href in home.links:
        if not same_site(href, base):
            continue
        haystack = f"{text.lower()} {urlparse(href).path.lower()}"
        if any(k in haystack for k in spec["keywords"]) and href not in candidates:
            candidates.append(href.split("#")[0])
    # 2) Common fallback paths.
    candidates += [base + p for p in spec["paths"] if base + p not in candidates]

    failures = []
    for url in candidates[:6]:  # cap requests per page type
        snap = fetch_page(url, label)
        if snap.ok:
            progress.info(f"{label}: {url}")
            return snap
        failures.append(f"{url} ({snap.error})")

    progress.warn(f"{label}: not found - tried {len(failures)} URL(s)")
    missing = PageSnapshot(label=label, requested_url=candidates[0] if candidates else base)
    missing.error = "No reachable page found. Tried: " + "; ".join(failures)
    return missing


def check_llms_txt(base: str) -> str:
    """Check /llms.txt at the root domain and return a short human-readable result."""
    url = f"{base}/llms.txt"
    try:
        resp = HTTP.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return f"{url}: request failed ({type(e).__name__}: {e})"
    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code == 200 and "html" not in ctype:
        preview = resp.text.strip()[:800]
        return f"{url}: PRESENT (HTTP 200, {len(resp.text)} chars). Preview:\n```\n{preview}\n```"
    if resp.status_code == 200:
        return f"{url}: returned HTTP 200 but as HTML (likely a soft-404 / catch-all page) - treat as ABSENT"
    return f"{url}: ABSENT (HTTP {resp.status_code})"


# ---------------------------------------------------------------------------
# Claude API calls
# ---------------------------------------------------------------------------


class Researcher:
    """Thin wrapper around the Messages API with web tools, pause_turn resumption and fallbacks."""

    def __init__(self, model: str):
        self.client = anthropic.Anthropic()
        self.model = model
        self.use_fallback = True
        self.sources: dict[str, str] = {}  # url -> title, gathered from citations and search results

    def ask(self, prompt: str, tools: list | None = None, max_tokens: int = 32000) -> str:
        """Run one research request and return the concatenated text of Claude's answer."""
        messages: list[dict] = [{"role": "user", "content": prompt}]
        texts: list[str] = []

        for _ in range(MAX_CONTINUATIONS + 1):
            response = self._stream(messages, tools, max_tokens)
            self._collect_sources(response)

            if response.stop_reason == "refusal":
                texts.append("\n\n_(The model declined to complete this part of the audit.)_")
                break

            texts.extend(b.text for b in response.content if b.type == "text")

            if response.stop_reason == "pause_turn":
                # The server-side tool loop hit its iteration limit. Send the partial assistant
                # turn back unchanged and the server resumes where it left off.
                messages = messages + [{"role": "assistant", "content": response.content}]
                continue
            if response.stop_reason == "max_tokens":
                texts.append("\n\n_(Output truncated: max_tokens reached.)_")
            break

        return "".join(texts).strip()

    def _stream(self, messages: list[dict], tools: list | None, max_tokens: int):
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        if self.use_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["extra_body"] = {"fallbacks": "default"}
        try:
            with self.client.beta.messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        except anthropic.BadRequestError as e:
            # Some accounts/models don't accept the fallback beta; disable it and retry once.
            if self.use_fallback and "fallback" in str(e).lower():
                self.use_fallback = False
                return self._stream(messages, tools, max_tokens)
            raise

    def _collect_sources(self, response) -> None:
        for block in response.content:
            if block.type == "text" and getattr(block, "citations", None):
                for c in block.citations:
                    url = getattr(c, "url", None)
                    if url:
                        self.sources.setdefault(url, getattr(c, "title", "") or "")
            elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                for r in block.content:
                    url = getattr(r, "url", None)
                    if url:
                        self.sources.setdefault(url, getattr(r, "title", "") or "")


def safe_layer(name: str, progress: Progress, fn) -> str:
    """Run a layer; if it fails, record the failure in the notes and keep the audit going."""
    try:
        with progress.spin(f"Researching {name}"):
            return fn()
    except anthropic.AuthenticationError:
        raise  # a bad key will fail every layer - stop immediately with a clear message
    except anthropic.APIStatusError as e:
        msg = f"API error {e.status_code}: {e.message}"
    except anthropic.APIConnectionError as e:
        msg = f"Connection error: {e}"
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
    progress.warn(f"{name} could not be completed ({msg}). Continuing.")
    return f"**{name} could not be completed.** Reason: {msg}"


# ---------------------------------------------------------------------------
# Audit context shared by every layer
# ---------------------------------------------------------------------------


@dataclass
class AuditContext:
    url: str
    icp: str
    competitors: list[str]
    company: str
    date: str
    pages: dict[str, PageSnapshot] = field(default_factory=dict)
    competitor_pages: dict[str, dict[str, PageSnapshot]] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    llms_txt: str = ""
    fetch_log: list[str] = field(default_factory=list)

    def intro(self) -> str:
        comps = ", ".join(self.competitors) or "(none provided)"
        return (
            f"Target company: {self.company} ({self.url})\n"
            f"Ideal Customer Profile (ICP) lens: {self.icp}\n"
            f"Competitors: {comps}\n"
            f"Audit date: {self.date}\n"
        )


def generate_keywords(researcher: Researcher, ctx: AuditContext) -> list[str]:
    """Ask Claude for 5 high-intent keywords tailored to the ICP; fall back to generic ones."""
    first_comp = company_name(ctx.competitors[0]) if ctx.competitors else "square"
    fallback = [
        "best payment processor for small business",
        f"switch from {first_comp} payment processing",
        "interchange plus pricing",
        "lowest credit card processing fees",
        f"{company_name(ctx.url)} vs {first_comp}",
    ]
    prompt = (
        f"{ctx.intro()}\n"
        "List exactly 5 high-intent Google search queries that a prospect matching this ICP would type "
        "when actively evaluating or switching to a product like the target company's. Mix: one category "
        "'best X for Y' query, one 'switch from <competitor>' query, one pricing-model query, one "
        "cost/fees query, and one ICP-vertical-specific query. Use the language real buyers use.\n"
        'Respond with ONLY a JSON array of 5 strings, e.g. ["query one", "query two", ...].'
    )
    try:
        text = researcher.ask(prompt, tools=None, max_tokens=4000)
        match = re.search(r"\[.*\]", text, re.DOTALL)
        keywords = [k.strip() for k in json.loads(match.group(0)) if isinstance(k, str) and k.strip()]
        if len(keywords) >= 3:
            return keywords[:5]
    except Exception:
        pass
    return fallback


# ---------------------------------------------------------------------------
# The five audit layers
# ---------------------------------------------------------------------------


def layer1_web_conversion(r: Researcher, ctx: AuditContext) -> str:
    snapshots = "\n".join(s.to_prompt() for s in ctx.pages.values())
    return r.ask(
        f"{ctx.intro()}\n"
        "## Task: Layer 1 - Web Conversion audit\n"
        "Analyze each of the following pages of the target site. Local snapshots are below; use "
        "web_fetch on any URL listed here if a snapshot failed, looks client-side rendered, or you "
        "need more detail.\n\n"
        "For EACH page report:\n"
        "- **Primary CTA** (exact text) and **competing CTAs** that dilute it\n"
        "- **Message clarity**: can this ICP tell within 5 seconds what this is, who it's for, and why it beats their current option?\n"
        "- **Friction points** (form length, gating, jargon, missing prices, dead ends)\n"
        "- **Time / steps to a value moment** (e.g. seeing an estimated monthly cost for this ICP's volume)\n"
        "- **Missing conversion elements** (social proof for this vertical, switching guarantees, calculators, pricing transparency, trust signals)\n"
        "If a page could not be retrieved at all, say so and move on.\n"
        "End with a 3-bullet summary of the most important Layer 1 findings and ONE genuine strength.\n\n"
        f"## Page snapshots\n{snapshots}",
        tools=RESEARCH_TOOLS,
    )


def layer2_paid(r: Researcher, ctx: AuditContext) -> str:
    kw = "\n".join(f"{i}. {k}" for i, k in enumerate(ctx.keywords, 1))
    home = ctx.pages.get("home")
    home_summary = home.to_prompt() if home else ""
    return r.ask(
        f"{ctx.intro()}\n"
        "## Task: Layer 2 - Paid Acquisition audit\n"
        f"Search for each of these high-intent keywords with web_search:\n{kw}\n\n"
        "For each keyword report:\n"
        "- Whether the target company appears in **paid** results, and what the ad copy says\n"
        "- Whether message match between the ad and its landing page is tight (fetch the landing page if you can identify it)\n"
        "- The ad's CTA\n"
        "- Which competitors appear to be bidding\n\n"
        "IMPORTANT: your web_search tool returns organic results and generally does NOT show live Google "
        "ads. Do not invent ad copy. Instead, gather indirect evidence where available (e.g. Google Ads "
        "Transparency Center pages, competitor landing pages built for paid traffic such as /lp/ or "
        "/go/ URLs, 'vs' and 'switch from' pages, third-party ad-intelligence write-ups) and label every "
        "paid-search claim as either OBSERVED or INFERRED. Where nothing is observable, state that and "
        "describe the manual check the reader should run.\n"
        "End with a 3-bullet summary and ONE genuine strength of the target company's paid posture "
        "(or its most paid-ready landing asset).\n\n"
        f"## Target homepage snapshot (for message-match comparison)\n{home_summary}",
        tools=RESEARCH_TOOLS,
    )


def answer_engine_probe(r: Researcher, ctx: AuditContext) -> str:
    """Ask Claude, acting as a neutral AI answer engine, the ICP's buying question."""
    question = f"What is the best payment processor for a {ctx.icp}?"
    answer = r.ask(
        "Answer the following question the way a neutral AI answer engine would for a real buyer. "
        "Research with web_search, recommend specific providers, and cite your sources. "
        "Do not favor any provider unless the evidence supports it.\n\n"
        f"Question: {question}",
        tools=[WEB_SEARCH_TOOL],
        max_tokens=16000,
    )
    return f"**Question asked:** {question}\n\n**Answer engine response:**\n\n{answer}"


def layer3_organic_aeo(r: Researcher, ctx: AuditContext) -> str:
    kw = "\n".join(f"{i}. {k}" for i, k in enumerate(ctx.keywords, 1))
    aeo = answer_engine_probe(r, ctx)
    faq_lines = "\n".join(
        f"- {s.label} ({s.requested_url}): "
        + (f"FAQ schema {'PRESENT' if s.has_faq_schema else 'absent'}; schema types: {', '.join(s.schema_types) or 'none'}"
           if s.ok else f"not checked - {s.error}")
        for s in ctx.pages.values()
    )
    return r.ask(
        f"{ctx.intro()}\n"
        "## Task: Layer 3 - Organic Search and AEO (answer-engine optimization) audit\n"
        f"1. Search each keyword with web_search and note where the target company ranks organically "
        f"(position, which URL ranks, and who outranks it - including review/listicle sites):\n{kw}\n\n"
        "2. Analyze the AI answer-engine probe below: is the target company recommended? How is it "
        "described, and is that description accurate and favorable for this ICP? Which sources are "
        "cited, and does the target company own or appear in them?\n\n"
        "3. Interpret the llms.txt and FAQ schema checks below (already performed programmatically).\n\n"
        "End with a 3-bullet summary and ONE genuine strength.\n\n"
        f"## AI answer-engine probe\n{aeo}\n\n"
        f"## llms.txt check\n{ctx.llms_txt}\n\n"
        f"## FAQ schema check (key pages)\n{faq_lines}",
        tools=RESEARCH_TOOLS,
    )


def layer4_lifecycle(r: Researcher, ctx: AuditContext) -> str:
    signup = ctx.pages.get("signup")
    signup_snap = signup.to_prompt() if signup else "(no signup page found)"
    return r.ask(
        f"{ctx.intro()}\n"
        "## Task: Layer 4 - Lifecycle and Post-Signup audit\n"
        "Analyze the beginning of the signup flow as far as it is accessible WITHOUT creating an "
        "account or submitting any form. Use the snapshot below and web_fetch on the signup URL (and any "
        "linked onboarding/help-center pages such as 'getting started' or 'how to set up' guides) for more "
        "detail. Public onboarding docs, help-center articles and review sites describing the onboarding/"
        "underwriting process are fair game - label those findings as secondhand.\n\n"
        "Report:\n"
        "- Number of steps visible (or stated) in the flow\n"
        "- Information required upfront (which fields; anything that signals underwriting friction such as SSN, bank statements, EIN)\n"
        "- Personalization signals (does the flow ask about business type/volume and adapt?)\n"
        "- First value moment and estimated time to reach it for this ICP (e.g. first transaction, first payout, seeing savings)\n"
        "- Visible onboarding prompts, checklists, emails or nurture hooks\n"
        "- What could NOT be observed without an account\n"
        "End with a 3-bullet summary and ONE genuine strength.\n\n"
        f"## Signup page snapshot\n{signup_snap}",
        tools=RESEARCH_TOOLS,
    )


def layer5_competitive(r: Researcher, ctx: AuditContext) -> str:
    if not ctx.competitors:
        return "No competitors were provided, so Layer 5 was skipped."
    blocks = []
    for comp, pages in ctx.competitor_pages.items():
        blocks.append(f"## Competitor: {comp}\n" + "\n".join(s.to_prompt() for s in pages.values()))
    target = "\n".join(ctx.pages[k].to_prompt() for k in ("home", "pricing") if k in ctx.pages)
    return r.ask(
        f"{ctx.intro()}\n"
        "## Task: Layer 5 - Competitive Comparison\n"
        "Run the same homepage and pricing page analysis on each competitor as for the target company. "
        "Use the snapshots below and web_fetch where a snapshot failed or is thin.\n\n"
        "For EACH competitor report:\n"
        "- How quickly a prospect matching the ICP gets to a cost/value estimate (clicks, and whether a calculator or published rates exist)\n"
        "- The primary CTA (exact text)\n"
        "- ONE specific contrast observation vs. the target company, with evidence from both sides\n"
        "Finish with a short comparison table (competitor | time-to-estimate | primary CTA | key contrast) "
        "and ONE genuine competitive strength of the target company.\n\n"
        f"# Target company (for contrast)\n{target}\n\n# Competitors\n" + "\n".join(blocks),
        tools=RESEARCH_TOOLS,
    )


# ---------------------------------------------------------------------------
# Synthesis into the final report
# ---------------------------------------------------------------------------

LAYER_TITLES = [
    "Layer 1 - Web Conversion",
    "Layer 2 - Paid Acquisition",
    "Layer 3 - Organic Search and AEO",
    "Layer 4 - Lifecycle and Post-Signup",
    "Layer 5 - Competitive Comparison",
]


def synthesize(r: Researcher, ctx: AuditContext, notes: dict[str, str]) -> str:
    """Turn raw layer notes into report sections 2-5 (section 1 is built deterministically)."""
    joined = "\n\n".join(f"# {title}\n\n{notes[title]}" for title in LAYER_TITLES)
    return r.ask(
        f"{ctx.intro()}\n"
        "Below are the raw research notes from a five-layer growth audit. Write the body of the final "
        "audit report in markdown with EXACTLY these four sections (the Audit Scope section is added "
        "separately - do not write it, and do not add a title or preamble):\n\n"
        "## 2. What's Working\n"
        "One genuine strength per layer (five bullets, each labelled with its layer), grounded in evidence.\n\n"
        "## 3. Channel-by-Channel Observations\n"
        "A `###` subsection per layer. Each observation is ONE bullet formatted exactly as:\n"
        "`- **Observed:** <what I observed> → **Why it matters:** <impact for this ICP> → **Hypothesis to test:** <a falsifiable test with the metric that would move>`\n"
        "3-5 observations per layer. If a layer could not be completed, say so in one line.\n\n"
        "## 4. Priority Stack\n"
        "The top 3 opportunities ranked by impact × confidence × speed. For each: a `###` heading with the "
        "opportunity name, a line scoring Impact / Confidence / Speed (High/Med/Low each), then ONE "
        "paragraph of rationale referencing the evidence.\n\n"
        "## 5. Data I'd Want Before Committing\n"
        "3-4 specific metrics or analytics questions (e.g. funnel conversion rates by step, paid search "
        "impression share on named keywords, cohort activation times) and what each would confirm or kill.\n\n"
        "Rules: stay faithful to the notes - do not add facts that are not in them; keep OBSERVED vs INFERRED "
        "distinctions; write for a growth lead who will act on this.\n\n"
        f"# Raw research notes\n\n{joined}",
        tools=None,
    )


def build_report(ctx: AuditContext, body: str, notes: dict[str, str], sources: dict[str, str]) -> str:
    comps = ", ".join(ctx.competitors) or "none"
    scope = (
        f"# {ctx.company.title()} Growth Audit\n\n"
        "## 1. Audit Scope\n\n"
        f"- **URL audited:** {ctx.url}\n"
        f"- **ICP lens:** {ctx.icp}\n"
        f"- **Date:** {ctx.date}\n"
        f"- **Competitors compared:** {comps}\n"
        "- **Channels covered:** Web conversion (homepage, pricing, compare/calculator, contact sales, signup), "
        "paid search, organic search, AI answer engines (AEO), lifecycle / signup flow, competitive positioning\n"
        f"- **High-intent keywords used:** {'; '.join(ctx.keywords)}\n"
        "- **Method notes:** Public pages only; no accounts created and no forms submitted. Paid-search "
        "findings are limited to what is publicly observable and are labelled OBSERVED vs INFERRED.\n"
    )

    appendix = ["\n---\n\n## Appendix A - Page Fetch Log\n"]
    appendix += [f"- {line}" for line in ctx.fetch_log] or ["- (no pages fetched)"]
    appendix.append("\n## Appendix B - Raw Layer Notes\n")
    for title in LAYER_TITLES:
        appendix.append(f"<details>\n<summary>{title}</summary>\n\n{notes[title]}\n\n</details>\n")
    if sources:
        appendix.append("\n## Appendix C - Sources Consulted\n")
        appendix += [f"- [{title or url}]({url})" for url, title in sorted(sources.items())]

    return scope + "\n" + body.strip() + "\n" + "\n".join(appendix) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a five-layer growth audit on a company website.")
    p.add_argument("--url", required=True, help="Company website to audit, e.g. helcim.com")
    p.add_argument("--icp", required=True, help='ICP lens, e.g. "$80K/month dental practice switching from Square"')
    p.add_argument("--competitors", default="", help="Comma-separated competitor URLs, e.g. square.com,stripe.com")
    p.add_argument("--keywords", default="", help="Optional: comma-separated keywords to use instead of generated ones")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model ID (default: {DEFAULT_MODEL})")
    return p.parse_args()


def fetch_all_pages(ctx: AuditContext, progress: Progress) -> None:
    """Fetch target and competitor pages locally, logging every outcome."""
    home = fetch_page(ctx.url, "Homepage")
    if home.ok:
        progress.info(f"Homepage: {ctx.url}")
    else:
        progress.warn(f"Homepage fetch failed ({home.error}) - Claude will try web_fetch instead")
    ctx.pages["home"] = home
    for key in PAGE_TYPES:
        ctx.pages[key] = discover_page(home, ctx.url, key, progress)

    ctx.llms_txt = check_llms_txt(ctx.url)
    progress.info("llms.txt: " + ctx.llms_txt.splitlines()[0].split(": ", 1)[-1])

    for comp in ctx.competitors:
        chome = fetch_page(comp, f"{bare_domain(comp)} homepage")
        progress.info(f"Competitor {bare_domain(comp)} homepage: {'ok' if chome.ok else chome.error}")
        cpricing = discover_page(chome, comp, "pricing", progress, label=f"{bare_domain(comp)} pricing page")
        ctx.competitor_pages[comp] = {"home": chome, "pricing": cpricing}

    all_snaps = list(ctx.pages.values()) + [s for d in ctx.competitor_pages.values() for s in d.values()]
    for s in all_snaps:
        ctx.fetch_log.append(
            f"{s.label}: {s.final_url or s.requested_url} - OK (HTTP {s.status_code})" if s.ok
            else f"{s.label}: {s.requested_url} - FAILED ({s.error})"
        )


def main() -> int:
    load_dotenv()  # reads ANTHROPIC_API_KEY from a .env file in the current directory
    args = parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY is not set. Add it to a .env file or your environment.", file=sys.stderr)
        return 1

    url = normalize_url(args.url)
    competitors = [normalize_url(c) for c in args.competitors.split(",") if c.strip()]
    ctx = AuditContext(
        url=url,
        icp=args.icp,
        competitors=competitors,
        company=company_name(url),
        date=dt.date.today().isoformat(),
    )
    researcher = Researcher(args.model)
    progress = Progress(total_steps=8)

    print(f"Growth audit: {url}\nICP: {args.icp}\nCompetitors: {', '.join(competitors) or 'none'}")

    try:
        # Step 1: gather page snapshots locally (fast, no API cost).
        progress.header("Fetching pages (target + competitors)")
        fetch_all_pages(ctx, progress)

        # Step 2: choose keywords for Layers 2 and 3.
        progress.header("Choosing high-intent keywords")
        if args.keywords:
            ctx.keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
        else:
            with progress.spin("Generating ICP-specific keywords"):
                ctx.keywords = generate_keywords(researcher, ctx)
        for k in ctx.keywords:
            progress.info(k)

        # Steps 3-7: the five layers. Each failure is captured and the audit continues.
        layers = [
            layer1_web_conversion,
            layer2_paid,
            layer3_organic_aeo,
            layer4_lifecycle,
            layer5_competitive,
        ]
        notes: dict[str, str] = {}
        for title, fn in zip(LAYER_TITLES, layers):
            progress.header(title)
            notes[title] = safe_layer(title, progress, lambda fn=fn: fn(researcher, ctx))

        # Step 8: synthesize into the final report and save it.
        progress.header("Writing the audit report")
        try:
            with progress.spin("Synthesizing findings"):
                body = synthesize(researcher, ctx, notes)
        except anthropic.AuthenticationError:
            raise
        except Exception as e:
            progress.warn(f"Synthesis failed ({type(e).__name__}: {e}); saving raw layer notes instead")
            body = "_Synthesis failed; see the raw layer notes in Appendix B._\n"
    except anthropic.AuthenticationError:
        print("\nError: the Anthropic API rejected the API key. Check ANTHROPIC_API_KEY.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAudit cancelled.", file=sys.stderr)
        return 130

    filename = f"{ctx.company}-growth-audit-{ctx.date}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(build_report(ctx, body, notes, researcher.sources))

    progress.done()
    print(f"Report saved to {os.path.abspath(filename)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
