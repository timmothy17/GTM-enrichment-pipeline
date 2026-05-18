"""
hubspot_sync.py
Push enriched Supabase companies into HubSpot CRM.
Backfills hubspot_company_id, hubspot_synced, hubspot_synced_at.
"""

import os
import json
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from hubspot import HubSpot
from hubspot.crm.companies import SimplePublicObjectInputForCreate
from hubspot.crm.companies.exceptions import ApiException as CompaniesApiException

load_dotenv()

CONNECTION_STRING = os.getenv("SUPABASE_CONNECTION_STRING")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")

hs = HubSpot(access_token=HUBSPOT_ACCESS_TOKEN)


def get_connection():
    return psycopg2.connect(CONNECTION_STRING)


def to_hubspot_timestamp(value) -> int | None:
    if not value:
        return None
    if isinstance(value, str):
        value = value.replace('Z', '+00:00')
        dt = datetime.datetime.fromisoformat(value)
    elif isinstance(value, datetime.datetime):
        dt = value
    else:
        return None
    
    # Convert to UTC then take midnight
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc)
    
    midnight_utc = datetime.datetime(
        dt.year, dt.month, dt.day,
        0, 0, 0, 0,
        tzinfo=datetime.timezone.utc
    )
    return int(midnight_utc.timestamp() * 1000) 


def find_hubspot_company_by_domain(domain: str) -> str | None:
    """
    Search HubSpot for an existing company by domain.
    Returns the HubSpot company ID if found, else None.
    """
    from hubspot.crm.companies import PublicObjectSearchRequest

    request = PublicObjectSearchRequest(
        filter_groups=[{
            "filters": [{
                "propertyName": "domain",
                "operator": "EQ",
                "value": domain
            }]
        }],
        properties=["domain", "name"],
        limit=1
    )

    try:
        response = hs.crm.companies.search_api.do_search(public_object_search_request=request)
        if response.results:
            return response.results[0].id
    except Exception as e:
        print(f"    ⚠️  HubSpot search error: {e}")
    return None


def build_hubspot_properties(company: dict) -> dict:
    """
    Map Supabase company fields to HubSpot company properties.
    Uses standard fields + custom properties for automation.
    """
    # Parse news_snippets JSON if stored as string
    news = company.get("news_snippets") or {}
    if isinstance(news, str):
        try:
            news = json.loads(news)
        except:
            news = {}

    dm = company.get("decision_makers") or []
    stack = company.get("procurement_stack_detected") or []

    # Get employee count from news snippets
    employees = news.get("estimated_employees")

    # Structured properties for HubSpot automation (create these in HubSpot UI first)
    props = {
        "name": company.get("name") or company.get("domain"),
        "domain": company.get("domain"),
        "description": company.get("website_description", ""),

        # Custom properties - create these in HubSpot first:
        # icp_score (Number), procurement_maturity (Dropdown),
        # competitive_risk (Single-line text), recommended_outreach_angle (Single-line text),
        # news_buying_trigger (Dropdown: yes/no), enrichment_version (Number),
        # enriched_at (Date)
        "icp_score": company.get("icp_score"),
        "procurement_maturity": company.get("procurement_maturity", ""),
        "competitive_risk": company.get("competitive_risk", ""),
        "recommended_outreach_angle": company.get("recommended_outreach_angle", ""),
        "news_buying_trigger": "yes" if company.get("news_buying_trigger") else "no",
        "enrichment_version": company.get("enrichment_version", 2),
        "enriched_at": to_hubspot_timestamp(company.get("website_enriched_at")),
        "competitive_routing": company.get("competitive_routing", "none"),
        "territory_tag": company.get("territory_tag", "other"),
    }

    # Add numberofemployees if we have it
    if employees:
        props["numberofemployees"] = str(employees)

    # Build rich description for human readability
    dm_text = "\n".join([
        f"  {d.get('role')}: {'✅' if d.get('detected') else '❓'} ({d.get('hiring_status', 'unknown')})"
        for d in dm
    ]) if dm else "  No decision maker data"

    stack_text = "\n".join([f"  - {s}" for s in stack]) if stack else "  - None detected"

    icp_score = company.get("icp_score", "N/A")
    maturity = company.get("procurement_maturity", "unknown")
    competitive = company.get("competitive_risk", "unknown")
    angle = company.get("recommended_outreach_angle", "N/A")
    reasoning = company.get("icp_reasoning", "N/A")

    props["description"] = (
        f"{company.get('website_description', '')}\n\n"
        f"🎯 ICP Score: {icp_score}/100\n"
        f"📊 Maturity: {maturity}\n"
        f"⚔️ Competitive Risk: {competitive}\n\n"
        f"👥 Decision Makers:\n{dm_text}\n\n"
        f"🔧 Detected Stack:\n{stack_text}\n\n"
        f"💡 Outreach Angle: {angle}\n\n"
        f"📝 Reasoning: {reasoning[:500]}..."
    )

    return props


def sync_company_to_hubspot(company: dict) -> str | None:
    """
    Upsert a company to HubSpot. Returns HubSpot company ID.
    """
    from hubspot.crm.companies import (
        SimplePublicObjectInputForCreate,
        SimplePublicObjectInput
    )

    domain = company.get("domain")
    if not domain:
        print("    No domain, skipping")
        return None

    hs_id = find_hubspot_company_by_domain(domain)
    props = build_hubspot_properties(company)

    try:
        if hs_id:
            response = hs.crm.companies.basic_api.update(
                company_id=hs_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties=props
                )
            )
            print(f"    Updated existing HubSpot company: {hs_id}")
        else:
            response = hs.crm.companies.basic_api.create(
                simple_public_object_input_for_create=(
                    SimplePublicObjectInputForCreate(
                        properties=props
                    )
                )
            )
            hs_id = response.id
            print(f"    Created new HubSpot company: {hs_id}")

        return hs_id

    except CompaniesApiException as e:
        print(f"    HubSpot API error: {e.body}")
        return None
    except Exception as e:
        print(f"    Unexpected error: {e}")
        return None


def run_hubspot_sync(batch_size: int = 50):
    """
    Sync all enriched companies (v2+) that haven't been synced yet.
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT * FROM companies
        WHERE enrichment_version >= 2
        AND (
            hubspot_synced = false 
            OR hubspot_synced IS NULL
            OR last_synced_version < enrichment_version
        )
        ORDER BY icp_score DESC NULLS LAST
        LIMIT %s
    """, (batch_size,))

    companies = cur.fetchall()
    total = len(companies)

    print(f"🔵 HubSpot Sync: {total} companies to sync")
    print("=" * 60)

    synced = 0
    failed = 0

    for i, company in enumerate(companies, 1):
        cid = company["id"]
        name = company["name"] or company["domain"]
        print(f"\n[{i}/{total}] {name}")

        hs_id = sync_company_to_hubspot(company)

        if hs_id:
            cur.execute("""
                UPDATE companies
                SET hubspot_company_id = %s,
                    hubspot_synced = true,
                    hubspot_synced_at = NOW(),
                    last_synced_version = enrichment_version,
                    updated_at = NOW()
                WHERE id = %s
            """, (hs_id, cid))
            conn.commit()
            synced += 1
        else:
            failed += 1

    cur.close()
    conn.close()

    print(f"\n✅ Synced: {synced} | ❌ Failed: {failed}")


if __name__ == "__main__":
    run_hubspot_sync(batch_size=10)
