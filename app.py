#!/usr/bin/env python3
"""
app.py - web UI for growth_audit.py.

Run:
    python app.py                      # http://127.0.0.1:5000
    python app.py --load helcim-growth-audit-2026-09-24.md   # also open a saved report in the UI

Three views:
    /                   input form (company URL, ICP, competitors)
    /audit/<id>         live progress (Server-Sent Events) that turns into the results view
    /audit/<id>/report.md   the full markdown report, including the analyst's field notes

The audit itself is growth_audit.run_audit(), executed in a background thread. A Progress subclass
turns its callbacks into events; each audit keeps its event history in memory so any number of
browser tabs can connect (or reconnect) and replay the stream from the start.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

import anthropic
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)

import growth_audit as ga

load_dotenv()

app = Flask(__name__)

# Short names used in the UI for the five layers (same order as ga.LAYER_TITLES).
LAYER_SHORT = ["Web Conversion", "Paid Acquisition", "Organic & AEO", "Lifecycle", "Competitive"]


# ---------------------------------------------------------------------------
# In-memory audit state
# ---------------------------------------------------------------------------


@dataclass
class AuditJob:
    """Everything the UI needs about one audit. Lives in memory for the life of the server."""

    id: str
    url: str
    icp: str
    competitors: list[str]
    company: str
    date: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    status: str = "running"  # running | complete | partial | out_of_credits | error
    message: str = ""
    keywords: list[str] = field(default_factory=list)
    layers: list[dict] = field(default_factory=list)
    body: str = ""
    notes: dict[str, str] = field(default_factory=dict)
    analyst_notes: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    fetch_log: list[str] = field(default_factory=list)
    report_path: str = ""
    events: list[dict] = field(default_factory=list)
    cond: threading.Condition = field(default_factory=threading.Condition)

    def __post_init__(self) -> None:
        if not self.layers:
            self.layers = [
                {"index": i, "title": t, "short": LAYER_SHORT[i - 1], "status": "pending", "summary": "", "notes": ""}
                for i, t in enumerate(ga.LAYER_TITLES, 1)
            ]

    @property
    def done(self) -> bool:
        return self.status != "running"

    def emit(self, kind: str, **data) -> None:
        """Record an event and wake every SSE stream waiting on this audit."""
        with self.cond:
            data.update(type=kind, seq=len(self.events), t=round(time.time() - self.started, 1))
            self.events.append(data)
            self.cond.notify_all()

    def context(self) -> ga.AuditContext:
        ctx = ga.AuditContext(
            url=self.url, icp=self.icp, competitors=self.competitors, company=self.company, date=self.date
        )
        ctx.keywords = self.keywords
        ctx.fetch_log = self.fetch_log
        return ctx

    def markdown(self) -> str:
        """The full report: same format as the CLI output, plus the analyst's field notes."""
        notes = {t: self.notes.get(t, "_Not run._") for t in ga.LAYER_TITLES}
        analyst = {ga.LAYER_TITLES[int(k) - 1]: v for k, v in self.analyst_notes.items()}
        return ga.build_report(self.context(), self.body, notes, self.sources, analyst)


AUDITS: dict[str, AuditJob] = {}
AUDITS_LOCK = threading.Lock()


def get_job(audit_id: str) -> AuditJob:
    job = AUDITS.get(audit_id)
    if not job:
        abort(404)
    return job


# ---------------------------------------------------------------------------
# Bridging growth_audit's progress callbacks to UI events
# ---------------------------------------------------------------------------


class WebProgress(ga.Progress):
    """Progress reporter that emits structured events instead of (only) printing."""

    def __init__(self, job: AuditJob):
        super().__init__(total_steps=8)
        self.job = job

    def header(self, title: str) -> None:
        self.step += 1
        phase = "prep" if self.step <= 2 else "report" if self.step == 8 else "layer"
        self.job.emit("step", step=self.step, total=self.total, title=title, phase=phase)
        self.job.emit("log", level="step", text=title)

    def info(self, msg: str) -> None:
        self.job.emit("log", level="info", text=msg)

    def warn(self, msg: str) -> None:
        self.job.emit("log", level="warn", text=msg)

    def spin(self, label: str) -> "_WebSpinner":
        return _WebSpinner(self.job, label)

    def keywords_chosen(self, keywords: list[str]) -> None:
        self.job.keywords = list(keywords)
        self.job.emit("keywords", keywords=keywords)

    def layer_started(self, index: int, title: str) -> None:
        self.job.layers[index - 1]["status"] = "running"
        self.job.emit("layer", index=index, title=title, status="running")

    def layer_finished(self, index: int, title: str, notes: str, ok: bool) -> None:
        layer = self.job.layers[index - 1]
        layer.update(status="done" if ok else "failed", notes=notes, summary=extract_summary(notes))
        self.job.notes[title] = notes
        self.job.emit("layer", index=index, title=title, status=layer["status"], summary=layer["summary"])

    def done(self) -> None:
        pass


class _WebSpinner:
    def __init__(self, job: AuditJob, label: str):
        self.job, self.label = job, label

    def __enter__(self) -> "_WebSpinner":
        self.start = time.time()
        self.job.emit("log", level="start", text=self.label)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        secs = int(time.time() - self.start)
        if exc_type:
            self.job.emit("log", level="error", text=f"{self.label} failed after {secs}s")
        else:
            self.job.emit("log", level="done", text=f"{self.label} done in {secs}s")


def run_job(job: AuditJob, model: str) -> None:
    """Background thread: run the audit and publish the result."""
    ctx = ga.AuditContext(url=job.url, icp=job.icp, competitors=job.competitors, company=job.company, date=job.date)
    researcher = ga.Researcher(model, on_activity=lambda text: job.emit("log", level="activity", text=text))
    progress = WebProgress(job)
    job.emit("log", level="info", text=f"Audit started for {job.url} with model {model}")
    try:
        outcome = ga.run_audit(ctx, researcher, progress)
        job.body = outcome.body
        job.notes = outcome.notes
        job.sources = dict(researcher.sources)
        job.fetch_log = ctx.fetch_log
        job.keywords = ctx.keywords
        job.message = outcome.message
        # Save the report to disk as well, same as the CLI, so nothing is lost if the server stops.
        job.report_path = os.path.abspath(f"{job.company}-growth-audit-{job.date}.md")
        with open(job.report_path, "w", encoding="utf-8") as f:
            f.write(job.markdown())
        job.status = outcome.status
    except anthropic.AuthenticationError:
        job.status, job.message = "error", "The Anthropic API rejected the API key. Check ANTHROPIC_API_KEY in .env."
    except Exception as e:  # never leave the UI spinning forever
        job.status, job.message = "error", f"{type(e).__name__}: {e}"
    job.finished = time.time()
    level = "error" if job.status == "error" else "done"
    job.emit("log", level=level, text=job.message or "Audit complete")
    job.emit("complete", status=job.status, message=job.message)


# ---------------------------------------------------------------------------
# Parsing the report into structured pieces for the results view
# ---------------------------------------------------------------------------


def split_sections(md: str, level: int) -> list[tuple[str, str]]:
    """Split markdown into (heading, content) pairs at the given heading level."""
    marker = "#" * level + " "
    parts: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in md.splitlines():
        if line.startswith(marker):
            if heading or "".join(buf).strip():
                parts.append((heading, "\n".join(buf).strip()))
            heading, buf = line[len(marker):].strip(), []
        else:
            buf.append(line)
    parts.append((heading, "\n".join(buf).strip()))
    return parts


def split_bullets(md: str) -> list[str]:
    """Top-level '- ' bullets, with continuation lines joined to their bullet."""
    bullets: list[str] = []
    for line in md.splitlines():
        if line.startswith(("- ", "* ")):
            bullets.append(line[2:].strip())
        elif bullets and line.strip():
            bullets[-1] += "\n" + line.strip()
    return bullets


def layer_number(text: str, fallback: int) -> int:
    m = re.search(r"Layer\s*(\d)", text)
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 5 else fallback


OBSERVATION_RE = re.compile(
    r"\*\*Observed:?\*\*:?\s*(?P<observed>.*?)\s*(?:→|->)\s*\*\*Why it matters:?\*\*:?\s*(?P<why>.*?)"
    r"\s*(?:→|->)\s*\*\*Hypothesis to test:?\*\*:?\s*(?P<hypothesis>.*)",
    re.DOTALL,
)
SCORE_RE = re.compile(
    r"Impact:?\**\s*:?\s*(?P<impact>High|Med(?:ium)?|Low).*?Confidence:?\**\s*:?\s*(?P<confidence>High|Med(?:ium)?|Low)"
    r".*?Speed:?\**\s*:?\s*(?P<speed>High|Med(?:ium)?|Low)",
    re.IGNORECASE,
)


def parse_body(body: str) -> dict:
    """Pull sections 2-5 of the synthesized report into structures the results view can render."""
    out = {"working": {}, "observations": {}, "priorities": [], "data": [], "unparsed": ""}
    sections = {h: c for h, c in split_sections(body, 2)}

    def find(*needles: str) -> str:
        for heading, content in sections.items():
            if any(n in heading.lower() for n in needles):
                return content
        return ""

    for i, bullet in enumerate(split_bullets(find("what's working", "what’s working", "working")), 1):
        n = layer_number(bullet, i)
        text = re.sub(r"^\*\*Layer\s*\d[^*]*\*\*\s*[:\-—]?\s*", "", bullet)
        out["working"][n] = text

    for i, (heading, content) in enumerate(split_sections(find("observation"), 3)):
        if not heading:
            continue
        n = layer_number(heading, i)
        items = []
        for bullet in split_bullets(content):
            m = OBSERVATION_RE.search(bullet)
            items.append({k: v.strip() for k, v in m.groupdict().items()} if m else {"raw": bullet})
        if not items and content:
            items.append({"raw": content})
        out["observations"][n] = items

    for heading, content in split_sections(find("priority"), 3):
        if not heading:
            continue
        m = SCORE_RE.search(content)
        rationale = content
        scores = {"impact": "", "confidence": "", "speed": ""}
        if m:
            scores = {k: {"h": "High", "m": "Med", "l": "Low"}[v[0].lower()] for k, v in m.groupdict().items()}
            # Drop the score line itself from the rationale.
            lines = content.splitlines()
            rationale = "\n".join(line for line in lines if not SCORE_RE.search(line)).strip()
        out["priorities"].append(
            {"title": re.sub(r"^\d+[.)]\s*", "", heading), "rationale": rationale, **scores}
        )

    out["data"] = split_bullets(find("data i", "data i’d", "before committing"))
    if not (out["working"] or out["observations"] or out["priorities"]):
        out["unparsed"] = body
    return out


def clean_notes(notes: str) -> str:
    """Drop tool-call narration that reports saved by older versions glued before the first heading."""
    m = re.search(r"(?:^|(?<=[.!?:)]))(#{1,3} )", notes)
    if m and m.start(1) > 0 and "\n" not in notes[: m.start(1)].strip():
        return notes[m.start(1):]
    return notes


def extract_summary(notes: str) -> str:
    """The layer's own summary for its card: a 'Summary' section, else its 'strength' section."""
    lines = notes.splitlines()
    for pattern in (r"summary|key findings|takeaways", r"strength"):
        found = _section_after_heading(lines, pattern)
        if found:
            return found
    text = re.sub(r"^#+\s.*$", "", notes, flags=re.MULTILINE).strip()
    return text[:700] + ("…" if len(text) > 700 else "")


def _section_after_heading(lines: list[str], pattern: str) -> str:
    """Content under the last heading matching pattern, up to the next heading of the same level."""
    for i in range(len(lines) - 1, -1, -1):
        m = re.match(rf"^(#{{1,4}})\s+.*(?:{pattern})", lines[i], re.IGNORECASE)
        if m:
            level = len(m.group(1))
            out = []
            for line in lines[i + 1:]:
                h = re.match(r"^(#{1,4})\s", line)
                if h and len(h.group(1)) <= level:
                    break
                out.append(line)
            summary = "\n".join(out).strip()
            if summary:
                return summary
    return ""


def result_payload(job: AuditJob) -> dict:
    parsed = parse_body(job.body)
    layers = []
    for layer in job.layers:
        i = layer["index"]
        layers.append(
            {
                "index": i,
                "title": layer["title"],
                "short": layer["short"],
                "status": layer["status"],
                "summary": layer["summary"],
                "notes": job.notes.get(layer["title"], ""),
                "working": parsed["working"].get(i, ""),
                "observations": parsed["observations"].get(i, []),
                "analyst_notes": job.analyst_notes.get(str(i), ""),
            }
        )
    duration = int((job.finished or time.time()) - job.started)
    return {
        "id": job.id,
        "status": job.status,
        "message": job.message,
        "company": job.company,
        "url": job.url,
        "icp": job.icp,
        "competitors": job.competitors,
        "date": job.date,
        "keywords": job.keywords,
        "duration": duration,
        "priorities": parsed["priorities"],
        "data": parsed["data"],
        "unparsed": parsed["unparsed"],
        "layers": layers,
        "sources": len(job.sources),
        "fetch_log": job.fetch_log,
        "report_path": job.report_path,
    }


# ---------------------------------------------------------------------------
# Loading a saved report (python app.py --load file.md) - handy for demos and re-reading past audits
# ---------------------------------------------------------------------------


def load_report(path: str) -> AuditJob:
    md = open(path, encoding="utf-8").read()

    def scope(label: str) -> str:
        m = re.search(rf"\*\*{re.escape(label)}:\*\*\s*(.*)", md)
        return m.group(1).strip() if m else ""

    url = scope("URL audited")
    competitors = [c.strip() for c in scope("Competitors compared").split(",") if c.strip() and c.strip() != "none"]
    body_start = md.find("## 2.")
    body_end = md.find("\n---\n\n## Appendix A")
    body = md[body_start:body_end] if body_start != -1 else ""
    # Field notes added in the UI are exported as section 6; load them back as analyst notes.
    analyst: dict[str, str] = {}
    if "## 6. Analyst Field Notes" in body:
        body, field_notes = body.split("## 6. Analyst Field Notes", 1)
        for heading, content in split_sections(field_notes, 3):
            if heading:
                analyst[str(layer_number(heading, 1))] = content
    notes = {
        title: clean_notes(text)
        for title, text in re.findall(r"<details>\n<summary>(.*?)</summary>\n\n(.*?)\n\n</details>", md, re.S)
    }
    sources = {u: t for t, u in re.findall(r"^- \[(.*?)\]\((https?://[^)]+)\)$", md, re.M)}
    fetch_log = re.findall(r"^- (.*)$", md[md.find("## Appendix A"):md.find("## Appendix B")], re.M)

    job = AuditJob(
        id=uuid.uuid4().hex[:10],
        url=url,
        icp=scope("ICP lens"),
        competitors=competitors,
        company=ga.company_name(url) if url else "company",
        date=scope("Date") or dt.date.today().isoformat(),
        status="complete",
        keywords=[k for k in scope("High-intent keywords used").split("; ") if k],
        body=body.strip(),
        notes=notes,
        analyst_notes=analyst,
        sources=sources,
        fetch_log=fetch_log,
        report_path=os.path.abspath(path),
    )
    job.finished = job.started
    for layer in job.layers:
        text = notes.get(layer["title"], "")
        failed = not text or "could not be completed" in text[:200] or text.strip() == "_Not run._"
        layer.update(status="failed" if failed else "done", notes=text, summary=extract_summary(text))
    job.emit("complete", status="complete", message="")
    return job


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def normalize_input_url(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    url = ga.normalize_url(raw)
    host = ga.bare_domain(url)
    return url if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host) else None


@app.get("/")
def index():
    recent = sorted(AUDITS.values(), key=lambda j: j.started, reverse=True)[:6]
    return render_template(
        "index.html",
        recent=recent,
        has_key=bool(os.environ.get("ANTHROPIC_API_KEY")),
        errors={},
        form={},
    )


@app.post("/audit")
def start_audit():
    form = {k: request.form.get(k, "").strip() for k in ("url", "icp", "competitors")}
    errors = {}
    url = normalize_input_url(form["url"])
    if not url:
        errors["url"] = "Enter a valid website, e.g. https://www.helcim.com"
    if len(form["icp"]) < 5:
        errors["icp"] = "Describe the ideal customer this audit should be read through"
    competitors = []
    for raw in filter(None, (c.strip() for c in form["competitors"].split(","))):
        comp = normalize_input_url(raw)
        if not comp:
            errors["competitors"] = f"'{raw}' doesn't look like a website"
            break
        competitors.append(comp)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        errors["form"] = "ANTHROPIC_API_KEY is not set. Add it to .env and restart the app."
    if errors:
        recent = sorted(AUDITS.values(), key=lambda j: j.started, reverse=True)[:6]
        return render_template("index.html", recent=recent, has_key=True, errors=errors, form=form), 400

    job = AuditJob(
        id=uuid.uuid4().hex[:10],
        url=url,
        icp=form["icp"],
        competitors=competitors,
        company=ga.company_name(url),
        date=dt.date.today().isoformat(),
    )
    with AUDITS_LOCK:
        AUDITS[job.id] = job
    threading.Thread(target=run_job, args=(job, app.config["MODEL"]), daemon=True).start()
    return redirect(url_for("audit_view", audit_id=job.id))


@app.get("/audit/<audit_id>")
def audit_view(audit_id: str):
    job = get_job(audit_id)
    return render_template("audit.html", job=job, layer_short=LAYER_SHORT)


@app.get("/audit/<audit_id>/events")
def audit_events(audit_id: str):
    """Server-Sent Events: replays the audit's history, then streams new events live."""
    job = get_job(audit_id)
    try:
        start = int(request.headers.get("Last-Event-ID", -1)) + 1
    except ValueError:
        start = 0

    def stream():
        idx = start
        yield "retry: 3000\n\n"
        while True:
            with job.cond:
                if idx >= len(job.events) and not job.done:
                    job.cond.wait(timeout=15)
                batch = job.events[idx:]
            if not batch:
                if job.done:
                    return
                yield ": keep-alive\n\n"  # stops proxies from closing an idle connection
                continue
            for event in batch:
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
            idx += len(batch)

    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/audit/<audit_id>")
def audit_result(audit_id: str):
    job = get_job(audit_id)
    if not job.done:
        return jsonify({"status": job.status}), 202
    return jsonify(result_payload(job))


@app.post("/api/audit/<audit_id>/notes")
def save_notes(audit_id: str):
    job = get_job(audit_id)
    data = request.get_json(silent=True) or {}
    layer = str(data.get("layer", ""))
    if layer not in {"1", "2", "3", "4", "5"}:
        abort(400)
    job.analyst_notes[layer] = str(data.get("text", ""))[:20000]
    return jsonify({"saved": True, "at": dt.datetime.now().strftime("%H:%M:%S")})


@app.get("/audit/<audit_id>/report.md")
def download_report(audit_id: str):
    job = get_job(audit_id)
    if not job.done:
        abort(409)
    headers = {}
    if not request.args.get("inline"):
        headers["Content-Disposition"] = f'attachment; filename="{job.company}-growth-audit-{job.date}.md"'
    return Response(job.markdown(), mimetype="text/markdown; charset=utf-8", headers=headers)


def main() -> None:
    p = argparse.ArgumentParser(description="Web UI for the growth audit tool.")
    # Hosting platforms such as Railway inject PORT; listen on all interfaces when they do.
    port = int(os.environ.get("PORT", 5000))
    default_host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    p.add_argument("--host", default=os.environ.get("HOST", default_host))
    p.add_argument("--port", type=int, default=port)
    p.add_argument("--model", default=app.config["MODEL"], help=f"Claude model ID (default: {app.config['MODEL']})")
    p.add_argument("--load", action="append", default=[], help="Open a saved report .md in the UI (repeatable)")
    args = p.parse_args()

    app.config["MODEL"] = args.model
    for path in args.load:
        job = load_report(path)
        AUDITS[job.id] = job
        print(f"Loaded {path} -> http://{args.host}:{args.port}/audit/{job.id}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Warning: ANTHROPIC_API_KEY is not set; new audits can't run until it is added to .env.")
    print(f"Growth Audit UI running at http://{args.host}:{args.port}")
    # threaded=True so SSE streams, note saves and the audit thread don't block each other.
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


# Used when the app is served by gunicorn (Procfile), where main() doesn't run.
app.config.setdefault("MODEL", os.environ.get("CLAUDE_MODEL", ga.DEFAULT_MODEL))

if __name__ == "__main__":
    main()
