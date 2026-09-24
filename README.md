# growth-audit-tool

A reusable tool, with a command line and a web UI, that runs a structured, five-layer growth audit on any company website through the lens of a specific Ideal Customer Profile (ICP). It compares the company against named competitors and writes the findings to a markdown report.

Research runs on the Anthropic API with the server-side **web search** and **web fetch** tools. Pages are also fetched and parsed locally, which gives Claude the page title, headings, CTAs, form fields and schema markup.

## What it audits

| Layer | What it looks at |
|---|---|
| 1. Web Conversion | Homepage, pricing, compare/calculator, contact sales and signup pages. For each page: the primary CTA and the CTAs that compete with it, how clear the message is, friction, steps to a value moment, and missing conversion elements |
| 2. Paid Acquisition | 5 high-intent keywords. For each: whether the company appears in paid results, the ad copy, how well the ad matches its landing page, and the CTA |
| 3. Organic Search and AEO | Organic rankings for the same keywords, an AI answer-engine probe ("what is the best payment processor for [ICP]?"), `/llms.txt`, and FAQ schema on key pages |
| 4. Lifecycle and Post-Signup | The part of the signup flow you can see without creating an account: steps, information required upfront, personalization, first value moment, and onboarding prompts |
| 5. Competitive Comparison | Homepage and pricing analysis for each competitor: how quickly a prospect gets to a cost estimate, the primary CTA, and one specific contrast with the target |

The report has these sections:

1. **Audit Scope**: URL, ICP, date, channels covered and keywords used
2. **What's Working**: one genuine strength per layer
3. **Channel-by-Channel Observations**: each written as *what I observed → why it matters → hypothesis to test*
4. **Priority Stack**: the top 3 opportunities, ranked by impact × confidence × speed
5. **Data I'd Want Before Committing**: 3–4 metrics or analytics questions

Three appendices follow: a page fetch log, the raw notes from each layer, and the sources consulted.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # then edit .env and paste your key
```

`.env` should contain:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Your Anthropic organization must have web search and web fetch enabled in the Claude Console.

## Web UI

```bash
python app.py
```

Then open http://127.0.0.1:5000.

![Input form](docs/screenshot-form.png)

1. **Input form:** enter the company URL, the ICP and competitor URLs, then click **Run audit**. The audit runs in a background thread, so the page stays responsive.
2. **Live progress:** progress streams to the browser over Server-Sent Events, with no page refreshes. The page shows:
   - a step indicator (prep → L1–L5 → report)
   - an overall progress bar and elapsed time
   - a live feed of each search, fetch and analysis step as it happens
   - a card for each layer, which fills in with that layer's summary when it finishes

   You can reload the page or open it in another tab. It replays everything that has happened so far and then carries on live.

   ![Live progress](docs/screenshot-progress.png)

3. **Results:**
   - The **Priority Stack** comes first: the top 3 opportunities with impact, confidence and speed shown as meters.
   - Each layer has a tab. AI research is tagged in teal: what's working, the observations (observed → why it matters → hypothesis) and the full research notes. Your own **field notes** go in the amber panel next to it. They save automatically as you type and are clearly separated from the AI findings.
   - **Copy markdown** and **Download .md** export the full report. Your field notes are added as an "Analyst Field Notes" section.

   ![Results](docs/screenshot-results.png)

Options: `--port 8080`, `--host 0.0.0.0` (to share on your network), `--model <id>`.

`--load report.md` opens a report you saved earlier in the results view without running a new audit, which is handy for demos. Any field notes in that file are loaded too.

Audit state lives in memory while the server runs. Each finished report is also written to disk with the same filename the CLI uses, so nothing is lost when you stop the server.

### Deploying to Railway

The repo includes a `Procfile`, so Railway runs the app with gunicorn:

```
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 16 --timeout 120
```

1. Create a Railway project from this GitHub repo. Railway installs `requirements.txt` and uses the Procfile.
2. Under **Variables**, add `ANTHROPIC_API_KEY`. You can also add `CLAUDE_MODEL` to change the model from the default `claude-opus-5`. Railway sets `PORT` itself.
3. Under **Settings → Networking**, generate a domain.

Why the Procfile looks like this:
- **One worker.** Audits and field notes are kept in the worker's memory. A second worker would have its own separate set of audits, and progress streams would miss events. Threads handle concurrent SSE streams and requests.
- **Data loss on restart.** A redeploy or restart clears in-memory audits and the report files written to the container's disk. Download reports you want to keep.
- **Anyone with the URL can use it.** The app has no login, so anyone who finds the URL can start audits billed to your API key. Keep the domain private, or put authentication in front of it.

`python app.py` also reads `PORT` from the environment. When `PORT` is set, it listens on `0.0.0.0`; otherwise it uses `127.0.0.1:5000`.

## Command line

```bash
python growth_audit.py \
  --url helcim.com \
  --icp '$80K/month dental practice switching from Square' \
  --competitors square.com,stripe.com
```

Put the ICP in **single quotes** if it contains a `$`. Otherwise your shell expands `$80K` into an empty string.

The report is saved in the current directory as `[company-name]-growth-audit-[date].md`, e.g. `helcim-growth-audit-2026-09-24.md`.

### Options

| Flag | Required | Description |
|---|---|---|
| `--url` | yes | The website to audit (`helcim.com`, `https://www.helcim.com` and similar forms all work) |
| `--icp` | yes | The ICP lens used for every judgment |
| `--competitors` | no | Comma-separated competitor URLs |
| `--keywords` | no | Comma-separated keywords for Layers 2–3. If you leave this out, Claude writes 5 ICP-specific keywords |
| `--model` | no | Claude model ID (default `claude-opus-5`) |
| `--resume` | no | Continue an interrupted run: reuse its keywords and completed layers, and only run what's missing |

### Resuming an interrupted run

Each layer is saved to `<company>-growth-audit.checkpoint.json` as soon as it finishes. If a run stops partway, re-run the same command with `--resume` to finish only the missing layers and the final report. A run can stop because the API account runs out of credit, the network drops, or you press Ctrl-C. When the account is out of credit, the tool stops immediately rather than spending more attempts, and it tells you to resume. The checkpoint is deleted after a complete, successful run.

### Progress output

```
[1/8] Fetching pages (target + competitors)
    - Homepage: https://helcim.com
    - Pricing page: https://helcim.com/pricing
    ! Compare / calculator page: not found - tried 6 URL(s)
[2/8] Choosing high-intent keywords
[3/8] Layer 1 - Web Conversion
    / Researching Layer 1 - Web Conversion (84s)
...
[8/8] Writing the audit report
Report saved to /path/to/helcim-growth-audit-2026-09-24.md
```

A full run makes about 8 API calls, each with multiple web searches. Expect it to take roughly 5–15 minutes. It costs a few dollars in API usage, depending on the model and how much research each layer does.

## How it stays robust

- **Page fetch failures are recorded, not fatal.** If a page returns an error, times out or can't be found, the failure goes into the fetch log. Claude is told to try `web_fetch` on that URL, and to mark the page unavailable if that also fails.
- **Page discovery.** The tool first looks for pricing, compare, contact sales and signup pages among the homepage's links. If none match, it tries common paths such as `/pricing`, `/compare`, `/calculator`, `/contact-sales` and `/signup`.
- **Completed layers are checkpointed.** An interrupted run can be finished with `--resume` without paying for the finished layers again.
- **Layer failures don't stop the audit.** If one layer's API call fails, the report says so and the other layers still run. If the final synthesis fails, the raw layer notes are still saved.
- **Long research turns** that pause partway (`pause_turn`) are resumed automatically.
- **Refusal fallback.** Requests opt into the API's server-side refusal fallback (`fallbacks: "default"`). If the model declines a request, the API re-runs it on a fallback model. If your account doesn't accept this beta, the tool turns it off and retries.

## Limitations

- **Paid search visibility.** The web search tool returns organic results, not live Google ad auctions. Layer 2 labels each paid-search claim **OBSERVED** or **INFERRED**. It uses indirect evidence such as the Google Ads Transparency Center and dedicated paid landing pages. Where it can't observe ads, it tells you which manual check to run. For real impression share, check Google Ads Auction Insights or a tool like SEMrush or SpyFu.
- **Organic rankings** come from Anthropic's web search index, which only approximates Google's rankings. Treat positions as directional.
- **AI answer engine.** The AEO probe asks Claude, with web search, to answer as a neutral answer engine. That is one engine. ChatGPT, Perplexity and Google AI Overviews may answer differently.
- **Signup flows.** The tool never creates accounts or submits forms. Layer 4 only covers what is publicly visible, plus public onboarding documentation.
- **JavaScript-heavy sites.** The local fetcher does not run JavaScript, so single-page apps can produce thin snapshots. These are flagged, and Claude is asked to use `web_fetch` for more detail.
- **Payments-flavoured prompts.** The prompts are written for a payments/fintech audit (e.g. "best payment processor for [ICP]"). For another category, edit the prompts in `answer_engine_probe()` and `generate_keywords()`.
