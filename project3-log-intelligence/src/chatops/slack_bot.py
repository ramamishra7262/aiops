"""
slack_bot.py
Azure Function HTTP trigger serving as a Slack slash-command bot.
Engineers type /logs <question> in Slack and get instant AI-powered log analysis.

/logs why is the backend pod restarting?
/logs show me the top 10 errors in the last hour
/logs what happened to the database at 14:30?
/logs generate postmortem for INC-2024-001
"""
import azure.functions as func
import json
import logging
import os
import hmac
import hashlib
import time
from urllib.parse import parse_qs
from ..log_analyzer.nl_log_query import NaturalLanguageLogAnalyzer
from ..postmortem.postmortem_generator import PostMortemGenerator, IncidentData

logger = logging.getLogger(__name__)
app = func.FunctionApp()

analyzer   = NaturalLanguageLogAnalyzer()
pm_gen     = PostMortemGenerator()


@app.function_name("SlackLogBot")
@app.route(route="slack/logs", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def slack_log_bot(req: func.HttpRequest) -> func.HttpResponse:
    """Handle Slack slash command /logs <question>."""

    # Verify Slack request signature
    if not _verify_slack_signature(req):
        return func.HttpResponse("Unauthorized", status_code=401)

    body = parse_qs(req.get_body().decode())
    text    = body.get("text",        [""])[0].strip()
    user_id = body.get("user_id",     ["unknown"])[0]
    channel = body.get("channel_id",  [""])[0]

    if not text:
        return _slack_response({"text": "Usage: `/logs <your question about the system>`\n"
                                        "Example: `/logs why are pods crashing in prod?`"})

    # Acknowledge immediately (Slack requires <3s response)
    # In production: send immediate ack, post actual answer asynchronously
    _send_thinking_message(channel, user_id, text)

    try:
        if text.lower().startswith("generate postmortem"):
            incident_id = text.split()[-1] if len(text.split()) > 2 else "UNKNOWN"
            response_text = _handle_postmortem(incident_id)
        else:
            result = analyzer.query(text)
            response_text = _format_log_result(result)
    except Exception as e:
        logger.exception(f"Bot error: {e}")
        response_text = f"❌ Error processing query: {str(e)}"

    return _slack_response({"text": response_text, "response_type": "in_channel"})


def _format_log_result(result) -> str:
    lines = [
        f"*🔍 Question:* {result.question}",
        f"*📊 Results:* {result.row_count} rows found",
        "",
        f"*🤖 Analysis:*\n{result.explanation}",
        "",
        f"*KQL Used:*\n```{result.kql_query}```",
    ]
    if result.suggested_followups:
        lines.append("\n*💡 Suggested follow-up questions:*")
        for i, q in enumerate(result.suggested_followups, 1):
            lines.append(f"  {i}. `/logs {q}`")
    return "\n".join(lines)


def _handle_postmortem(incident_id: str) -> str:
    # In production: fetch incident from ITSM / Log Analytics
    return f"📋 Post-mortem generation for `{incident_id}` started. Will post to #incidents-postmortems shortly."


def _send_thinking_message(channel: str, user_id: str, text: str):
    """Send immediate acknowledgement via Slack API."""
    import requests
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if slack_token:
        requests.post(
            "https://slack.com/api/chat.postEphemeral",
            headers={"Authorization": f"Bearer {slack_token}"},
            json={"channel": channel, "user": user_id, "text": f"⏳ Analysing: _{text}_ ..."},
            timeout=3,
        )


def _verify_slack_signature(req: func.HttpRequest) -> bool:
    """Verify the request came from Slack using HMAC-SHA256."""
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "").encode()
    if not signing_secret:
        logger.warning("SLACK_SIGNING_SECRET not set — skipping verification (dev mode)")
        return True

    timestamp   = req.headers.get("X-Slack-Request-Timestamp", "")
    signature   = req.headers.get("X-Slack-Signature", "")
    body        = req.get_body().decode()

    # Replay attack protection: reject requests older than 5 minutes
    if abs(time.time() - float(timestamp)) > 300:
        return False

    base_str  = f"v0:{timestamp}:{body}".encode()
    expected  = "v0=" + hmac.new(signing_secret, base_str, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _slack_response(payload: dict) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload),
        mimetype="application/json",
        status_code=200,
    )
