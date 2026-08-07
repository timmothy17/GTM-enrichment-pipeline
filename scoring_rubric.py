"""
scoring_rubric.py

The three-layer scoring rubric and the JSON response schema, shared by
enrich.py and rescore.py.

This lives in one file because it did not used to. The rubric was duplicated as
prose in two prompt strings and drifted apart: every timing multiplier
disagreed between the two scripts, Layer 2 carried a different instruction
block on each side, and only rescore.py told the model to set
competitive_routing when the Ariba hard filter fired. The same company
therefore scored differently depending on which script had run last, and
nothing surfaced it.

Only the rubric and the response schema live here. The framing around them
stays with the callers, because it genuinely differs: enrich.py synthesises
from fresh module evidence, rescore.py replays evidence already stored.

Note that none of this arithmetic is executed. The weight table is
interpolated into the prompt as text and the model returns raw_score,
timing_multiplier and icp_score as JSON fields, which are written to the
database unchanged. See the limitations section of the README.
"""

import json
from typing import Dict

_WEIGHTS_PLACEHOLDER = "__WEIGHTS_JSON__"

# Layer 3 values are the compressed 0.85-1.15 scale with a true 1.00x neutral.
# The alternative was a 0.7-1.3 scale with no neutral row, which penalised a
# company at 0.9x simply for being unremarkable and let timing swing a score by
# +/-30%. Timing is an adjustment on top of fit and should not dominate it.
_SCORING_RUBRIC_TEMPLATE = """═══════════════════════════════════════════════════════════════
LAYER 1 — HARD FILTERS
═══════════════════════════════════════════════════════════════
Cap at 20/100 if ANY of these trigger:
1. SAP Ariba deployed → hard filter, set competitive_routing = "ariba"
2. Under 200 employees → too small for enterprise motion
3. Government or non-profit → different buying motion

NOT hard filters — route differently:
- Zip detected → competitive_routing = "zip", Tier 1 rip-and-replace, do NOT cap
- Coupa detected → competitive_routing = "coupa", Tier 2 long play, apply 0.90x timing
- No tool → competitive_routing = "none", greenfield, score normally

═══════════════════════════════════════════════════════════════
LAYER 2 — WEIGHTED SIGNALS
═══════════════════════════════════════════════════════════════
Weights:
__WEIGHTS_JSON__

Score each signal 0-100:
- 90-100 = Exceptional, clear evidence
- 70-89  = Strong evidence, minor ambiguity
- 50-69  = Moderate, partial or inferred
- 30-49  = Weak, mentioned in passing
- 0-29   = No evidence or contradicts signal

Rules:
- no_procurement_tool = POSITIVE for Omnea. No tool detected = high score (greenfield)
- procurement_hiring = actively building the function = high score
- headcount_growth = fast growth without procurement tooling = high score
- recent_funding = fresh capital creates budget + scaling pressure = high score
- finance_coo_hiring = new execs trigger tooling reviews = high score
- global_regulated_complex = multi-jurisdiction / compliance = high score

raw_score = sum(signal_score * weight / 100)

IMPORTANT: heavily favour companies with 1000+ employees. The ICP is
enterprise, not mid-market.

═══════════════════════════════════════════════════════════════
LAYER 3 — TIMING MULTIPLIER
═══════════════════════════════════════════════════════════════
1.15x → New CFO or COO hired in last 6 months
1.10x → Series B or C raised in last 12 months
1.05x → Active procurement/ops hiring right now
1.00x → No recent signals
0.90x → Coupa detected (long play, harder sell)
0.85x → Layoffs or cost freeze

final_score = min(100, round(raw_score * timing_multiplier))
If hard_filter_triggered: final_score = min(20, final_score)"""


RESPONSE_SCHEMA = """Return STRICT JSON:
{
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
    {
      "role": "CFO|COO|VP Finance|Head of Procurement|CEO",
      "detected": true|false,
      "evidence": "What research says about this role",
      "hiring_status": "actively_hiring|stable|recently_hired|unknown"
    }
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
  "icp_score_breakdown": {
    "no_procurement_tool": {"score": 0, "reasoning": ""},
    "procurement_hiring": {"score": 0, "reasoning": ""},
    "headcount_growth": {"score": 0, "reasoning": ""},
    "recent_funding": {"score": 0, "reasoning": ""},
    "finance_coo_hiring": {"score": 0, "reasoning": ""},
    "global_regulated_complex": {"score": 0, "reasoning": ""}
  },
  "icp_reasoning": "One paragraph: (1) hard filter result, (2) signal strengths, (3) timing/urgency, (4) why the final score is what it is",
  "recommended_outreach_angle": "One sentence hook for an SDR"
}
Return JSON only."""


def build_scoring_rubric(weights: Dict[str, int]) -> str:
    """Render the rubric with the active weight table interpolated."""
    return _SCORING_RUBRIC_TEMPLATE.replace(
        _WEIGHTS_PLACEHOLDER, json.dumps(weights, indent=2)
    )
