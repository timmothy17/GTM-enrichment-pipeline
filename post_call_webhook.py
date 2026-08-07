"""
post_call_webhook.py

HTTP entry point for the conversation intelligence pipeline. Granola (or Gong)
posts a finished call here; this resolves the account from the domain, runs the
immediate analysis tier, and persists the record. The enriched tier runs
separately about 24 hours later and updates the same row.

Run with: flask --app post_call_webhook run
"""

from flask import Flask, request, jsonify
from psycopg2.extras import RealDictCursor

from conversation_intelligence_demo import (
    get_connection,
    get_company_context,
    run_immediate_analysis,
    store_call_record,
)

app = Flask(__name__)


@app.route('/webhook/granola', methods=['POST'])
def granola_webhook():
    data = request.get_json(silent=True) or {}
    meeting_id = data.get('meeting_id')
    transcript = data.get('transcript')
    domain = data.get('domain')

    if not (meeting_id and transcript and domain):
        return jsonify({
            "error": "meeting_id, transcript and domain are all required"
        }), 400

    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        company = get_company_context(cur, domain)
        if not company:
            return jsonify({"error": f"unknown domain: {domain}"}), 404

        immediate = run_immediate_analysis(transcript, company)

        cur.execute(
            "SELECT id FROM contacts WHERE company_id = %s LIMIT 1",
            (str(company["id"]),)
        )
        contact_row = cur.fetchone()

        call_id = store_call_record(
            cur,
            company_id=str(company["id"]),
            contact_id=str(contact_row["id"]) if contact_row else None,
            hubspot_company_id=company.get("hubspot_company_id"),
            territory=company.get("territory_tag", "other"),
            transcript=transcript,
            immediate_analysis=immediate,
            enriched_analysis=None,
            meeting_id=meeting_id,
            duration_seconds=data.get("duration_seconds"),
            rep_name=data.get("rep_name"),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

    return jsonify({"status": "received", "call_id": str(call_id)}), 200


if __name__ == "__main__":
    app.run(port=5000)
