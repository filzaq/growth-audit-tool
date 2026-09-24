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
      "snapshot" (title, headings, CTAs, forms, schema markup, visible text). Thin (<500 words) or
      bot-blocked pages are re-rendered in headless Chromium via Playwright; content that only
      appears after JavaScript runs is flagged as a CRO (Layer 1) and SEO/AEO (Layer 3) finding.
      Fetch failures are recorded and the audit continues.
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

# JavaScript-rendered pages: when the raw HTML has fewer words than this, the page is re-fetched in
# a headless browser (Playwright + Chromium). Same when a plain request is blocked with one of the
# status codes below, since bot protection often lets a real browser through.
JS_RENDER_MIN_WORDS = 500
BROWSER_RETRY_STATUSES = {401, 403, 429, 503}
BROWSER_TIMEOUT_MS = 30_000

# Price-like tokens ("$349", "0.35%", "per month", "/mo") - used to spot pricing that only exists
# after JavaScript runs.
PRICE_PATTERN = re.compile(
    r"[$€£]\s?\d[\d,.]*[kKmM]?\b|\b\d+(?:\.\d+)?\s?%|\bper (?:month|transaction|user|seat|year)\b|/mo(?:nth)?\b",
    re.IGNORECASE,
)
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

    # Structured hooks. The terminal output above already covers these, so they are no-ops here;
    # the web UI (app.py) overrides them to stream layer results to the browser.
    def keywords_chosen(self, keywords: list[str]) -> None:
        pass

    def layer_started(self, index: int, title: str) -> None:
        pass

    def layer_finished(self, index: int, title: str, notes: str, ok: bool) -> None:
        pass


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
    text: str = field(default="", repr=False)  # full visible text (not sent to Claude)
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor text, absolute href)
    # How the page was fetched: "standard" (plain HTTP) or "playwright" (headless browser).
    fetch_method: str = "standard"
    raw_word_count: int | None = None  # words in the raw HTML, when the browser version was used
    js_missing: list[tuple[str, str]] = field(default_factory=list)  # (short, detailed) content only JS shows
    render_note: str = ""  # what happened with the browser fallback, if it ran

    @property
    def js_rendered(self) -> bool:
        """True when key content only appeared after JavaScript ran in a headless browser."""
        return bool(self.js_missing)

    def method_summary(self) -> str:
        if self.fetch_method == "playwright":
            if self.raw_word_count is not None:
                return (
                    f"headless browser (Playwright) - raw HTML had {self.raw_word_count:,} words, "
                    f"rendered page {self.word_count:,}"
                )
            return f"headless browser (Playwright) - {self.render_note or 'plain request was blocked'}"
        return "standard HTTP" + (f" ({self.render_note})" if self.render_note else "")

    def to_prompt(self) -> str:
        """Render the snapshot as markdown for inclusion in a prompt."""
        if not self.ok:
            return (
                f"### {self.label}\n"
                f"- URL: {self.requested_url}\n"
                f"- LOCAL FETCH FAILED: {self.error}\n"
                + (f"- Headless browser: {self.render_note}\n" if self.render_note else "")
                + "- Try web_fetch on this URL; if that also fails, record the page as unavailable.\n"
            )
        lines = [
            f"### {self.label}",
            f"- URL: {self.requested_url} (final: {self.final_url}, HTTP {self.status_code})",
            f"- Fetch method: {self.method_summary()}",
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
        ]
        if self.js_rendered:
            lines.append(
                "- JAVASCRIPT-RENDERED: missing from the raw HTML, visible only after JavaScript runs: "
                + "; ".join(detail for _, detail in self.js_missing)
            )
        lines += [
            f"- Visible text excerpt (first {PAGE_TEXT_CHARS} chars):",
            "```",
            self.text_excerpt,
            "```",
        ]
        return "\n".join(lines) + "\n"


def short_error(e: Exception) -> str:
    """Condense verbose requests/urllib3 errors to their root cause for logs and prompts."""
    msg = str(e)
    cause = re.search(r"Caused by \w+\((.*)\)\)?$", msg)
    if cause:
        msg = cause.group(1)
    return f"{type(e).__name__}: {msg[:200]}"


HTTP = requests.Session()
HTTP.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})


def fetch_page(url: str, label: str) -> PageSnapshot:
    """Fetch and snapshot a page, falling back to a headless browser for thin or blocked pages.

    Tries a plain HTTP request first. If that returns fewer than JS_RENDER_MIN_WORDS words (or is
    blocked by bot protection), the page is rendered in headless Chromium and the richer version is
    kept. snap.fetch_method records which one was used, and snap.js_missing lists key content that
    only appeared after JavaScript ran. Never raises: failures are recorded on the snapshot.
    """
    snap = _fetch_standard(url, label)
    thin = snap.ok and snap.word_count < JS_RENDER_MIN_WORDS
    blocked = not snap.ok and snap.status_code in BROWSER_RETRY_STATUSES
    if not (thin or blocked):
        return snap

    rendered = render_with_browser(url, label)
    if not rendered.ok:
        snap.render_note = f"headless browser retry failed: {rendered.error}"
        return snap
    rendered.fetch_method = "playwright"
    if blocked:
        rendered.render_note = f"plain request was blocked ({snap.error})"
        return rendered

    rendered.raw_word_count = snap.word_count
    rendered.js_missing = detect_js_only_content(snap, rendered)
    if rendered.js_missing or rendered.word_count >= snap.word_count + 100:
        return rendered
    snap.render_note = f"headless browser retry found no extra content ({rendered.word_count:,} words)"
    return snap


def _fetch_standard(url: str, label: str) -> PageSnapshot:
    """Plain HTTP fetch (no JavaScript)."""
    snap = PageSnapshot(label=label, requested_url=url)
    try:
        resp = HTTP.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        snap.error = short_error(e)
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
    snap.text = text
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


_BROWSER_LOCK = threading.Lock()  # one Chromium at a time keeps memory predictable on small hosts
_browser_unavailable = ""  # set once if Playwright/Chromium can't start, so we stop retrying


def render_with_browser(url: str, label: str) -> PageSnapshot:
    """Load a page in headless Chromium (Playwright) and snapshot the rendered DOM. Never raises."""
    global _browser_unavailable
    snap = PageSnapshot(label=label, requested_url=url)
    if _browser_unavailable:
        snap.error = _browser_unavailable
        return snap
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        _browser_unavailable = snap.error = "Playwright is not installed (pip install playwright)"
        return snap

    launch_args: dict = {"headless": True}
    if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):  # use a system/preinstalled Chromium
        launch_args["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
    with _BROWSER_LOCK:
        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(**launch_args)
                except PlaywrightError as e:
                    first_line = str(e).strip().splitlines()[0][:160]
                    _browser_unavailable = snap.error = (
                        f"Chromium could not start ({first_line}); run `playwright install chromium`"
                    )
                    return snap
                try:
                    page = browser.new_page(user_agent=USER_AGENT, locale="en-US")
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8_000)
                    except PlaywrightError:
                        pass  # pages with analytics or chat widgets may never go idle; use what loaded
                    snap.status_code = resp.status if resp else None
                    snap.final_url = page.url
                    if snap.status_code and snap.status_code >= 400:
                        snap.error = f"HTTP {snap.status_code}"
                        return snap
                    _parse_html(page.content(), page.url, snap)
                    snap.ok = True
                finally:
                    browser.close()
        except Exception as e:  # navigation timeouts, crashes - never stop the audit
            snap.error = f"{type(e).__name__}: {str(e).strip().splitlines()[0][:200]}"
    return snap


def _cta_text(entry: str) -> str:
    return entry.split(" -> ")[0].replace(" [button]", "").strip()


def detect_js_only_content(raw: PageSnapshot, rendered: PageSnapshot) -> list[tuple[str, str]]:
    """Key content present after rendering but missing from the raw HTML, as (short, detailed) pairs."""
    missing: list[tuple[str, str]] = []

    raw_prices = len(PRICE_PATTERN.findall(raw.text))
    rendered_prices = PRICE_PATTERN.findall(rendered.text)
    if len(rendered_prices) >= 2 and raw_prices * 2 < len(rendered_prices):
        # Prefer currency amounts as examples ("$349") over phrases like "per month".
        amounts = sorted(dict.fromkeys(p.strip() for p in rendered_prices), key=lambda t: t[0] not in "$€£")
        missing.append(("pricing", f"pricing ({len(rendered_prices)} price mentions, e.g. {', '.join(amounts[:3])})"))

    raw_ctas = {_cta_text(c).lower() for c in raw.ctas}
    new_ctas = [_cta_text(c) for c in rendered.ctas if _cta_text(c).lower() not in raw_ctas]
    if new_ctas:
        quoted = ", ".join(f"'{t}'" for t in new_ctas[:3])
        missing.append(("CTAs", f"calls to action such as {quoted}"))

    if rendered.h1 and not raw.h1:
        missing.append(("the main headline", f"the main headline ('{rendered.h1[0][:80]}')"))
    elif len(rendered.h2) >= len(raw.h2) + 3:
        missing.append(("section headings", f"{len(rendered.h2) - len(raw.h2)} section headings"))

    if rendered.forms and not raw.forms:
        missing.append(("forms", "the page's forms (e.g. signup or contact fields)"))

    new_schema = [t for t in rendered.schema_types if t not in raw.schema_types]
    if new_schema:
        missing.append(("structured data", f"structured data ({', '.join(new_schema)})"))

    if rendered.word_count >= max(2 * raw.word_count, raw.word_count + 150):
        missing.append(
            ("body copy", f"most of the body copy ({raw.word_count:,} of {rendered.word_count:,} words are in the raw HTML)")
        )
    return missing


def _join(items: list[str]) -> str:
    items = list(dict.fromkeys(items))
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def build_js_findings(ctx: "AuditContext") -> dict[int, dict[str, str]]:
    """Turn JavaScript-rendered target pages into Layer 1 (CRO) and Layer 3 (SEO/AEO) observations.

    Returns {layer number: {"observed", "why", "hypothesis", "notes"}} or {} when nothing was flagged.
    """
    flagged = [s for s in ctx.pages.values() if s.js_rendered]
    if not flagged:
        return {}
    shorts = _join([short for s in flagged for short, _ in s.js_missing])
    details = "; ".join(f"{_join([d for _, d in s.js_missing])} on the {s.label.lower()}" for s in flagged)
    evidence = "; ".join(
        f"{s.label}: {s.raw_word_count:,} words in raw HTML vs {s.word_count:,} after rendering ({s.final_url})"
        for s in flagged
    )
    has_pricing = any(short == "pricing" for s in flagged for short, _ in s.js_missing)
    lead = "Pricing and other key content is" if has_pricing else "Key content is"
    page_names = _join([s.label.lower() for s in flagged])

    l1_observed = (
        f"{lead} client-side injected via JavaScript. Visitors on slow connections or with JS disabled "
        f"cannot see {details}."
    )
    l1_why = "This creates conversion risk on first load."
    l3_observed = f"Key page content including {shorts} is JavaScript-rendered and not present in raw HTML."
    l3_why = (
        "This means AI crawlers, search engine bots, and tools like ChatGPT and Perplexity cannot reliably "
        "index this content. Any query asking about pricing or features will return outdated or "
        "third-party sourced figures rather than the live page content."
    )
    footer = f"_Automated check - detected by comparing a plain HTTP fetch with a headless-browser render. {evidence}._"
    return {
        1: {
            "observed": l1_observed,
            "why": l1_why,
            "hypothesis": (
                f"Server-render (or pre-render) {shorts} on the {page_names} so it is in the first HTML response; "
                "measure first-load bounce rate and pricing-to-signup conversion on mobile/slow-connection sessions before vs. after."
            ),
            "notes": f"## Automated check: JavaScript-rendered content (CRO)\n\n{l1_observed} {l1_why}\n\n{footer}",
        },
        3: {
            "observed": l3_observed,
            "why": l3_why,
            "hypothesis": (
                f"Ship {shorts} in the raw HTML (SSR/static rendering) and mirror key figures in llms.txt; re-run the "
                "same AI answer-engine probes and track whether answers quote the live figures and cite the company's own URLs."
            ),
            "notes": f"## Automated check: JavaScript-rendered content (SEO/AEO)\n\n{l3_observed} {l3_why}\n\n{footer}",
        },
    }


def inject_js_findings(body: str, findings: dict[int, dict[str, str]]) -> str:
    """Insert the automated JS findings as the first observation under Layers 1 and 3 in section 3."""
    if not findings:
        return body
    lines = body.splitlines()
    leftovers = []
    for layer_no, f in sorted(findings.items()):
        bullet = f"- **Observed:** {f['observed']} → **Why it matters:** {f['why']} → **Hypothesis to test:** {f['hypothesis']}"
        sec_start = next((i for i, ln in enumerate(lines) if re.match(r"^##\s+3\.|^##\s+.*Channel-by-Channel", ln)), None)
        if sec_start is None:
            leftovers.append((layer_no, bullet))
            continue
        sec_end = next((i for i in range(sec_start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        head = next(
            (i for i in range(sec_start + 1, sec_end) if re.match(rf"^###\s+.*Layer\s*{layer_no}\b", lines[i])), None
        )
        if head is None:
            leftovers.append((layer_no, bullet))
            continue
        pos = head + 1
        if pos < len(lines) and not lines[pos].strip():
            pos += 1
        lines.insert(pos, bullet)
    body = "\n".join(lines)
    if leftovers:
        body = body.rstrip() + "\n\n## Automated Findings\n"
        for layer_no, bullet in leftovers:
            body += f"\n### {LAYER_TITLES[layer_no - 1]}\n\n{bullet}\n"
    return body + "\n"


def fetch_methods_note(snaps: list[PageSnapshot]) -> str:
    """One markdown line per page saying how it was fetched, for the end of a layer's notes."""
    rows = [
        f"- {s.label}: {s.method_summary() if s.ok else 'not retrieved locally (' + (s.error or 'error') + ')'}"
        for s in snaps
    ]
    return "**Page fetch methods (local snapshots):**\n" + "\n".join(rows)


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
            via = " (rendered in headless browser)" if snap.fetch_method == "playwright" else ""
            progress.info(f"{label}: {url}{via}")
            return snap
        failures.append((url, snap.error))

    progress.warn(f"{label}: not found - tried {len(failures)} URL(s)")
    missing = PageSnapshot(label=label, requested_url=candidates[0] if candidates else base)
    tried = ", ".join(u for u, _ in failures)
    last_error = failures[-1][1] if failures else "no candidate URLs"
    missing.error = f"No reachable page found. Tried: {tried} (last error: {last_error})"
    return missing


def check_llms_txt(base: str) -> str:
    """Check /llms.txt at the root domain and return a short human-readable result."""
    url = f"{base}/llms.txt"
    try:
        resp = HTTP.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return (
            f"{url}: LOCAL CHECK FAILED ({short_error(e)}). "
            "Use web_fetch on this URL to determine whether llms.txt is present."
        )
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


# Content block types that mark a server-side tool call or its result.
TOOL_BLOCK_TYPES = {"server_tool_use", "web_search_tool_result", "web_fetch_tool_result"}


class Researcher:
    """Thin wrapper around the Messages API with web tools, pause_turn resumption and fallbacks."""

    def __init__(self, model: str, on_activity=None):
        self.client = anthropic.Anthropic()
        self.model = model
        self.use_fallback = True
        self.sources: dict[str, str] = {}  # url -> title, gathered from citations and search results
        # Optional callback(str) told about each search, fetch and writing phase as it streams
        # (used by the web UI's live log; the CLI leaves it unset).
        self.on_activity = on_activity

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

            # Keep only the answer: text written after the last tool call. Earlier text blocks are
            # running commentary between searches ("Now I'll fetch the pricing page...").
            tool_positions = [i for i, b in enumerate(response.content) if b.type in TOOL_BLOCK_TYPES]
            if tool_positions:
                texts = []
            start = tool_positions[-1] + 1 if tool_positions else 0
            texts.extend(b.text for b in response.content[start:] if b.type == "text")

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
                if self.on_activity:
                    self._report_activity(stream)
                return stream.get_final_message()
        except anthropic.BadRequestError as e:
            # Some accounts/models don't accept the fallback beta; disable it and retry once.
            if self.use_fallback and "fallback" in str(e).lower():
                self.use_fallback = False
                return self._stream(messages, tools, max_tokens)
            raise

    def _report_activity(self, stream) -> None:
        """Consume stream events, describing each completed tool call to on_activity."""
        writing = False
        for event in stream:
            if event.type == "content_block_start" and event.content_block.type == "text" and not writing:
                writing = True
                self.on_activity("Analyzing results and writing findings")
            if event.type != "content_block_stop":
                continue
            block = event.content_block
            if block.type == "server_tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                if block.name == "web_search":
                    self.on_activity(f'Searching the web: "{args.get("query", "")}"')
                elif block.name == "web_fetch":
                    self.on_activity(f"Fetching {args.get('url', 'page')}")
                writing = False
            elif block.type == "web_search_tool_result":
                if isinstance(block.content, list):
                    self.on_activity(f"  {len(block.content)} results returned")
                else:
                    self.on_activity(f"  search failed ({getattr(block.content, 'error_code', 'error')})")
            elif block.type == "web_fetch_tool_result":
                content = block.content
                if getattr(content, "type", "") == "web_fetch_tool_result_error":
                    self.on_activity(f"  fetch failed ({getattr(content, 'error_code', 'error')})")
                else:
                    self.on_activity("  page retrieved")

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


class OutOfCreditsError(Exception):
    """The API account has no credit left; every further call would fail, so stop and checkpoint."""


def is_out_of_credits(e: Exception) -> bool:
    return "credit balance" in str(e).lower()


def safe_layer(name: str, progress: Progress, fn) -> tuple[str, bool]:
    """Run a layer and return (notes, succeeded). Failures are recorded and the audit keeps going."""
    try:
        with progress.spin(f"Researching {name}"):
            return fn(), True
    except anthropic.AuthenticationError:
        raise  # a bad key will fail every layer - stop immediately with a clear message
    except anthropic.APIStatusError as e:
        if is_out_of_credits(e):
            raise OutOfCreditsError(str(e)) from e
        msg = f"API error {e.status_code}: {e.message}"
    except anthropic.APIConnectionError as e:
        msg = f"Connection error: {e}"
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
    progress.warn(f"{name} could not be completed ({msg}). Continuing.")
    return f"**{name} could not be completed.** Reason: {msg}", False


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
    js_findings: dict[int, dict[str, str]] = field(default_factory=dict)

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
        f"Do NOT include the target company's brand name ({company_name(ctx.url)}) in any query: these "
        "must be unbranded searches from prospects who have not yet chosen a provider. Naming a "
        "competitor the ICP is switching away from is fine.\n"
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
           if s.ok else f"not checked locally ({s.error}) - use web_fetch on this URL and look for FAQPage JSON-LD")
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
        "3. Interpret the llms.txt and FAQ schema checks below (performed programmatically). Where a "
        "check says it failed locally, verify it yourself with web_fetch before drawing a conclusion.\n\n"
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
        "distinctions; write for a growth lead who will act on this. Sections headed 'Automated check: "
        "JavaScript-rendered content' are inserted into section 3 by the tool itself - do not restate them "
        "as observations (you may still weigh them in the Priority Stack).\n\n"
        f"# Raw research notes\n\n{joined}",
        tools=None,
    )


def build_report(
    ctx: AuditContext,
    body: str,
    notes: dict[str, str],
    sources: dict[str, str],
    analyst_notes: dict[str, str] | None = None,
) -> str:
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

    # Notes the analyst added by hand (web UI), kept separate from the AI research.
    filled = {t: n.strip() for t, n in (analyst_notes or {}).items() if n and n.strip()}
    if filled:
        body = body.rstrip() + "\n\n## 6. Analyst Field Notes\n\n_Live observations added by the analyst, not AI-generated._\n"
        for title in LAYER_TITLES:
            if title in filled:
                body += f"\n### {title}\n\n{filled[title]}\n"

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
    p.add_argument(
        "--resume",
        action="store_true",
        help="Reuse keywords and completed layers from the last interrupted run (same URL and ICP)",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Checkpointing - completed layers are saved as they finish so an interrupted run
# (out of credits, network loss, Ctrl-C) can be resumed without paying for them again.
# ---------------------------------------------------------------------------


def checkpoint_path(ctx: AuditContext) -> str:
    return f"{ctx.company}-growth-audit.checkpoint.json"


def save_checkpoint(ctx: AuditContext, completed: dict[str, str], sources: dict[str, str]) -> None:
    data = {
        "url": ctx.url,
        "icp": ctx.icp,
        "competitors": ctx.competitors,
        "keywords": ctx.keywords,
        "completed": completed,
        "sources": sources,
    }
    with open(checkpoint_path(ctx), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_checkpoint(ctx: AuditContext, progress: Progress) -> dict:
    path = checkpoint_path(ctx)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        progress.warn(f"--resume: no checkpoint found at {path}; running the full audit")
        return {}
    except (OSError, json.JSONDecodeError) as e:
        progress.warn(f"--resume: could not read {path} ({e}); running the full audit")
        return {}
    if data.get("url") != ctx.url or data.get("icp") != ctx.icp:
        progress.warn("--resume: checkpoint was made for a different URL or ICP; running the full audit")
        return {}
    if data.get("competitors") != ctx.competitors and LAYER_TITLES[4] in data.get("completed", {}):
        progress.warn("--resume: competitors changed; Layer 5 will be re-run")
        del data["completed"][LAYER_TITLES[4]]
    progress.info(f"Resuming from {path}: {len(data.get('completed', {}))} layer(s) already complete")
    return data


def fetch_all_pages(ctx: AuditContext, progress: Progress) -> None:
    """Fetch target and competitor pages locally, logging every outcome."""
    home = fetch_page(ctx.url, "Homepage")
    if home.ok:
        via = " (rendered in headless browser)" if home.fetch_method == "playwright" else ""
        progress.info(f"Homepage: {ctx.url}{via}")
    else:
        progress.warn(f"Homepage fetch failed ({home.error}) - Claude will try web_fetch instead")
    ctx.pages["home"] = home
    for key in PAGE_TYPES:
        ctx.pages[key] = discover_page(home, ctx.url, key, progress)

    ctx.llms_txt = check_llms_txt(ctx.url)
    llms_status = ctx.llms_txt.splitlines()[0].split(": ", 1)[-1]
    if llms_status.startswith("LOCAL CHECK FAILED"):
        llms_status = "local check failed - Claude will verify with web_fetch"
    progress.info("llms.txt: " + llms_status)

    for comp in ctx.competitors:
        chome = fetch_page(comp, f"{bare_domain(comp)} homepage")
        progress.info(f"Competitor {bare_domain(comp)} homepage: {'ok' if chome.ok else chome.error}")
        cpricing = discover_page(chome, comp, "pricing", progress, label=f"{bare_domain(comp)} pricing page")
        ctx.competitor_pages[comp] = {"home": chome, "pricing": cpricing}

    all_snaps = list(ctx.pages.values()) + [s for d in ctx.competitor_pages.values() for s in d.values()]
    for s in all_snaps:
        ctx.fetch_log.append(
            f"{s.label}: {s.final_url or s.requested_url} - OK (HTTP {s.status_code}, {s.method_summary()})" if s.ok
            else f"{s.label}: {s.requested_url} - FAILED ({s.error})"
            + (f" [headless browser: {s.render_note}]" if not s.ok and s.render_note else "")
        )
    if _browser_unavailable:
        progress.warn(f"Headless browser fallback unavailable: {_browser_unavailable}")

    ctx.js_findings = build_js_findings(ctx)
    if ctx.js_findings:
        pages = _join([s.label for s in ctx.pages.values() if s.js_rendered])
        progress.warn(f"JavaScript-rendered content on {pages} - flagged as a CRO (L1) and SEO/AEO (L3) finding")


def layer_page_snapshots(ctx: AuditContext, index: int) -> list[PageSnapshot]:
    """The local page snapshots each layer's prompt was built from."""
    target = list(ctx.pages.values())
    if index in (1, 3):
        return target
    if index == 2:
        return [ctx.pages["home"]] if "home" in ctx.pages else []
    if index == 4:
        return [ctx.pages["signup"]] if "signup" in ctx.pages else []
    return [ctx.pages[k] for k in ("home", "pricing") if k in ctx.pages] + [
        s for d in ctx.competitor_pages.values() for s in d.values()
    ]


def annotate_layer_notes(ctx: AuditContext, index: int, notes: str) -> str:
    """Append the automated JS finding (Layers 1 and 3) and the page fetch methods to a layer's notes."""
    extra = []
    if index in ctx.js_findings:
        extra.append(ctx.js_findings[index]["notes"])
    snaps = layer_page_snapshots(ctx, index)
    if snaps:
        extra.append(fetch_methods_note(snaps))
    return notes.rstrip() + "\n\n" + "\n\n".join(extra) if extra else notes


@dataclass
class AuditOutcome:
    """What run_audit() produced. status: complete | partial | out_of_credits."""

    body: str
    notes: dict[str, str]
    status: str
    message: str = ""
    failed_layers: list[str] = field(default_factory=list)


LAYER_FUNCTIONS = [
    layer1_web_conversion,
    layer2_paid,
    layer3_organic_aeo,
    layer4_lifecycle,
    layer5_competitive,
]


def run_audit(
    ctx: AuditContext,
    researcher: Researcher,
    progress: Progress,
    keywords: str = "",
    resume: bool = False,
) -> AuditOutcome:
    """Run the full audit pipeline. Used by both the CLI (main) and the web UI (app.py).

    Raises anthropic.AuthenticationError (bad key) and KeyboardInterrupt; everything else is
    captured in the returned AuditOutcome so the caller can always write a report.
    """
    checkpoint = load_checkpoint(ctx, progress) if resume else {}
    completed: dict[str, str] = dict(checkpoint.get("completed", {}))
    researcher.sources.update(checkpoint.get("sources", {}))

    notes: dict[str, str] = {title: "_Not run._" for title in LAYER_TITLES}
    body = "_Synthesis did not run; see the raw layer notes in Appendix B._\n"

    try:
        # Step 1: gather page snapshots locally (fast, no API cost).
        progress.header("Fetching pages (target + competitors)")
        fetch_all_pages(ctx, progress)

        # Step 2: choose keywords for Layers 2 and 3.
        progress.header("Choosing high-intent keywords")
        if keywords:
            ctx.keywords = [k.strip() for k in keywords.split(",") if k.strip()]
        elif checkpoint.get("keywords"):
            ctx.keywords = checkpoint["keywords"]
            progress.info("Reusing keywords from checkpoint")
        else:
            with progress.spin("Generating ICP-specific keywords"):
                ctx.keywords = generate_keywords(researcher, ctx)
        for k in ctx.keywords:
            progress.info(k)
        progress.keywords_chosen(ctx.keywords)

        # Steps 3-7: the five layers. Each failure is captured and the audit continues;
        # each success is checkpointed so it never has to be paid for twice.
        failed: list[str] = []
        for index, (title, fn) in enumerate(zip(LAYER_TITLES, LAYER_FUNCTIONS), 1):
            progress.header(title)
            progress.layer_started(index, title)
            if title in completed:
                progress.info("Already complete - reusing notes from checkpoint")
                notes[title] = completed[title]
                progress.layer_finished(index, title, notes[title], True)
                continue
            notes[title], ok = safe_layer(title, progress, lambda fn=fn: fn(researcher, ctx))
            if ok:
                notes[title] = annotate_layer_notes(ctx, index, notes[title])
            progress.layer_finished(index, title, notes[title], ok)
            if ok:
                completed[title] = notes[title]
                save_checkpoint(ctx, completed, researcher.sources)
            else:
                failed.append(title)

        # Step 8: synthesize into the final report.
        progress.header("Writing the audit report")
        try:
            with progress.spin("Synthesizing findings"):
                body = synthesize(researcher, ctx, notes)
            body = inject_js_findings(body, ctx.js_findings)
        except anthropic.AuthenticationError:
            raise
        except Exception as e:
            if is_out_of_credits(e):
                raise OutOfCreditsError(str(e)) from e
            progress.warn(f"Synthesis failed ({type(e).__name__}: {e}); saving raw layer notes instead")
            body = inject_js_findings("_Synthesis failed; see the raw layer notes in Appendix B._\n", ctx.js_findings)
            return AuditOutcome(body, notes, "partial", "The final synthesis step failed.", failed)
    except OutOfCreditsError:
        save_checkpoint(ctx, completed, researcher.sources)
        missing = [t for t in LAYER_TITLES if t not in completed]
        return AuditOutcome(
            body,
            notes,
            "out_of_credits",
            "your Anthropic account is out of API credit. Completed layers are saved in "
            f"{checkpoint_path(ctx)}. Add credit in the Claude Console (Plans & Billing), then re-run "
            "the same command with --resume to finish only what's missing.",
            missing,
        )
    except KeyboardInterrupt:
        save_checkpoint(ctx, completed, researcher.sources)
        raise

    # A clean, complete run no longer needs its checkpoint (and a stale one could be resumed by mistake).
    if not failed and os.path.exists(checkpoint_path(ctx)):
        os.remove(checkpoint_path(ctx))
    if failed:
        return AuditOutcome(body, notes, "partial", f"{len(failed)} layer(s) could not be completed.", failed)
    return AuditOutcome(body, notes, "complete")


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
        outcome = run_audit(ctx, researcher, progress, keywords=args.keywords, resume=args.resume)
    except anthropic.AuthenticationError:
        print("\nError: the Anthropic API rejected the API key. Check ANTHROPIC_API_KEY.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAudit cancelled. Progress saved; re-run with --resume to continue.", file=sys.stderr)
        return 130
    if outcome.status == "out_of_credits":
        print(f"\nStopped: {outcome.message}", file=sys.stderr)
    body, notes = outcome.body, outcome.notes
    exit_code = {"complete": 0, "out_of_credits": 2}.get(outcome.status, 1)

    filename = f"{ctx.company}-growth-audit-{ctx.date}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(build_report(ctx, body, notes, researcher.sources))

    progress.done()
    print(f"Report saved to {os.path.abspath(filename)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
