"""
auto_ticket.py
Automatically creates Azure DevOps work items for customer-impacting incidents.
Uses OpenAI to generate a clear, structured ticket title and description.
"""
import json
import logging
import os
import requests
from dataclasses import dataclass
from openai import AzureOpenAI
from ..correlator.alert_correlator import CorrelatedIncident

logger = logging.getLogger(__name__)

TICKET_PROMPT = """Generate a clear, concise Azure DevOps incident ticket for an AIOps-detected anomaly.

Output JSON with:
{
  "title": "Brief, specific title (under 80 chars)",
  "description": "3-4 sentence description: what anomaly was detected, which resource, what impact",
  "reproduction_steps": ["step1", "step2"],
  "acceptance_criteria": ["criterion1", "criterion2"],
  "severity_tag": "Sev1|Sev2|Sev3",
  "area_path": "Infrastructure\\Monitoring"
}"""


@dataclass
class TicketResult:
    ticket_id: str
    url: str
    title: str
    severity: str


class AutoTicketCreator:

    def __init__(self):
        self.openai = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version="2024-02-01",
        )
        self.deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        self.ado_org    = os.environ["ADO_ORGANIZATION"]
        self.ado_project = os.environ["ADO_PROJECT"]
        self.ado_token  = os.environ["ADO_PAT"]

    def create_ticket(self, incident: CorrelatedIncident) -> Optional[TicketResult]:
        """Only create tickets for customer-impacting, high-severity incidents."""
        if not incident.is_customer_impacting or incident.severity_score < 0.6:
            logger.info(f"Skipping ticket for {incident.incident_id} (not impactful enough)")
            return None

        # Generate ticket content with GPT-4o
        ticket_data = self._generate_ticket_content(incident)

        # Create work item in Azure DevOps
        return self._create_ado_work_item(incident, ticket_data)

    def _generate_ticket_content(self, incident: CorrelatedIncident) -> dict:
        prompt = f"""Incident: {incident.incident_id}
Root Metric: {incident.root_metric}
Likely Cause: {incident.likely_cause}
Severity Score: {incident.severity_score:.2f}
Affected Resources: {', '.join(incident.resource_ids[:3])}
Anomalies Detected: {len(incident.anomalies)}
Customer Impacting: {incident.is_customer_impacting}
Start Time: {incident.start_time}"""

        response = self.openai.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": TICKET_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.2,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)

    def _create_ado_work_item(self, incident: CorrelatedIncident, content: dict) -> Optional[TicketResult]:
        url = (f"https://dev.azure.com/{self.ado_org}/{self.ado_project}/"
               f"_apis/wit/workitems/$Bug?api-version=7.1")
        headers = {
            "Content-Type": "application/json-patch+json",
            "Authorization": f"Basic {self._encode_pat()}",
        }
        patch = [
            {"op": "add", "path": "/fields/System.Title",       "value": content.get("title", incident.incident_id)},
            {"op": "add", "path": "/fields/System.Description", "value": content.get("description", "")},
            {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Severity", "value": content.get("severity_tag", "Sev2")},
            {"op": "add", "path": "/fields/System.Tags",        "value": "AIOps; auto-generated; anomaly-detection"},
            {"op": "add", "path": "/fields/System.AreaPath",    "value": content.get("area_path", self.ado_project)},
        ]
        try:
            resp = requests.post(url, headers=headers, json=patch, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return TicketResult(
                ticket_id=str(data["id"]),
                url=data["_links"]["html"]["href"],
                title=content.get("title", ""),
                severity=content.get("severity_tag", "Sev2"),
            )
        except Exception as e:
            logger.error(f"ADO ticket creation failed: {e}")
            return None

    def _encode_pat(self) -> str:
        import base64
        return base64.b64encode(f":{self.ado_token}".encode()).decode()
