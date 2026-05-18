from flask import Flask, request, jsonify
import json
from immediate_analysis import run_immediate_analysis

app = Flask(__name__)

@app.route('/webhook/granola', methods=['POST'])
def granola_webhook():
    data = request.json
    meeting_id = data.get('meeting_id')
    transcript = data.get('transcript')
    
    # Store raw transcript immediately
    store_call_record(meeting_id, transcript, data)
    
    # Fire immediate analysis (lightweight)
    run_immediate_analysis(meeting_id)
    
    return jsonify({"status": "received"}), 200
