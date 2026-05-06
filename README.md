# GTM Enrichment Engine

A lightweight, LLM-powered company enrichment pipeline.

The problem: SDRs spend hours researching prospects before they can write a personalized email. Existing tools (Clay, Apollo) charge per-credit and don't always surface procurement-specific signals.

This solution: Ingest a list of target domains, run live web research via Kimi k2.6, score ICP fit with a three-layer system, and push structured intelligence directly into HubSpot where sales already works.

---

## Architecture

    CSV (domains) → ingest.py → Supabase (raw)
                                  ↓
                            enrich.py (Kimi API + web search)
                                  ↓
                            Supabase (enriched + scored)
                                  ↓
                            hubspot_sync.py → HubSpot CRM

- Supabase is the source of truth and audit layer. All raw evidence is stored as JSONB so you can rescore, regenerate outreach angles, or debug without re-running expensive web searches.
- HubSpot is the activation layer. Only the final, structured intelligence lands here — what the SDR actually needs to see.

---

## Three-Layer ICP Scoring For Procurement

Most enrichment tools give you a black-box score. This system separates fit, readiness, and urgency.

### Layer 1 — Hard Filters (Binary)

If any trigger, the company is capped at 20/100 regardless of other signals.

| Filter | Rationale |
|--------|-----------|
| Has Coupa, SAP Ariba, or Zip deployed | Already has procurement tooling — not a greenfield opportunity |
| Under 100 employees | Too small for enterprise procurement orchestration |
| Government or non-profit | Different buying motion, often not a fit |

### Layer 2 — Weighted Signals (0-100)

Each signal is scored against strict anchors:
- 90-100 = Exceptional, near-perfect evidence
- 70-89  = Strong evidence, but one source or slight ambiguity
- 50-69  = Moderate evidence. Signal exists but is partial or inferred
- 30-49  = Weak evidence. Mentioned in passing or low-confidence source
- 0-29   = No evidence or evidence contradicts the signal

| Signal | Weight | Why |
|--------|--------|-----|
| No procurement tool detected | 25% | Greenfield opportunity — the strongest positive signal for Omnea |
| Procurement hiring | 20% | Actively building the function = imminent need |
| Headcount growth | 15% | Fast growth creates vendor chaos without tooling |
| Recent funding | 15% | Fresh capital = budget + scaling pressure |
| Finance/COO hiring | 15% | New execs almost always trigger tooling reviews |
| Global/regulated complexity | 10% | Multi-jurisdiction compliance drives formal procurement |

Raw score = sum(signal_score * weight / 100)

### Layer 3 — Timing Multiplier (0.85x to 1.15x)

Captures urgency without compressing the entire scale into the 90-100 band.

| Multiplier | Condition |
|------------|-----------|
| 1.15x | New CFO or COO hired in last 6 months |
| 1.10x | Series B/C raised in last 12 months |
| 1.05x | Active procurement/ops hiring right now |
| 1.00x | No recent signals |
| 0.85x | Layoffs or cost freeze detected |

Final ICP score = min(100, round(raw_score * timing_multiplier))
If hard filter triggered: final score = min(20, final score)

---

## Smart Sync

A common mistake in enrichment-to-CRM pipelines is overwriting HubSpot records blindly or missing rescored data. This system uses version-aware syncing.

Sync query:

    SELECT * FROM companies
    WHERE enrichment_version >= 2
      AND (
        hubspot_synced = false 
        OR hubspot_synced IS NULL
        OR last_synced_version < enrichment_version
      )

How it works:

1. First enrichment runs → enrichment_version = 2, last_synced_version = 0
   → syncs to HubSpot → last_synced_version = 2

2. You rescore with new weights → enrichment_version = 3, last_synced_version = 2
   → sync picks it up automatically

3. No rescoring happens → last_synced_version = enrichment_version
   → skipped on next run

This means:
- No duplicate API calls to HubSpot for unchanged records
- Automatic catch-up when you improve scoring logic or refresh evidence
- Audit trail — you always know which version of your scoring model a HubSpot record reflects

---

## Setup

### 1. Environment Variables

Create a .env file:

    SUPABASE_CONNECTION_STRING=postgresql://postgres:[password]@db.[project].supabase.co:5432/postgres
    KIMI_API_KEY=sk-your-moonshot-key
    HUBSPOT_ACCESS_TOKEN=your-private-app-token

### Note on Supabase Connection

If you are on an IPv4 network (most home and office networks), use the Session Pooler connection string, not the Direct connection string. The direct connection requires IPv6 and will time out on standard networks.

### 2. Supabase Schema

Run the schema in schema.sql (or create tables manually). Key tables:

- companies — domains, enrichment data, ICP scores, HubSpot sync state
- contacts — placeholder for future decision-maker contact data
- enrichment_log — audit trail of every run
- scoring_weights — configurable ICP weights (change without code deploys)

### 3. HubSpot Custom Properties

Create these company properties in HubSpot before first sync:

| Property | Type | Purpose |
|----------|------|---------|
| icp_score | Number | Filter lists and trigger workflows |
| procurement_maturity | Dropdown (none, ad-hoc, emerging, mature) | Segment by readiness |
| competitive_risk | Single-line text | Know if they use a competitor |
| recommended_outreach_angle | Single-line text | SDR copy-pastes into email |
| news_buying_trigger | Dropdown (yes, no) | Trigger hot prospect workflows |
| enrichment_version | Number | Track data freshness |
| enriched_at | Date | Know when to re-enrich |

---

## Usage

### 1. Ingest Domains

    python ingest.py --csv targets.csv

CSV format:

    domain
    synthesia.io
    wayve.ai
    tines.com

### 2. Enrich Companies

    python enrich.py

This runs 3-5 targeted web searches per company via Kimi's $web_search tool, then synthesizes evidence into structured intelligence with the three-layer scoring system.

Rate limit: 3 RPM on the Kimi tier used here. A single company takes ~2 minutes; batch overnight for production use.

Cost: ~$0.05 per company (vs. Clay's per-credit model).

### 3. Sync to HubSpot

    python hubspot_sync.py

Upserts companies into HubSpot:
- Searches for existing records by domain to avoid duplicates
- Updates if found, creates if new
- Backfills hubspot_company_id into Supabase
- Sets last_synced_version = enrichment_version

### 4. Rescore Without Re-Research

If you adjust weights or scoring logic, run:

    python rescore.py

This feeds existing raw_evidence into the new scoring prompt — no web searches, no cost, instant.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Supabase as middle layer | Raw evidence is valuable. Storing it means you can rescore, debug, or train future models without re-paying for web searches. |
| Additive timing bonus | Multiplicative bonuses (1.3x) compress all good companies into 95-100. Additive keeps a real distribution (40-100). |
| Hard filters before scoring | A company using Coupa isn't a weak ICP — they're a non-ICP. The system should say so explicitly, not bury it in a low score. |
| Configurable weights in SQL | PMs and sales leaders can tune the model without a code deploy. scoring_weights table with is_active flag allows A/B testing. |
| Version-aware HubSpot sync | Prevents stale data in CRM and avoids unnecessary API calls. Critical when scoring logic evolves weekly. |

---

## Limitations & Next Steps

What this doesn't do (and shouldn't try to):

- Contact data — Names, emails, and direct dials require Apollo.io or LinkedIn Sales Navigator. This is the intelligence layer; contact data is a separate concern.
- Real-time triggers — Currently batch/on-demand. In production, wire to n8n or a queue to trigger enrichment when new domains enter the pipeline.
- Email generation — Outreach angles are one-line hooks. A future module could expand these into full drafts using the same evidence.

What I'd add for production:

1. n8n/Zapier orchestration — Trigger enrichment when a new domain is added to a Google Sheet or CRM list
2. Custom HubSpot workflows — Auto-create tasks when icp_score > 80 and news_buying_trigger = yes
3. Re-enrichment cadence — High-ICP companies re-checked every 30 days; stale data auto-flagged
4. Slack alerts — Post "New 90+ ICP: [Company]" to a sales channel on sync

---

## Built With

- Kimi k2.6 (https://platform.moonshot.cn/) — LLM with built-in web search
- Supabase (https://supabase.com/) — Postgres + real-time sync
- HubSpot CRM (https://www.hubspot.com/) — SDR workspace
- Python + openai SDK (Kimi is OpenAI-compatible)

---

## Notes

- All data is public-domain web research. No customer PII or proprietary data touches the LLM.
- Kimi's web search tool respects robots.txt and returns snippets, not full page scrapes.
- Rate limiting is enforced client-side (20 RPM) with exponential backoff on 429s. Upgrade to higher tiers in Kimi to unlock higher rate limits (and hence quicker processing)!
