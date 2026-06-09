"""
function_app.py
Azure Function (HTTP trigger) that receives Azure Monitor alert webhooks,
runs AI-powered RCA, executes auto-remediation, and posts results to Slack.
"""
import azure.functions as func
import json
import logging
import os
import requests
from src.incident_detector.alert_processor import AlertProcessor
from src.openai_rca.rca_engine import RCAEngine
from src.remediation.remediation_engine import RemediationEngine
from src.incident_detector.models import RemediationStatus

logger = logging.getLogger(__name__)
app = func.FunctionApp()

alert_processor  = AlertProcessor()
rca_engine       = RCAEngine()
remediation_engine = RemediationEngine()


@app.function_name("AIOpsIncidentHandler")
@app.route(route="incident", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def incident_handler(req: func.HttpRequest) -> func.HttpResponse:
    """Main webhook handler for Azure Monitor alerts."""
    logger.info("AIOps Incident Handler triggered")

    try:
        payload = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON payload", status_code=400)

    try:
        # 1. Enrich alert with logs + metrics
        enriched = alert_processor.process(payload)

        # 2. AI-powered root cause analysis
        rca = rca_engine.analyse(enriched)
        logger.info(f"RCA complete: {rca.root_cause} (confidence: {rca.confidence:.0%})")

        # 3. Auto-remediation if safe to do so
        remediation = remediation_engine.remediate(rca)
        logger.info(f"Remediation status: {remediation.status}")

        # 4. Post full report to Slack
        _post_to_slack(enriched, rca, remediation)

        # 5. Log incident to custom Log Analytics table for future training
        _log_incident(enriched, rca, remediation)

        response = {
            "alert_id":         enriched.alert.id,
            "root_cause":       rca.root_cause,
            "confidence":       rca.confidence,
            "auto_remediable":  rca.auto_remediable,
            "remediation_status": remediation.status.value,
            "actions_taken":    remediation.actions_taken,
        }
        return func.HttpResponse(json.dumps(response), mimetype="application/json", status_code=200)

    except Exception as e:
        logger.exception(f"Incident processing failed: {e}")
        return func.HttpResponse(f"Internal error: {str(e)}", status_code=500)


def _post_to_slack(enriched, rca, remediation):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return

    severity_emoji = {"Sev0": "🚨", "Sev1": "🔴", "Sev2": "🟠", "Sev3": "🟡", "Sev4": "🟢"}
    emoji = severity_emoji.get(enriched.alert.severity, "⚠️")

    status_emoji = {
        RemediationStatus.SUCCESS: "✅ Auto-remediated",
        RemediationStatus.FAILED:  "❌ Auto-remediation failed",
        RemediationStatus.SKIPPED: "👤 Manual action required",
        RemediationStatus.PENDING: "⏳ Pending",
    }

    actions_text = "\n".join(f"  • {a}" for a in remediation.actions_taken[:5]) or "  None taken"

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} AIOps Incident: {enriched.alert.name}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Severity:*\n{enriched.alert.severity}"},
                    {"type": "mrkdwn", "text": f"*Confidence:*\n{rca.confidence:.0%}"},
                    {"type": "mrkdwn", "text": f"*Impact:*\n{rca.estimated_impact}"},
                    {"type": "mrkdwn", "text": f"*Remediation:*\n{status_emoji.get(remediation.status, '❓')}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Root Cause:*\n{rca.root_cause}"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Contributing Factors:*\n" +
                         "\n".join(f"  • {f}" for f in rca.contributing_factors[:4])}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Actions Taken:*\n{actions_text}"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Resource:*\n`{enriched.alert.resource_id}`"}
            }
        ]
    }

    try:
        requests.post(webhook_url, json=message, timeout=5)
    except Exception as e:
        logger.warning(f"Slack post failed: {e}")


def _log_incident(enriched, rca, remediation):
    """Custom Log Analytics ingestion for future ML training."""
    import datetime
    record = {
        "TimeGenerated":       datetime.datetime.utcnow().isoformat() + "Z",
        "AlertId":             enriched.alert.id,
        "AlertName":           enriched.alert.name,
        "Severity":            str(enriched.alert.severity),
        "ResourceType":        enriched.alert.resource_type,
        "RootCause":           rca.root_cause,
        "Confidence":          rca.confidence,
        "WasAutoRemediated":   remediation.status == RemediationStatus.SUCCESS,
        "RemediationRunbook":  remediation.runbook_name,
        "MeanTimeToResolve":   remediation.duration_seconds,
    }
    logger.info(f"Incident record: {json.dumps(record)}")
    # In production: use Azure Monitor Ingestion API to write to custom table
