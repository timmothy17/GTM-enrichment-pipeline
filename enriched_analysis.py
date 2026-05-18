def build_enriched_prompt(transcript, company_history, regional_patterns, rep_patterns):
    return f"""
You are analysing a sales call with full context for a procurement software company.

Account history (previous calls with this company):
{company_history}

Regional patterns (what works in this territory):
{regional_patterns}

Rep patterns (this rep's historical performance):
{rep_patterns}

This call transcript:
{transcript}

Return JSON only:
{{
  "pain_points_identified": ["list"],
  "objections_raised": ["list"],
  "champion_signals": ["behaviours suggesting internal advocacy"],
  "competitor_mentions": ["any competitors mentioned"],
  "recommended_outreach_angle": "one sentence hook for follow up",
  "deal_stage_assessment": "early|mid|late|stalled",
  "recommended_next_steps": ["prioritised list"],
  "regional_comparison": "how this call compares to regional patterns",
  "coaching_note": "one sentence for rep improvement if relevant"
}}
"""
