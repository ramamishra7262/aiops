"""
postmortem_generator.py
Generates a structured, blameless post-mortem document from incident data
using Azure OpenAI GPT-4o. Outputs Markdown ready for Confluence/Notion/GitHub.
"""
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from openai import AzureOpenAI

logger = logging.getLogger(__name__)

POSTMORTEM_PROMPT = """You are an experienced SRE writing a blameless post-mortem for an Azure infrastructure incident.
Using the provided incident data, generate a complete, professional post-mortem in Markdown format.

The post-mortem must include ALL of these sections:
1. **Incident Summary** — severity, duration, impact (% users, services affected)
2. **Timeline** — chronological bullet list with timestamps (UTC)
3. **Root Cause** — technical root cause, be specific
4. **Contributing Factors** — what made this worse or harder to detect
5. **Impact Analysis** — quantify: error rate, latency increase, affected users/revenue estimate
6. **What Went Well** — detection speed, response, tooling that helped
7. **What Went Wrong** — gaps in monitoring, slow detection, unclear runbooks
8. **Action Items** — specific, assignable tasks with suggested owners and due dates (table format)
9. **Lessons Learned** — 2-3 systemic improvements

Tone: blameless, factual, focused on systems not people.
Format: GitHub-flavoured Markdown. Include a header with incident ID, date, and severity badge."""


@dataclass
class IncidentData:
    incident_id: str
    title: str
    severity: str
    start_time: str
    end_time: str
    affected_services: list[str]
    alert_data: dict
    timeline_events: list[dict]
    rca_result: dict
    remediation_actions: list[str]
    metrics_during_incident: dict


class PostMortemGenerator:

    def __init__(self):
        self.client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version="2024-02-01",
        )
        self.deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    def generate(self, incident: IncidentData) -> str:
        logger.info(f"Generating post-mortem for incident: {incident.incident_id}")

        duration_mins = self._calc_duration(incident.start_time, incident.end_time)
        prompt = f"""## Incident Data

**Incident ID:** {incident.incident_id}
**Title:** {incident.title}
**Severity:** {incident.severity}
**Start:** {incident.start_time} UTC
**End:** {incident.end_time} UTC
**Duration:** {duration_mins} minutes
**Affected Services:** {', '.join(incident.affected_services)}

### AI Root Cause Analysis
```json
{json.dumps(incident.rca_result, indent=2)}
```

### Timeline of Events
```json
{json.dumps(incident.timeline_events, indent=2, default=str)}
```

### Auto-Remediation Actions Taken
{chr(10).join(f'- {a}' for a in incident.remediation_actions)}

### Key Metrics During Incident
```json
{json.dumps(incident.metrics_during_incident, indent=2, default=str)}
```

Generate the complete blameless post-mortem document."""

        response = self.client.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": POSTMORTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.3,
            max_tokens=3000,
        )
        return response.choices[0].message.content

    def _calc_duration(self, start: str, end: str) -> int:
        try:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            s = datetime.strptime(start, fmt)
            e = datetime.strptime(end,   fmt)
            return int((e - s).total_seconds() / 60)
        except Exception:
            return 0
