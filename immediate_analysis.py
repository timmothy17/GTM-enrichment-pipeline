def build_immediate_prompt(transcript, company_context):
    return f"""
You are analysing a sales call for a procurement software company.

Company context:
{company_context}

Call transcript:
{transcript}

Return JSON only:
{{
  "call_outcome": "meeting_booked|follow_up_needed|no_interest|unclear",
  "next_meeting_booked": true|false,
  "explicit_commitments": ["list of things either party committed to"],
  "key_topics_covered": ["list of topics"],
  "sentiment": "positive|neutral|negative",
  "immediate_next_step": "one sentence recommended action for the rep"
}}
"""
