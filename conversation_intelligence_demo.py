"""
conversation_intelligence_demo.py

Demonstrates the full conversation intelligence pipeline end to end.
Simulates the automated flow that n8n would orchestrate in production:

  Granola/Gong webhook → context assembly → immediate analysis → 
  24hr enriched analysis → HubSpot + Notion write-back

In production:
  - Granola / Gong webhook triggers n8n workflow
  - n8n calls the analysis functions via HTTP
  - 24hr delay node handles the enriched analysis timing
  - Results write back to HubSpot and Notion automatically

This script runs the full pipeline manually for demonstration 
and validation purposes before production wiring.
"""

import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

CONNECTION_STRING = os.getenv("SUPABASE_CONNECTION_STRING")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

client = OpenAI(
    api_key=os.getenv("KIMI_API_KEY"),
    base_url="https://api.moonshot.ai/v1"
)

SAMPLE_TRANSCRIPT = """
Rep (Jamie, Omnea): Hey Marcus, really appreciate you making 
time — I know Q2 is always hectic on the finance side.

Prospect (Marcus Webb, VP Finance, Synthesia): Ha, yeah tell 
me about it. We've got board prep next week so the timing's 
interesting but I figured a 30-minute break might actually 
do me good. How are things at Omnea?

Rep: Going well, growing fast which brings its own chaos — 
you'll know the feeling. We just moved offices actually, 
which was its own procurement nightmare ironically enough.

Prospect: Ha, classic. Yeah we did that about a year ago, 
absolute disaster. Anyway — what did you want to chat about?

Rep: So just to set context quickly — Omnea helps scaling 
companies get control of their procurement and vendor 
management. The reason I reached out specifically is that 
you've had a really significant growth year and from the 
outside it looks like you're at the stage where the informal 
processes that got you here start to become the bottleneck. 
Does that resonate at all?

Prospect: Honestly yeah, more than I'd like to admit. We 
closed our Series D last year and the pace of hiring since 
then has been pretty intense. Finance team alone went from 
8 to 22 people in about 14 months.

Rep: That's a huge jump. What's that done to your vendor and 
procurement process — are you managing things centrally or 
is it more distributed across teams?

Prospect: It's pretty chaotic if I'm being honest with you. 
Every team basically buys what they need and then comes to 
us after the fact for sign-off. We've probably got 80 or 90 
SaaS tools at this point — I genuinely don't know the exact 
number. Renewals sneak up on us constantly. We had three 
auto-renewals last quarter that nobody flagged until after 
they'd already gone through.

Rep: How much are we talking in terms of unplanned spend?

Prospect: Nothing catastrophic individually but it adds up. 
And the bigger issue is the lack of visibility. I can't tell 
you right now what we're actually using versus what we're 
paying for. That's the thing that keeps me up at night more 
than the individual amounts.

Rep: When approvals do happen — is there a system or is it 
more ad hoc?

Prospect: Slack and email threads mostly. Someone sends me 
a message, I ask a few questions, sometimes it gets approved 
sometimes it doesn't. There's no real audit trail. And 
honestly I've been meaning to fix this for a while but 
there's always something more urgent.

Rep: Has anything changed recently that's made it more 
pressing?

Prospect: Yeah actually — our new CFO Sarah joined about six 
weeks ago. She came from a company that had proper 
procurement tooling in place and she's not happy with what 
she walked into. She's already flagged it as something we 
need to address, probably in the back half of the year.

Rep: Interesting — do you know what tooling she had 
previously?

Prospect: I think they were using Coupa but she said it was 
massively over-engineered for what they actually needed. 
Too complex, too expensive, implementation took forever. 
She's specifically looking for something lighter weight that 
the team will actually use day to day.

Rep: That's a really common story with Coupa at your stage. 
What would light weight look like to her — is it about 
the interface, the implementation timeline, the cost?

Prospect: Probably all three honestly. And I'll be straight 
with you Jamie — Sarah holds the budget quite tightly. 
Anything new has to have a really clear business case. She's 
going to want to see concrete numbers on what we're losing 
through the current process and what the ROI looks like 
before she'd even consider bringing it to the board.

Rep: That's completely fair and honestly it's the right 
question. The way we'd approach that is to actually do a 
spend analysis with you first — look at what you're currently 
running, identify where the leakage is, and quantify it. 
Most companies at your stage find somewhere between 15 and 
25 percent of software spend is either duplicated, unused, 
or auto-renewed without review. For a company your size 
that's usually a meaningful number.

Prospect: Yeah that would actually be a more compelling 
conversation to bring to Sarah than just a product demo. 
She responds better to data.

Rep: Exactly. Is it worth getting her on a call so we can 
understand what success looks like from her side? I'd rather 
build the business case around her criteria than assume.

Prospect: Yeah that makes sense. She's the one who'll 
ultimately make the call anyway. I can intro you — she's 
pretty direct so just come prepared with specifics.

Rep: Appreciated, I will. One last thing — you mentioned 
probably back half of the year. Is Q3 realistic or is that 
more of a Q4 conversation once budget planning is done?

Prospect: Probably Q3 for evaluation, Q4 for any actual 
decision. We've got a board meeting in September and if 
there's something worth presenting it would need to be ready 
by then.

Rep: That's really helpful framing. I'll send over some 
availability for a three-way with Sarah and in the meantime 
I'll put together a quick overview of how we'd approach the 
spend analysis so she has something concrete to look at 
before we speak.

Prospect: Perfect. Good chat Jamie, looking forward to it.

Rep: Likewise, thanks Marcus. Speak soon.
"""


def get_connection():
    return psycopg2.connect(CONNECTION_STRING)


def get_company_context(cur, domain: str) -> dict:
    """Pull enrichment intelligence for this company from Supabase."""
    cur.execute("""
        SELECT 
            id, name, domain, hubspot_company_id,
            icp_score, recommended_outreach_angle,
            procurement_maturity, competitive_routing,
            territory_tag, website_description,
            icp_reasoning, decision_makers,
            news_snippets
        FROM companies
        WHERE domain = %s
    """, (domain,))
    return cur.fetchone()


def get_call_history(cur, company_id: str) -> list:
    """Pull previous call records for this company."""
    cur.execute("""
        SELECT 
            call_date, rep_name, duration_seconds,
            immediate_analysis, enriched_analysis
        FROM call_records
        WHERE company_id = %s
        ORDER BY call_date DESC
        LIMIT 3
    """, (company_id,))
    return cur.fetchall()


def run_immediate_analysis(transcript: str, company_context: dict) -> dict:
    """
    Lightweight analysis fired immediately after call ends.
    Captures what happened — not yet what it means.
    """
    prompt = f"""You are analysing a sales call for Omnea, a procurement 
orchestration platform.

Company context:
- Name: {company_context['name']}
- ICP Score: {company_context['icp_score']}/100
- Procurement maturity: {company_context['procurement_maturity']}
- Recommended outreach angle: {company_context['recommended_outreach_angle']}

Call transcript:
{transcript}

Return JSON only:
{{
  "call_outcome": "meeting_booked|follow_up_needed|no_interest|unclear",
  "next_meeting_booked": true,
  "next_meeting_with": "name and role of next contact",
  "explicit_commitments": ["list of things either party committed to"],
  "key_topics_covered": ["list of main topics discussed"],
  "sentiment": "positive|neutral|negative",
  "champion_identified": true,
  "champion_name": "name if identified",
  "champion_role": "role if identified",
  "economic_buyer_identified": true,
  "economic_buyer_name": "name if identified",
  "immediate_next_step": "one sentence recommended action for the rep"
}}"""

    response = client.chat.completions.create(
        model="kimi-k2.6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"thinking": {"type": "disabled"}}
    )
    
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def run_enriched_analysis(
    transcript: str,
    company_context: dict,
    call_history: list,
    immediate_analysis: dict
) -> dict:
    """
    Deeper analysis with full account context.
    In production this fires 24 hours later via n8n.
    For demo purposes we run it immediately after.
    """
    
    history_text = "No previous calls on record — this is the first contact."
    if call_history:
        history_text = json.dumps([
            {
                "date": str(r["call_date"]),
                "immediate_analysis": r["immediate_analysis"]
            }
            for r in call_history
        ], indent=2)
    
    dm_text = json.dumps(company_context.get("decision_makers") or [], indent=2)
    
    prompt = f"""You are analysing a sales call with full account context 
for Omnea, a procurement orchestration platform.

ACCOUNT INTELLIGENCE (from enrichment engine):
Company: {company_context['name']} ({company_context['domain']})
ICP Score: {company_context['icp_score']}/100
Territory: {company_context['territory_tag']}
Competitive routing: {company_context['competitive_routing']}
Description: {company_context['website_description']}
ICP reasoning: {company_context['icp_reasoning'][:500] if company_context.get('icp_reasoning') else 'N/A'}
Known decision makers: {dm_text}

PREVIOUS CALL HISTORY:
{history_text}

IMMEDIATE ANALYSIS (captured right after call):
{json.dumps(immediate_analysis, indent=2)}

THIS CALL TRANSCRIPT:
{transcript}

Analyse this call in full context. Return JSON only:
{{
  "pain_points_identified": ["specific pain points mentioned"],
  "objections_raised": ["any objections or concerns raised"],
  "champion_signals": ["behaviours suggesting Marcus will advocate internally"],
  "economic_buyer_confirmed": true,
  "economic_buyer_notes": "what we know about Sarah the CFO",
  "competitor_mentions": ["any competitors mentioned and context"],
  "deal_stage_assessment": "early|mid|late|stalled",
  "recommended_outreach_angle": "one sentence hook for follow up email to Sarah",
  "recommended_next_steps": [
    "prioritised list of actions for the rep"
  ],
  "urgency_signals": ["signals that suggest timing matters"],
  "risk_signals": ["anything that could slow or kill the deal"],
  "coaching_note": "one sentence feedback for the rep on this call",
  "account_summary": "two sentence summary of where this account stands and why it matters"
}}"""

    response = client.chat.completions.create(
        model="kimi-k2.6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"thinking": {"type": "disabled"}}
    )
    
    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def store_call_record(
    cur,
    company_id: str,
    contact_id: str,
    hubspot_company_id: str,
    territory: str,
    transcript: str,
    immediate_analysis: dict,
    enriched_analysis: dict
) -> str:
    """Store the full call record in Supabase."""
    cur.execute("""
        INSERT INTO call_records (
            company_id, contact_id, hubspot_company_id,
            granola_meeting_id, call_date, duration_seconds,
            rep_name, territory, transcript,
            immediate_analysis, enriched_analysis,
            immediate_processed_at, enriched_processed_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (granola_meeting_id) DO UPDATE SET
            immediate_analysis = EXCLUDED.immediate_analysis,
            enriched_analysis = EXCLUDED.enriched_analysis,
            enriched_processed_at = EXCLUDED.enriched_processed_at
        RETURNING id
    """, (
        company_id,
        contact_id,
        hubspot_company_id,
        "demo_synthesia_001",
        datetime.now(timezone.utc),
        847,
        "Jamie Chen",
        territory,
        transcript,
        json.dumps(immediate_analysis),
        json.dumps(enriched_analysis),
        datetime.now(timezone.utc),
        datetime.now(timezone.utc)
    ))
    return cur.fetchone()["id"]


def push_to_hubspot_note(hubspot_company_id: str, enriched_analysis: dict):
    """
    In production this writes back to HubSpot via API.
    For demo we just print what would be written.
    """
    print("\n" + "="*60)
    print("📤 HUBSPOT WRITE-BACK (what would sync automatically)")
    print("="*60)
    print(f"Company ID: {hubspot_company_id}")
    print(f"\n🎯 Account summary:")
    print(f"   {enriched_analysis.get('account_summary')}")
    print(f"\n💡 Recommended outreach angle:")
    print(f"   {enriched_analysis.get('recommended_outreach_angle')}")
    print(f"\n📋 Next steps for rep:")
    for step in enriched_analysis.get("recommended_next_steps", []):
        print(f"   → {step}")
    print(f"\n⚠️  Risk signals:")
    for risk in enriched_analysis.get("risk_signals", []):
        print(f"   ⚠ {risk}")
    print(f"\n🏋️  Coaching note:")
    print(f"   {enriched_analysis.get('coaching_note')}")


def run_test():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    print("🔍 Step 1: Loading company context from Supabase...")
    company = get_company_context(cur, "synthesia.io")
    if not company:
        print("❌ Synthesia not found in Supabase. Run ingest + enrich first.")
        return
    print(f"   ✅ Found: {company['name']} | ICP: {company['icp_score']}/100")
    
    print("\n📞 Step 2: Loading call history...")
    history = get_call_history(cur, str(company["id"]))
    print(f"   ✅ {len(history)} previous calls on record")
    
    print("\n⚡ Step 3: Running immediate analysis (fires within minutes of call)...")
    immediate = run_immediate_analysis(SAMPLE_TRANSCRIPT, company)
    print(f"   ✅ Outcome: {immediate.get('call_outcome')}")
    print(f"   ✅ Next meeting: {immediate.get('next_meeting_with')}")
    print(f"   ✅ Champion: {immediate.get('champion_name')} ({immediate.get('champion_role')})")
    print(f"   ✅ Next step: {immediate.get('immediate_next_step')}")
    
    print("\n🧠 Step 4: Running enriched analysis (fires 24hrs later in production)...")
    enriched = run_enriched_analysis(
        SAMPLE_TRANSCRIPT, company, history, immediate
    )
    print(f"   ✅ Deal stage: {enriched.get('deal_stage_assessment')}")
    print(f"   ✅ Pain points: {len(enriched.get('pain_points_identified', []))} identified")
    print(f"   ✅ Outreach angle: {enriched.get('recommended_outreach_angle')}")
    
    print("\n💾 Step 5: Storing call record in Supabase...")
    
    # Get contact_id — you'll need to replace this with the actual UUID
    cur.execute("""
        SELECT id FROM contacts 
        WHERE company_id = %s 
        LIMIT 1
    """, (str(company["id"]),))
    contact_row = cur.fetchone()
    contact_id = str(contact_row["id"]) if contact_row else None
    
    call_id = store_call_record(
        cur,
        str(company["id"]),
        contact_id,
        company.get("hubspot_company_id"),
        company.get("territory_tag", "uk"),
        SAMPLE_TRANSCRIPT,
        immediate,
        enriched
    )
    conn.commit()
    print(f"   ✅ Call record stored: {call_id}")
    
    print("\n📤 Step 6: Simulating HubSpot write-back...")
    push_to_hubspot_note(company.get("hubspot_company_id"), enriched)
    
    print("\n" + "="*60)
    print("✅ CONVERSATION INTELLIGENCE PIPELINE — COMPLETE")
    print("="*60)
    print("\nIn production this entire flow runs automatically:")
    print("Granola webhook → n8n → immediate analysis → HubSpot")
    print("                        ↓ 24hr wait")
    print("               enriched analysis → HubSpot + Notion")
    
    cur.close()
    conn.close()


if __name__ == "__main__":
    run_test()
