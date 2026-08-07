"""
rescore.py
Re-score enriched companies using stored raw_evidence.
No web searches — uses existing data with updated scoring logic.
Optionally fetches territory_tag via a single lightweight Kimi call.
"""

import os
import re
import json
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from dotenv import load_dotenv
from tabulate import tabulate
from typing import Dict, Tuple

from scoring_rubric import build_scoring_rubric, RESPONSE_SCHEMA

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

def get_connection():
    return psycopg2.connect(CONNECTION_STRING)


def get_active_scoring_weights(cur) -> Dict:
    cur.execute("""
        SELECT weights_json FROM scoring_weights 
        WHERE is_active = true 
        ORDER BY created_at DESC 
        LIMIT 1
    """)
    row = cur.fetchone()
    if row and row["weights_json"]:
        return dict(row["weights_json"])
    return {
        "no_procurement_tool": 25,
        "procurement_hiring": 20,
        "headcount_growth": 15,
        "recent_funding": 15,
        "finance_coo_hiring": 15,
        "global_regulated_complex": 10,
    }


def fetch_territory_tag(company_name: str, domain: str) -> str:
    """
    Single lightweight Kimi search to determine territory.
    Only called if territory_tag is missing or 'other'.
    """
    system_prompt = (
        "You are a research assistant. Return ONLY valid JSON, no markdown."
    )
    user_prompt = f"""Where is {company_name} ({domain}) headquartered?

Return JSON only:
{{
  "territory_tag": "nordics|us_west|us_east|us_midwest|us_south|germany|france|uk|benelux|apac|other",
  "hq_city": "city name",
  "hq_country": "country name",
  "reasoning": "one sentence"
}}

Territory rules:
- Nordics = Denmark, Sweden, Norway, Finland, Iceland → "nordics"
- US = split by region:
    West (CA, WA, OR, NV, AZ, CO) → "us_west"
    East (NY, MA, CT, NJ, PA, DC, FL, GA) → "us_east"  
    Midwest (IL, OH, MI, MN, TX) → "us_midwest"
    South (TX, NC, TN, GA) → "us_south"
- Germany → "germany"
- France → "france"
- UK → "uk"
- Netherlands, Belgium, Luxembourg → "benelux"
- Asia Pacific → "apac"
- Everything else → "other"

Return JSON only."""

    try:
        time.sleep(3)  # Basic rate limiting
        completion = kimi.chat.completions.create(
            model="kimi-k2.6",
            max_tokens=200,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            extra_body={"thinking": {"type": "disabled"}},
            tools=WEB_SEARCH_TOOL
        )
        
        # Handle tool calls
        choice = completion.choices[0]
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        while choice.finish_reason == "tool_calls":
            message_dict = {
                "role": "assistant",
                "content": choice.message.content or "",
                "tool_calls": choice.message.tool_calls,
            }
            messages.append(message_dict)
            
            for tool_call in choice.message.tool_calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": json.dumps(
                        json.loads(tool_call.function.arguments)
                    ),
                })
            
            time.sleep(3)
            completion = kimi.chat.completions.create(
                model="kimi-k2.6",
                max_tokens=200,
                messages=messages,
                extra_body={"thinking": {"type": "disabled"}},
                tools=WEB_SEARCH_TOOL
            )
            choice = completion.choices[0]
        
        raw = choice.message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        return result.get("territory_tag", "other")
    
    except Exception as e:
        print(f"    ⚠️  Territory fetch failed: {e}")
        return "other"


# Most disqualifying first. Ariba is a hard filter, Coupa is a long play, Zip
# is a rip-and-replace target — so a stack containing both Ariba and Coupa must
# route to Ariba regardless of the order the model happened to list them in.
ROUTING_PRIORITY = ("ariba", "coupa", "zip")


def derive_competitive_routing(procurement_stack: list) -> str:
    """
    Derives competitive_routing from existing procurement_stack_detected.
    No new API call needed — data already in raw_evidence.
    """
    stack_lower = [str(tool).lower() for tool in procurement_stack or []]

    # Iterate priorities, not detected tools. Iterating tools returns whichever
    # vendor appears first in the list rather than the one that matters most.
    for routing in ROUTING_PRIORITY:
        # Word-anchored: bare substring matching made "zip" hit any vendor
        # whose name merely contains those letters (Zipline, Zipwhip, ...).
        pattern = re.compile(rf"\b{routing}\b")
        if any(pattern.search(tool) for tool in stack_lower):
            return routing

    return "none"


def rescore_company(company: dict, weights: Dict) -> Tuple[Dict, float]:
    """
    Re-score a company using stored raw_evidence.
    Only new API call is for territory_tag if missing.
    """
    
    evidence = company.get("raw_evidence") or {}
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except (json.JSONDecodeError, TypeError):
            evidence = {}
    
    evidence_json = json.dumps(evidence, indent=2)
    
    system_prompt = (
        "You are a GTM intelligence analyst for Omnea, a procurement "
        "orchestration platform. Return ONLY valid JSON."
    )
    
    user_prompt = f"""Re-score this company using the stored research evidence below.
No new research needed — derive everything from the evidence provided.

Company: {company['name']} ({company['domain']})

Stored evidence:
{evidence_json}

Apply the THREE-LAYER scoring system:

{build_scoring_rubric(weights)}

{RESPONSE_SCHEMA}"""

    completion = kimi.chat.completions.create(
        model="kimi-k2.6",
        max_tokens=2000,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        extra_body={"thinking": {"type": "disabled"}}
        # No web search tool — pure reasoning on stored evidence
    )
    
    raw = completion.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    result = json.loads(raw)
    
    # Derive competitive_routing from stack if model missed it
    if not result.get("competitive_routing"):
        result["competitive_routing"] = derive_competitive_routing(
            result.get("procurement_stack_detected", [])
        )
    
    return result, 0.0  # No search cost


def run_rescore(fetch_territory: bool = True):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    weights = get_active_scoring_weights(cur)
    print(f"⚖️  Active weights: {json.dumps(weights)}")
    
    # Fetch all previously enriched companies
    cur.execute("""
        SELECT id, name, domain, raw_evidence, 
               procurement_stack_detected, territory_tag,
               icp_score, enrichment_version
        FROM companies
        WHERE enrichment_version >= 2
        AND raw_evidence IS NOT NULL
        ORDER BY icp_score DESC NULLS LAST
    """)
    
    companies = cur.fetchall()
    total = len(companies)
    print(f"\n🔄 Rescoring {total} companies using stored evidence")
    print(f"💰 Zero web search cost — reusing raw evidence")
    print("=" * 70)
    
    summary_rows = []
    
    for i, company in enumerate(companies, 1):
        cid = company["id"]
        name = company["name"] or company["domain"]
        domain = company["domain"]
        
        print(f"\n[{i}/{total}] {name}")
        
        # Fetch territory if missing or unknown
        territory = company.get("territory_tag")

        if fetch_territory and (not territory or territory == "other"):
            print(f"  📍 Fetching territory tag...")
            territory = fetch_territory_tag(name, domain)
            print(f"  📍 Territory: {territory}")

        try:
            result, _ = rescore_company(company, weights)
            
            # Override territory with freshly fetched value if we got one
            if territory and territory != "other":
                result["territory_tag"] = territory
            
            # Build full breakdown
            breakdown = result.get("icp_score_breakdown", {})
            full_breakdown = dict(breakdown)
            full_breakdown.update({
                "hard_filter_triggered": result.get("hard_filter_triggered", False),
                "hard_filter_reason": result.get("hard_filter_reason", ""),
                "timing_multiplier": result.get("timing_multiplier", 1.0),
                "timing_multiplier_reason": result.get("timing_multiplier_reason", ""),
                "raw_score": result.get("raw_score", 0),
                "final_score": result.get("icp_score", 0),
                "weights_used": weights
            })
            
            # Update Supabase
            cur.execute("""
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
                    enrichment_version = enrichment_version + 1,
                    updated_at = NOW(),
                    hubspot_synced = false
                WHERE id = %s
            """, (
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
                cid
            ))
            conn.commit()
            
            icp = result.get("icp_score", 0)
            routing = result.get("competitive_routing", "none")
            territory_out = result.get("territory_tag", "other")
            hard = "🚫" if result.get("hard_filter_triggered") else "✅"
            angle = result.get("recommended_outreach_angle", "")[:45]
            
            summary_rows.append([
                domain[:20], hard, result.get("raw_score", 0),
                result.get("timing_multiplier", 1.0), icp,
                routing, territory_out, angle
            ])
            
            print(f"  ✅ {hard} Raw:{result.get('raw_score',0)} "
                  f"×{result.get('timing_multiplier',1.0)} → {icp}/100 "
                  f"| routing:{routing} | territory:{territory_out}")
        
        except Exception as e:
            conn.rollback()
            print(f"  ❌ Error: {e}")
            summary_rows.append([domain[:20], "ERR", "ERR", "ERR", 
                                  "ERR", "ERR", "ERR", str(e)[:35]])
    
    cur.close()
    conn.close()
    
    print("\n" + "=" * 115)
    print("RESCORE COMPLETE — Zero web search cost")
    print("=" * 115)
    print(tabulate(
        summary_rows,
        headers=["Domain", "Filter", "Raw", "Mult", "ICP", 
                 "Routing", "Territory", "Outreach Angle"],
        tablefmt="grid",
        maxcolwidths=[20, 6, 5, 5, 5, 8, 10, 35]
    ))
    print(f"\n💡 Run hubspot_sync.py to push updated scores to HubSpot")


if __name__ == "__main__":
    run_rescore(fetch_territory=True)
