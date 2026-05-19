# GTM Enrichment Engine
### Signal-based account intelligence + conversation analysis for Omnea

A production-ready GTM intelligence system with two working modules.

**Module 1 — Account intelligence:** Ingest target company domains, run live web research, score ICP fit with a three-layer model, and push structured intelligence into HubSpot automatically.

**Module 2 — Conversation intelligence:** Process call transcripts through a context assembly layer, run AI analysis against full account history, and write structured insights back to HubSpot and Notion automatically — eliminating the manual Gong-to-Claude workflow your team runs today.

---

## Architecture

```
CSV (domains) → ingest.py → Supabase (raw)
                               ↓
                         enrich.py (Kimi + web search)
                               ↓
                         Supabase (enriched + scored)
                               ↓
                         hubspot_sync.py → HubSpot CRM

Granola webhook → conversation_intelligence_demo.py → Supabase (call_records)
                                                          ↓
                                                     HubSpot + Notion (write-back)
```

**Supabase** is the source of truth and audit layer. Raw evidence is stored as JSONB — rescore without re-running expensive web searches.

**HubSpot** is the activation layer. Only final structured intelligence lands here — what the SDR actually needs.

---

## Why Python, Not n8n

This is a deliberate architectural decision for the demo.

n8n excels at: event routing, webhook handling, scheduling, and simple API connections. It is an orchestration tool.

It is not suited for: multi-step data transformation with business logic, stateful processing, complex scoring models with configurable weights, retry logic with exponential backoff, or anything that benefits from unit testing and version control.

This pipeline contains a rate limiter class, exponential backoff on 429s, multi-step tool call handling, JSONB transformations, version comparison logic, competitive routing rules, configurable weight tables, and a synthesis layer feeding back into scoring. None of that belongs in a visual workflow tool.

**The correct separation of concerns:**
- n8n = when to run (triggers, scheduling, routing, alerting)
- Python = what to do (business logic, scoring, analysis)
- SQL Database (Supabase) = source of truth (persistence, audit trail)
- HubSpot = activation layer (what reps see)

The scripts were built and validated locally first. Deploying half-working logic into n8n creates debugging hell. Code first, orchestration second.

---

## Production Deployment

These scripts are designed as triggered jobs, not persistent services.

**Recommended:** Deploy as a private web service on **Render.com** (containerisation with Docker not required) — the same platform already running production systems built at my current company. n8n calls each service via HTTP when triggered.

```
Render service: enrich_service      (POST /enrich)
Render service: conv_intel_service  (POST /analyse-call)
Render service: rescore_service     (POST /rescore)
Render service: hubspot_sync_service (POST /sync)
```

n8n handles:
- New domain enters HubSpot → trigger enrich
- Granola webhook fires → trigger conv_intel (immediate), then 24hr delay → enriched analysis
- Nightly → trigger rescore on stale records
- After every enrich/rescore → trigger sync
- Errors → Slack alert

This is a one-day wiring task once scoring logic is validated — which is why it was built and tested locally first.

---

## Three-Layer ICP Scoring

Most enrichment tools give a black-box score. This system separates fit, readiness, and urgency.

### Layer 1 — Hard Filters (Binary)

If any trigger, score is capped at 20/100.

| Filter | Rationale |
|--------|-----------|
| SAP Ariba deployed | Deeply embedded — not worth pursuing |
| Under 200 employees | Too small for enterprise procurement motion |
| Government or non-profit | Different buying motion |

**Not hard filters — route differently:**

| Detection | Routing | Rationale |
|-----------|---------|-----------|
| Zip detected | competitive_routing = "zip" | Rip-and-replace opportunity, Tier 1 |
| Coupa detected | competitive_routing = "coupa" | Long play, Tier 2, 0.90x timing multiplier |
| No tool | competitive_routing = "none" | Greenfield, score normally |

Coupa and Zip are not disqualifiers — they're routing signals. This distinction matters for outreach strategy.

### Layer 2 — Weighted Signals (0-100 per signal)

| Signal | Weight | Why |
|--------|--------|-----|
| No procurement tool detected | 25% | Greenfield — strongest positive signal for Omnea |
| Procurement hiring | 20% | Building the function = imminent need |
| Headcount growth | 15% | Fast growth creates vendor chaos without tooling |
| Recent funding | 15% | Fresh capital = budget + scaling pressure |
| Finance/COO hiring | 15% | New execs almost always trigger tooling reviews |
| Global/regulated complexity | 10% | Multi-jurisdiction drives formal procurement |

`raw_score = sum(signal_score * weight / 100)`

Weights are stored in the `scoring_weights` SQL table and configurable without a code deploy. The `is_active` flag allows A/B testing of different weight sets.

### Layer 3 — Timing Multiplier (0.85x to 1.15x)

| Multiplier | Condition |
|------------|-----------|
| 1.15x | New CFO or COO hired in last 6 months |
| 1.10x | Series B/C raised in last 12 months |
| 1.05x | Active procurement/ops hiring right now |
| 1.00x | No recent signals |
| 0.90x | Coupa detected (long play, harder sell) |
| 0.85x | Layoffs or cost freeze |

`final_score = min(100, round(raw_score * timing_multiplier))`

Additive multiplier keeps a real score distribution (40-100) rather than compressing everything into 90-100.

---

## Smart Sync

Version-aware syncing prevents stale data in HubSpot and avoids unnecessary API calls.

```sql
SELECT * FROM companies
WHERE enrichment_version >= 2
  AND (
    hubspot_synced = false
    OR hubspot_synced IS NULL
    OR last_synced_version < enrichment_version
  )
```

When `rescore.py` runs, it increments `enrichment_version` and sets `hubspot_synced = false`. The sync query picks these up automatically on the next run — no manual intervention needed.

**What this means in practice:**
- Rescore overnight → HubSpot automatically reflects updated scores on next sync
- No duplicate API calls for unchanged records
- Audit trail: always know which scoring model version a HubSpot record reflects

---

## Conversation Intelligence Module

`conversation_intelligence_demo.py` demonstrates the full pipeline end to end.

### The Problem It Solves

Every rep on the team manually copies Gong transcripts into Claude for analysis. The output stays in Claude — nothing writes back to HubSpot or Notion. Context is lost. The next rep to work the account starts from scratch.

### The Pipeline

```
Granola / Gong transcript
        ↓
Context assembly
(pull ICP score + call history from Supabase, Notion page for account)
        ↓
Immediate analysis — fires within minutes of call ending
(what happened: outcome, commitments, champion identified, next step)
        ↓
Store to Supabase call_records
        ↓
[24hr wait via n8n delay node in production]
        ↓
Enriched analysis — fires 24hrs later with full context
(what it means: pain points, champion signals, deal stage, outreach angle, risk signals)
        ↓
Write back to HubSpot deal record + Notion page automatically
```

### Two Analysis Tiers

**Immediate** — lightweight, fires within minutes. A rep needs to act within hours of a call. This tier gives them the next step immediately without waiting for deep analysis.

Output: call outcome, next meeting booked, champion identified, explicit commitments, immediate next step.

**Enriched** — deeper, fires 24hrs later. Pulls full account history from Supabase, cross-references the ICP intelligence from the enrichment engine, and reasons about the account in full context.

Output: pain points, objections, champion signals, deal stage assessment, recommended outreach angle for the follow-up, risk signals, coaching note for the rep.

### Context Assembly — Why It Matters

Claude has no memory between API calls. The middleware layer assembles full context before each analysis call:

- Company ICP score, competitive routing, territory tag, recommended angle (from Supabase)
- Previous call records for this account (from call_records table)
- Immediate analysis output (for enriched tier)

This is what makes the enriched analysis intelligent rather than generic — it knows this is a Series D company with a new CFO, a greenfield procurement stack, and a Q3 board meeting deadline before it reads a word of the transcript.

### Note on LLM Choice

Current implementation uses Kimi k2.6. Production deployment would use Claude (Anthropic API) for better reasoning on nuanced call analysis and alignment with the team's existing workflow.

---

## Setup

### 1. Environment Variables

Create a `.env` file:

```
SUPABASE_CONNECTION_STRING=postgresql://postgres:[password]@db.[project].supabase.co:5432/postgres
KIMI_API_KEY=sk-your-moonshot-key
HUBSPOT_ACCESS_TOKEN=your-private-app-token
```

**Note on Supabase connection:** Use the Session Pooler connection string on IPv4 networks. The direct connection requires IPv6 and will time out on standard networks.

### 2. Supabase Schema

Key tables:

| Table | Purpose |
|-------|---------|
| `companies` | Enrichment data, ICP scores, HubSpot sync state |
| `contacts` | Decision maker contact data |
| `call_records` | Transcripts, immediate + enriched analysis JSONB |
| `call_patterns` | Cross-rep regional intelligence (weekly batch job) |
| `enrichment_log` | Audit trail of every run |
| `scoring_weights` | Configurable ICP weights — no code deploy to change |

### 3. HubSpot Custom Properties

Create these before first sync:

| Property | Type | Purpose |
|----------|------|---------|
| icp_score | Number | Filter lists, trigger automated workflows |
| procurement_maturity | Dropdown (none/ad-hoc/emerging/mature) | Segment by readiness |
| competitive_routing | Dropdown (none/zip/coupa/ariba) | Route to correct outreach sequence |
| competitive_risk | Single-line text | Human-readable competitive context |
| recommended_outreach_angle | Single-line text | SDR copy-pastes to open outreach |
| territory_tag | Dropdown | Route to correct sequence variant |
| news_buying_trigger | Dropdown (yes/no) | Trigger hot prospect workflows |
| enrichment_version | Number | Track data freshness |
| enriched_at | Date | Know when to re-enrich |

---

## Usage

### 1. Ingest domains
```bash
python ingest.py
```
Reads `test_leads.csv`, inserts companies into Supabase with deduplication by domain.

### 2. Enrich companies
```bash
python enrich.py
```
Runs 5 targeted web search modules per company via Kimi k2.6, synthesises evidence into structured intelligence with three-layer ICP scoring. Approximately $0.05 per company. Run overnight for batches.

### 3. Rescore without re-research
```bash
python rescore.py
```
Feeds stored `raw_evidence` back through updated scoring logic. **Zero web search cost.** This is the feature that makes the system maintainable — update the scoring model and propagate changes across all records instantly.

Also fetches `territory_tag` for any records where it's missing or unknown (single lightweight Kimi call per company).

### 4. Sync to HubSpot
```bash
python hubspot_sync.py
```
Version-aware upsert. Only syncs records where `last_synced_version < enrichment_version`. Searches for existing records by domain before creating to avoid duplicates.

### 5. Run conversation intelligence demo
```bash
python conversation_intelligence_demo.py
```
Runs the full pipeline against a Synthesia sample transcript. Loads company context from Supabase (ICP score, enrichment intelligence), runs both analysis tiers, stores to `call_records`, and prints the simulated HubSpot write-back.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Supabase as middle layer | Raw evidence is valuable. Storing it means you can rescore, debug, or retrain without re-paying for web searches. This is the feature that makes the system maintainable long-term. |
| Additive timing multiplier | Multiplicative multipliers compress scores into 90-100. Additive keeps a real distribution (40-100) — better for prioritisation. |
| Hard filters before scoring | A company using SAP Ariba isn't a weak ICP — they're a non-ICP. Separate categories, not low scores. |
| Competitive routing not hard filter | Zip is a rip-and-replace opportunity. Coupa is a long play. Neither is disqualified — they're routed to different sequences. |
| Configurable weights in SQL | RevOps can tune the model without a code deploy. `scoring_weights` table with `is_active` flag allows A/B testing of weight sets. |
| Version-aware HubSpot sync | Prevents stale data. Rescoring automatically flags records for resync — no manual intervention needed. |
| Two-tier call analysis | Reps need next steps within hours. Deep context can wait 24hrs. Separating the tiers keeps immediate analysis fast and cheap. |
| Python not n8n for business logic | Complex scoring and analysis belongs in version-controlled, testable code. n8n handles orchestration; Python handles intelligence. |

---

## Known Limitations and Roadmap

### Current limitations

- **Name disambiguation:** "Causaly" vs "Causal" — similar company names can confuse the research module. Fix: pre-search validation step to ground each company's identity before running module queries.
- **B2C companies** score 0 rather than triggering a hard filter — add "B2C detected" as an explicit hard filter category.
- **Ireland** maps to "other" territory — should map to "uk" for Omnea's sales motion.

### Modular search architecture (planned)

Each search module is currently a Python dict. The roadmap is YAML config files per module — drop a new file in `/modules` and the pipeline picks it up without code changes.

This enables:
- A/B testing of search strategies without code deploys
- Territory-specific modules (German-language searches for German companies)
- Cost and signal quality tracking per module independently
- Enabling/disabling modules per run via `is_active` flag

**Module candidates:**

| Module | New signals unlocked |
|--------|---------------------|
| g2_reviews | Pain points in buyers' own words |
| glassdoor_ops | Internal process signals from employee reviews |
| linkedin_jobs_deep | Live hiring velocity with job title specifics |
| regulatory | Regulated complexity signals from filings |

### Production next steps

1. Render.com deployment (Dockerisation not required on this platform)
2. n8n orchestration wiring (triggers, scheduling, routing)
3. HubSpot workflows — auto-create tasks when `icp_score > 80` and `news_buying_trigger = yes`
4. Re-enrichment cadence — 30-day refresh for high-ICP companies, auto-flag stale records
5. Slack alerts — post "New 90+ ICP: [Company]" to a sales channel on sync
6. Weekly regional pattern analysis — batch job against `call_records` to surface what's working by territory

---

## Brief Objectives Mapping

This system was designed against three objectives from the GTM Systems Engineer brief:

**Remove daily administrative burden**
- Conversation intelligence eliminates manual Gong-to-Claude transcript copy-paste
- Automated enrichment removes account research time (hours → seconds per account)
- Version-aware sync eliminates manual CRM updates after rescoring

**Unlock new pipeline generation potential**
- Signal-based ICP scoring surfaces high-value accounts from the 400 per rep that would otherwise be deprioritised
- Sixth Sense integration connects unused intent data to the prioritisation layer
- Territory tagging enables region-specific sequence selection automatically

**Drive AI adoption**
- Kimi/Claude in the conversation intelligence layer — integrated into existing workflow, not bolted on
- AI-powered ICP scoring embedded in HubSpot where the team already works
- Both modules write back to HubSpot and Notion — the tools the team uses daily — rather than creating new interfaces to adopt

---

## Built With

- [Kimi k2.6](https://platform.moonshot.cn/) — LLM with built-in web search (production: Claude API)
- [Supabase](https://supabase.com/) — PostgreSQL + real-time sync
- [HubSpot CRM](https://www.hubspot.com/) — SDR activation layer
- Python + openai SDK (Kimi is OpenAI-compatible)
- [Render.com](https://render.com/) — target deployment platform for Python microservices
- n8n — orchestration layer (production wiring)

---

## Notes

- All data is public-domain web research. No customer PII or proprietary data touches the LLM.
- Kimi's web search tool respects robots.txt and returns snippets, not full page scrapes.
- Rate limiting is enforced client-side (20 RPM) with exponential backoff on 429s.
- The `rescore.py` command is the most important feature for long-term maintainability — it makes improving the model a lot more cost efficient
