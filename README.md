# growth-audit-tool

A reusable command-line tool that runs a structured, five-layer growth audit on any company website through the lens of a specific Ideal Customer Profile (ICP). It compares the company against named competitors and writes the findings to a markdown report.

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

## Usage

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
