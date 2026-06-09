"""
rca_engine.py
Uses Azure OpenAI GPT-4 to analyse enriched alert context and generate:
- Root cause analysis
- Confidence score
- Recommended remediation actions
- Auto-remediation decision
"""
import json
import logging
import os
import time
from openai import AzureOpenAI
from ..incident_detector.models import RCAResult
from ..incident_detector.alert_processor import EnrichedAlert

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert Azure SRE (Site Reliability Engineer) with deep knowledge of:
- Azure Kubernetes Service (AKS), App Service, Azure SQL, Storage, and networking
- Prometheus/Grafana metrics and alerting patterns
- Common failure modes: OOMKilled pods, CPU throttling, connection pool exhaustion,
  disk pressure, network timeouts, certificate expiry, deployment rollout failures
- Incident response and root cause analysis

Your task: Given an Azure Monitor alert + recent logs + metrics, produce a structured JSON analysis.
Be concise, specific, and actionable. Base your analysis ONLY on the provided data.
Output ONLY valid JSON matching the schema below — no markdown, no prose outside the JSON.

Schema:
{
  "root_cause": "Single clear sentence describing the most likely root cause",
  "confidence": 0.85,
  "contributing_factors": ["factor1", "factor2"],
  "recommended_actions": [
    {"priority": 1, "action": "...", "command": "kubectl ...", "risk": "low"},
    {"priority": 2, "action": "...", "command": "az ...", "risk": "medium"}
  ],
  "auto_remediable": true,
  "remediation_runbook": "restart-oom-pod",
  "estimated_impact": "~5% of requests returning 503 for ~10 minutes",
  "post_mortem_summary": "Two-sentence summary for incident ticket"
}"""


class RCAEngine:
    """
    Sends enriched alert context to Azure OpenAI GPT-4 and parses RCA output.
    Uses exponential backoff for rate-limit handling.
    """

    def __init__(self):
        self.client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version="2024-02-01",
        )
        self.deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        self.max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "2000"))

    def analyse(self, enriched: EnrichedAlert) -> RCAResult:
        user_prompt = self._build_prompt(enriched)
        logger.info(f"Sending RCA request for alert: {enriched.alert.id}")

        response = self._call_with_retry(user_prompt)
        return self._parse_response(enriched.alert.id, response)

    def _build_prompt(self, enriched: EnrichedAlert) -> str:
        alert = enriched.alert
        log_sample = enriched.recent_logs[:20]   # limit tokens
        metric_sample = enriched.related_metrics.get("metrics", [])[:15]

        return f"""## Alert Details
- Name: {alert.name}
- Severity: {alert.severity}
- Resource: {alert.resource_id}
- Resource Type: {alert.resource_type}
- Fired At: {alert.fired_at}
- Description: {alert.description}
- Signal Type: {alert.signal_type}

## Recent Error Logs (last 30 minutes, most recent first)
```json
{json.dumps(log_sample, indent=2, default=str)}
```

## Related Metrics (last 1 hour)
```json
{json.dumps(metric_sample, indent=2, default=str)}
```

## Similar Past Incidents (last 30 days)
```json
{json.dumps(enriched.similar_past_incidents, indent=2, default=str)}
```

## Affected Resources
{json.dumps(enriched.affected_resources)}

Analyse the above and return your RCA JSON."""

    def _call_with_retry(self, prompt: str, retries: int = 3) -> str:
        for attempt in range(retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.deployment,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    max_tokens=self.max_tokens,
                    temperature=0.1,    # low temp for deterministic RCA
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content
            except Exception as e:
                wait = 2 ** attempt
                logger.warning(f"OpenAI call failed (attempt {attempt+1}): {e}. Retrying in {wait}s")
                if attempt < retries - 1:
                    time.sleep(wait)
                else:
                    raise

    def _parse_response(self, alert_id: str, raw: str) -> RCAResult:
        try:
            data = json.loads(raw)
            return RCAResult(
                alert_id=alert_id,
                root_cause=data.get("root_cause", "Unknown"),
                confidence=float(data.get("confidence", 0.5)),
                contributing_factors=data.get("contributing_factors", []),
                recommended_actions=data.get("recommended_actions", []),
                auto_remediable=bool(data.get("auto_remediable", False)),
                remediation_runbook=data.get("remediation_runbook"),
                estimated_impact=data.get("estimated_impact", "Unknown"),
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OpenAI response: {e}\nRaw: {raw}")
            return RCAResult(
                alert_id=alert_id,
                root_cause="RCA parsing failed — manual investigation required",
                confidence=0.0,
                contributing_factors=[],
                recommended_actions=[],
                auto_remediable=False,
                remediation_runbook=None,
                estimated_impact="Unknown",
            )
