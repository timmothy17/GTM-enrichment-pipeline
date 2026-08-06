"""
Omnea GTM Enrichment Engine v2.2
Three-layer scoring: Hard Filters → Weighted Signals → Timing Multiplier
"""

import os
import time
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from dotenv import load_dotenv
from tabulate import tabulate
from typing import Dict, List, Tuple, Any

load_dotenv()

CONNECTION_STRING = os.getenv("SUPABASE_CONNECTION_STRING")
KIMI_API_KEY = os.getenv("KIMI_API_KEY")

kimi = OpenAI(
    api_key=KIMI_API_KEY,
    base_url="https://api.moonshot.ai/v1"
)

WEB_SEARCH_TOOL = [
    {
        "type": "builtin_function",
        "function": {"name": "$web_search"},
    }
]

# ============================================================================
# RATE LIMITER
# ============================================================================

class RateLimiter:
    def __init__(self, max_rpm: int = 20):
        self.min_interval = 60.0 / max_rpm
        self.last_call = 0.0

    def wait(self):
        elapsed = time.time() - self.last_call
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            time.sleep(sleep_time)
        self.last_call = time.time()


rate_limiter = RateLimiter(max_rpm=20)

# ============================================================================
# SCORING SYSTEM — Three Layer
# ============================================================================

DEFAULT_WEIGHTS = {
    "no_procurement_tool": 25,
    "procurement_hiring": 20,
    "headcount_growth": 15,
    "recent_funding": 15,
    "finance_coo_hiring": 15,
    "global_regulated_complex": 10,
}

def get_active_scoring_weights(cur) -> Dict[str, int]:
    cur.execute("""
        SELECT weights_json FROM scoring_weights 
        WHERE is_active = true 
        ORDER BY created_at DESC 
        LIMIT 1
    """)
    row = cur.fetchone()
    if row and row["weights_json"]:
        return dict(row["weights_json"])
    return DEFAULT_WEIGHTS


# ============================================================================
# SEARCH ARCHITECTURE
# ============================================================================

SEARCH_MODULES = {
    "company_profile": [
        "{name} about us business model target customer pricing",
        "{name} headcount employees 2025 2026 team size",
        "{name} founded headquarters location global offices",
    ],
    "procurement_stack": [
        "{name} procurement software spend management vendor",
        "{name} finance tech stack ERP NetSuite SAP Coupa Zip Vendr",
        "{name} accounts payable automation SaaS spend control",
    ],
    "decision_makers": [
        "{name} leadership team CFO Chief Financial Officer",
        "{name} COO VP Operations Head of Procurement",
        "{name} executive team directors LinkedIn leadership",
    ],
    "jobs_deep": [
        "{name} careers job openings 2025 2026 hiring",
        "site:lever.co {domain} OR site:greenhouse.io {name} jobs",
        "{name} hiring finance operations procurement strategy",
    ],
    "news_triggers": [
        "{name} funding round investment 2025 2026",
        "{name} layoffs cost cutting budget freeze 2025",
        "{name} expansion new office product launch M&A 2025",
    ],
}


def get_connection():
    return psycopg2.connect(CONNECTION_STRING)


def log_enrichment(cur, company_id: str, enrichment_type: str,
                   status: str, cost_usd: float = 0.0,
                   error_message: str = None):
    cur.execute(
        """
        INSERT INTO enrichment_log 
          (company_id, enrichment_type, status, cost_usd, error_message)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (company_id, enrichment_type, status, cost_usd, error_message)
    )


def kimi_web_search(system_prompt: str, user_prompt: str,
                    max_tokens: int = 1200, max_retries: int = 3,
                    use_web_search: bool = True) -> Tuple[str, float]:
    """
    Single Kimi call, optionally with the built-in web search tool attached.

    Pass use_web_search=False for pure reasoning over evidence already in hand.
    Attaching the tool lets the model issue (billable) searches it does not
    need, so synthesis-style calls should leave it off.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    request_kwargs: Dict[str, Any] = {
        "model": "kimi-k2.6",
        "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    if use_web_search:
        request_kwargs["tools"] = WEB_SEARCH_TOOL

    for attempt in range(max_retries):
        rate_limiter.wait()

        try:
            completion = kimi.chat.completions.create(
                messages=messages, **request_kwargs
            )
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "rate_limit" in err_str:
                backoff = 3 * (2 ** attempt)
                print(f"      ⏳ Rate limited (429). Backing off {backoff}s...")
                time.sleep(backoff)
                continue
            raise

        choice = completion.choices[0]
        finish_reason = choice.finish_reason
        search_count = 0

        while finish_reason == "tool_calls":
            # reasoning_content has to be echoed back on the assistant turn.
            # Moonshot rejects the follow-up request if it is dropped from the
            # message history, so this is a protocol requirement, not caching.
            message_dict = {
                "role": "assistant",
                "content": choice.message.content or "",
                "tool_calls": choice.message.tool_calls,
                "reasoning_content": getattr(
                    choice.message, "reasoning_content", "Searching..."
                )
            }
            messages.append(message_dict)

            for tool_call in choice.message.tool_calls:
                tool_call_name = tool_call.function.name
                tool_call_arguments = json.loads(tool_call.function.arguments)

                if tool_call_name == "$web_search":
                    search_count += 1
                    query = tool_call_arguments.get("query", "unknown")
                    print(f"      🔍 Web search #{search_count}: {query[:60]}...")
                    # $web_search is a builtin_function: Moonshot runs the
                    # search server-side and injects the results itself. The
                    # client's only job is to acknowledge the call by echoing
                    # the arguments straight back as the tool result. This is
                    # the documented contract, not an unimplemented stub.
                    tool_result = tool_call_arguments
                else:
                    tool_result = f"Error: unknown tool '{tool_call_name}'"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call_name,
                    "content": json.dumps(tool_result),
                })

            rate_limiter.wait()
            try:
                completion = kimi.chat.completions.create(
                    messages=messages, **request_kwargs
                )
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "rate_limit" in err_str:
                    backoff = 3 * (2 ** attempt)
                    print(f"      ⏳ Rate limited during tool loop. Backing off {backoff}s...")
                    time.sleep(backoff)
                    break
                raise

            choice = completion.choices[0]
            finish_reason = choice.finish_reason

        if finish_reason == "stop":
            raw = choice.message.content.strip()

            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                raw = "\n".join(lines).strip()

            # Rough estimate only — a flat per-search rate, not token
            # accounting. Real usage is on completion.usage and is ignored.
            estimated_cost = round(search_count * 0.005, 4)
            return raw, estimated_cost

    raise Exception("Max retries exceeded due to rate limiting")


def run_module_search(company_name: str, domain: str,
                      module_name: str, queries: List[str]) -> Dict[str, Any]:
    system_prompt = (
        "You are a B2B sales intelligence analyst. Research the query "
        "and return concise, factual findings. Return ONLY valid JSON, "
        "no markdown, no backticks, no extra text."
    )

    results = {}
    total_cost = 0.0

    for i, query_template in enumerate(queries, 1):
        query = query_template.format(name=company_name, domain=domain)
        user_prompt = f"""Research this query: "{query}"

Return a JSON object with this structure:
{{
  "findings": "Concise summary of what you found (2-3 sentences max)",
  "confidence": "high|medium|low",
  "key_facts": ["bullet 1", "bullet 2"],
  "sources_implied": ["type of source"]
}}

If nothing relevant found: {{"findings": "No relevant information found", "confidence": "low", "key_facts": [], "sources_implied": []}}

Return JSON only."""

        try:
            raw, cost = kimi_web_search(system_prompt, user_prompt, max_tokens=600)
            total_cost += cost
            data = json.loads(raw)
            results[f"query_{i}"] = data
        except Exception as e:
            results[f"query_{i}"] = {
                "findings": f"Error: {str(e)}",
                "confidence": "low",
                "key_facts": [],
                "sources_implied": []
            }

    return {
        "module": module_name,
        "results": results,
        "cost": round(total_cost, 4)
    }


# ============================================================================
# SYNTHESIS — Three Layer Scoring
# ============================================================================

def synthesize_company_evidence(company_name: str, domain: str,
                                evidence: Dict[str, Any],
                                weights: Dict[str, int]) -> Tuple[Dict, float]:
    evidence_json = json.dumps(evidence, indent=2)

    system_prompt = (
        "You are a GTM intelligence analyst for Omnea, a procurement "
        "orchestration platform. Return ONLY valid JSON."
    )

    user_prompt = f"""You have completed deep research on {company_name} ({domain}).

Raw evidence from 5 research modules:
{evidence_json}

Analyze this evidence and produce a comprehensive sales intelligence report using the THREE-LAYER scoring system below.

═══════════════════════════════════════════════════════════════════
LAYER 1 — HARD FILTERS (Binary Disqualifiers)
═══════════════════════════════════════════════════════════════════
If any trigger, the company is capped at 20/100.

Hard filters:
1. Has SAP Ariba deployed → cap at 20 (deeply embedded, not worth pursuing)
2. Under 200 employees → cap at 20 (too small for enterprise procurement motion)
3. Government or non-profit → cap at 20

NOT hard filters — route differently instead:
- Zip detected → set competitive_routing = "zip", Tier 1 rip-and-replace opportunity, do NOT cap score
- Coupa detected → set competitive_routing = "coupa", Tier 2 long play, apply 0.85x timing multiplier
- No tool detected → set competitive_routing = "none", greenfield opportunity, score normally

═══════════════════════════════════════════════════════════════════
LAYER 2 — WEIGHTED SIGNALS (0-100 per signal)
═══════════════════════════════════════════════════════════════════
Use these exact weights:
{json.dumps(weights, indent=2)}

Compute: raw_score = sum(signal_score * weight / 100)

Rules:
- no_procurement_tool = POSITIVE for Omnea. No tool detected = high score (greenfield)
- procurement_hiring = actively building the function = high score
- headcount_growth = fast growth without procurement tooling = high score
- recent_funding = fresh capital creates budget + scaling pressure = high score
- finance_coo_hiring = new execs trigger tooling reviews = high score
- global_regulated_complex = multi-jurisdiction / compliance = high score

═══════════════════════════════════════════════════════════════════
LAYER 3 — TIMING MULTIPLIER (0.7x to 1.3x)
═══════════════════════════════════════════════════════════════════
Apply AFTER raw_score to capture urgency:

1.3x → New CFO or COO hired in last 6 months (new finance leader = tooling review)
1.2x → Series B or C raised in last 12 months (fresh capital, scaling pressure)
1.1x → Active procurement/ops hiring RIGHT NOW
0.9x → No recent news, stable, no hiring signals (latent need, low urgency)
0.7x → Layoffs or cost freeze signals (budget contraction)

Compute: final_score = min(100, round(raw_score * timing_multiplier))
If hard_filter_triggered: final_score = min(20, final_score)

═══════════════════════════════════════════════════════════════════

Return STRICT JSON:

{{
  "description": "One sentence on what the company does",
  "target_customer": "SMB|mid-market|enterprise|mixed",
  "pain_points": ["up to 5 business problems"],
  "tech_mentions": ["technologies mentioned"],
  "complexity_signals": ["signals of operational complexity"],
  
  "procurement_stack_detected": ["specific tools detected or empty list"],
  "procurement_maturity": "none|ad-hoc|emerging|mature",
  "competitive_risk": "None detected|Using [tool]|Mature procurement function",
  "competitive_routing": "none|zip|coupa|ariba",
  "territory_tag": "nordics|us_west|us_east|us_midwest|us_south|germany|france|uk|benelux|apac|other",
  
  "decision_makers": [
    {{
      "role": "CFO|COO|VP Finance|Head of Procurement|CEO",
      "detected": true|false,
      "evidence": "What research says about this role",
      "hiring_status": "actively_hiring|stable|recently_hired|unknown"
    }}
  ],
  
  "hiring_procurement": true|false,
  "procurement_job_titles": ["relevant job titles found"],
  "procurement_job_count": 0,
  "hiring_signal_strength": "none|low|medium|high",
  
  "news_buying_trigger": true|false,
  "news_buying_trigger_reason": "One sentence on strongest trigger",
  "key_news_item": "Most relevant headline",
  "funding_stage": "seed|series-a|series-b|series-c|growth|public|bootstrapped|unknown",
  "last_funding_amount": "amount or unknown",
  "last_funding_date": "date or unknown",
  "estimated_employee_count": null,
  "growth_signals": ["list of growth indicators"],
  
  "hard_filter_triggered": false,
  "hard_filter_reason": "Which hard filter triggered, or 'None'",
  "timing_multiplier": 1.0,
  "timing_multiplier_reason": "Why this multiplier was chosen",
  "raw_score": 0,
  "icp_score": 0,
  "icp_score_breakdown": {{
    "no_procurement_tool": {{"score": 0, "reasoning": ""}},
    "procurement_hiring": {{"score": 0, "reasoning": ""}},
    "headcount_growth": {{"score": 0, "reasoning": ""}},
    "recent_funding": {{"score": 0, "reasoning": ""}},
    "finance_coo_hiring": {{"score": 0, "reasoning": ""}},
    "global_regulated_complex": {{"score": 0, "reasoning": ""}}
  }},
  "icp_reasoning": "One paragraph explaining: (1) hard filter result, (2) signal strengths, (3) timing/urgency, and (4) why the final score is what it is",
  "recommended_outreach_angle": "One sentence hook for an SDR"
}}
Return JSON only."""
    # Synthesis reasons over evidence the module searches already gathered, so
    # the web search tool is deliberately withheld here. Attaching it let the
    # model fire extra billable searches that were never needed.
    raw, cost = kimi_web_search(
        system_prompt, user_prompt, max_tokens=2000, use_web_search=False
    )
    result = json.loads(raw)
    return result, cost


# ============================================================================
# DATABASE SAVE
# ============================================================================

def save_enrichment(cur, company_id: str, result: Dict, evidence: Dict,
                    weights: Dict, total_cost: float):
    # Build the full score breakdown including hard filters, timing, and signals
    breakdown = result.get("icp_score_breakdown", {})
    
    full_breakdown = dict(breakdown)
    full_breakdown["hard_filter_triggered"] = result.get("hard_filter_triggered", False)
    full_breakdown["hard_filter_reason"] = result.get("hard_filter_reason", "")
    full_breakdown["timing_multiplier"] = result.get("timing_multiplier", 1.0)
    full_breakdown["timing_multiplier_reason"] = result.get("timing_multiplier_reason", "")
    full_breakdown["raw_score"] = result.get("raw_score", 0)
    full_breakdown["final_score"] = result.get("icp_score", 0)
    full_breakdown["weights_used"] = weights

    cur.execute(
        """
        UPDATE companies SET
            website_description = %s,
            website_target_customer = %s,
            website_pain_points = %s,
            website_tech_mentions = %s,
            website_complexity_signals = %s,
            
            procurement_stack_detected = %s,
            procurement_maturity = %s,
            decision_makers = %s,
            competitive_risk = %s,
            competitive_routing = %s,
            territory_tag = %s,
            
            hiring_procurement = %s,
            procurement_job_titles = %s,
            procurement_job_count = %s,
            hiring_signal_strength = %s,
            
            news_buying_trigger = %s,
            news_buying_trigger_reason = %s,
            news_snippets = %s,
            
            icp_score = %s,
            icp_reasoning = %s,
            icp_score_breakdown = %s,
            recommended_outreach_angle = %s,
            
            raw_evidence = %s,
            enrichment_version = 2,
            website_enriched_at = NOW(),
            jobs_checked_at = NOW(),
            news_checked_at = NOW(),
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            result.get("description"),
            result.get("target_customer"),
            json.dumps(result.get("pain_points", [])),
            json.dumps(result.get("tech_mentions", [])),
            json.dumps(result.get("complexity_signals", [])),
            
            json.dumps(result.get("procurement_stack_detected", [])),
            result.get("procurement_maturity"),
            json.dumps(result.get("decision_makers", [])),
            result.get("competitive_risk"),
            result.get("competitive_routing", "none"),
            result.get("territory_tag", "other"),
            
            result.get("hiring_procurement", False),
            json.dumps(result.get("procurement_job_titles", [])),
            result.get("procurement_job_count", 0),
            result.get("hiring_signal_strength", "none"),
            
            result.get("news_buying_trigger", False),
            result.get("news_buying_trigger_reason"),
            json.dumps({
                "key_news": result.get("key_news_item"),
                "funding_stage": result.get("funding_stage"),
                "last_funding_amount": result.get("last_funding_amount"),
                "last_funding_date": result.get("last_funding_date"),
                "estimated_employees": result.get("estimated_employee_count"),
                "growth_signals": result.get("growth_signals", [])
            }),
            
            result.get("icp_score", 0),
            result.get("icp_reasoning"),
            json.dumps(full_breakdown),
            result.get("recommended_outreach_angle"),
            
            json.dumps(evidence),
            company_id
        )
    )

    log_enrichment(cur, company_id, "v2_deep_enrichment", "success", total_cost)


# ============================================================================
# MAIN
# ============================================================================

def run_enrichment_v2(batch_size: int = 3):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    weights = get_active_scoring_weights(cur)
    print(f"⚖️  Active scoring weights: {json.dumps(weights, indent=2)}")

    cur.execute("""
        SELECT * FROM companies 
        WHERE enrichment_version < 2 OR enrichment_version IS NULL
        ORDER BY created_at
        LIMIT %s
    """, (batch_size,))
    companies = cur.fetchall()
    total = len(companies)

    print(f"\n🚀 Found {total} companies to enrich with v2")
    print("=" * 70)

    summary_rows = []
    total_cost = 0.0

    for i, company in enumerate(companies, 1):
        company_id = company["id"]
        name = company["name"]
        domain = company["domain"]
        
        print(f"\n[{i}/{total}] {name} ({domain})")
        print("-" * 50)

        evidence = {}
        phase_cost = 0.0

        for module_name, queries in SEARCH_MODULES.items():
            print(f"  📡 Module: {module_name}...")
            module_result = run_module_search(name, domain, module_name, queries)
            evidence[module_name] = module_result["results"]
            phase_cost += module_result["cost"]

        print(f"  🧠 Synthesizing with 3-layer scoring...")
        try:
            result, synth_cost = synthesize_company_evidence(
                name, domain, evidence, weights
            )
            phase_cost += synth_cost
            
            save_enrichment(cur, company_id, result, evidence, weights, phase_cost)
            conn.commit()
            
            total_cost += phase_cost
            
            icp_score = result.get("icp_score", 0)
            raw_score = result.get("raw_score", 0)
            multiplier = result.get("timing_multiplier", 1.0)
            hard_filter = "🚫" if result.get("hard_filter_triggered") else "✅"
            maturity = result.get("procurement_maturity", "unknown")
            competitive = result.get("competitive_risk", "unknown")[:25]
            angle = result.get("recommended_outreach_angle", "")[:40]
            
            summary_rows.append([
                domain, hard_filter, raw_score, multiplier, icp_score, maturity, angle, f"${phase_cost:.3f}"
            ])
            
            print(f"  ✅ Filter:{hard_filter} Raw:{raw_score} ×{multiplier} → ICP:{icp_score}/100")

        except Exception as e:
            conn.rollback()
            log_enrichment(cur, company_id, "v2_deep_enrichment", "failed", 0, str(e))
            conn.commit()
            print(f"  ❌ Error: {e}")
            summary_rows.append([domain, "ERR", "ERR", "ERR", "ERR", "ERR", str(e)[:35], "$0"])

    cur.close()
    conn.close()

    print("\n" + "=" * 115)
    print("ENRICHMENT v2.2 COMPLETE — Three-Layer Scoring")
    print("=" * 115)
    print(tabulate(
        summary_rows,
        headers=["Domain", "Filter", "Raw", "Mult", "ICP", "Maturity", "Outreach Angle", "Cost"],
        tablefmt="grid",
        maxcolwidths=[16, 6, 5, 5, 5, 10, 30, 8]
    ))
    print(f"\n💰 Total cost: ${total_cost:.4f}")


if __name__ == "__main__":
    run_enrichment_v2(batch_size=2)
