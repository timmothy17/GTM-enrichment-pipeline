import os
import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()

CONNECTION_STRING = os.getenv("SUPABASE_CONNECTION_STRING")

def get_connection():
    return psycopg2.connect(CONNECTION_STRING)

def normalise_row(row):
    company_name = str(row.get("company_name", "") or "").strip().title()
    domain = str(row.get("domain", "") or "").strip().lower()
    first_name = str(row.get("contact_first_name", "") or "").strip()
    last_name = str(row.get("contact_last_name", "") or "").strip()
    email = str(row.get("contact_email", "") or "").strip().lower()
    job_title = str(row.get("job_title", "") or "").strip()

    # Extract domain from email if domain is empty
    if not domain and "@" in email:
        domain = email.split("@")[1].lower()

    return {
        "company_name": company_name,
        "domain": domain,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "job_title": job_title
    }

def run_ingest():
    df = pd.read_csv("test_leads.csv")
    
    conn = get_connection()
    cur = conn.cursor()

    companies_inserted = 0
    companies_skipped = 0
    contacts_inserted = 0
    contacts_skipped = 0

    for _, row in df.iterrows():
        data = normalise_row(row)

        if not data["domain"]:
            print(f"Skipping row — no domain: {data['company_name']}")
            continue

        # Check if company already exists
        cur.execute(
            "SELECT id FROM companies WHERE domain = %s",
            (data["domain"],)
        )
        existing = cur.fetchone()

        if existing:
            print(f"Skipping {data['domain']} — already exists")
            companies_skipped += 1
            company_id = existing[0]
        else:
            cur.execute(
                """
                INSERT INTO companies (name, domain)
                VALUES (%s, %s)
                RETURNING id
                """,
                (data["company_name"], data["domain"])
            )
            company_id = cur.fetchone()[0]
            companies_inserted += 1
            print(f"Inserted {data['domain']}")

        # Insert contact if any contact data exists
        has_contact = any([
            data["first_name"],
            data["last_name"],
            data["email"],
            data["job_title"]
        ])

        if has_contact:
            # Companies were deduped above but contacts were not, so re-running
            # the same CSV used to insert a duplicate contact row every time.
            # Match on email where we have one, otherwise on name within the
            # company.
            if data["email"]:
                cur.execute(
                    "SELECT id FROM contacts WHERE email = %s",
                    (data["email"],)
                )
            else:
                cur.execute(
                    """
                    SELECT id FROM contacts
                    WHERE company_id = %s
                      AND first_name IS NOT DISTINCT FROM %s
                      AND last_name IS NOT DISTINCT FROM %s
                    """,
                    (
                        company_id,
                        data["first_name"] or None,
                        data["last_name"] or None
                    )
                )

            if cur.fetchone():
                contacts_skipped += 1
            else:
                cur.execute(
                    """
                    INSERT INTO contacts
                      (company_id, first_name, last_name, email, job_title)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        company_id,
                        data["first_name"] or None,
                        data["last_name"] or None,
                        data["email"] or None,
                        data["job_title"] or None
                    )
                )
                contacts_inserted += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"\nDone.")
    print(f"Companies inserted: {companies_inserted}")
    print(f"Companies skipped: {companies_skipped}")
    print(f"Contacts inserted: {contacts_inserted}")
    print(f"Contacts skipped: {contacts_skipped}")

if __name__ == "__main__":
    run_ingest()
