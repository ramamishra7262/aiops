"""
alert_processor.py
Receives Azure Monitor webhook alerts, enriches them with Log Analytics context,
then triggers OpenAI-powered root cause analysis and automated remediation.
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from .models import Alert, AlertSeverity

logger = logging.getLogger(__name__)


@dataclass
class EnrichedAlert:
    alert: Alert
    recent_logs: list[dict]
    related_metrics: dict
    affected_resources: list[str]
    similar_past_incidents: list[dict]


class AlertProcessor:
    """
    Processes incoming Azure Monitor alerts:
    1. Parses and validates the alert payload
    2. Fetches last 30-minute logs from Log Analytics
    3. Correlates with recent metric spikes
    4. Returns an EnrichedAlert for RCA
    """

    def __init__(self):
        self.credential = DefaultAzureCredential()
        self.logs_client = LogsQueryClient(self.credential)
        self.workspace_id = os.environ["LOG_ANALYTICS_WORKSPACE_ID"]
        self.kv_url = os.environ.get("KEY_VAULT_URL", "")

    def process(self, raw_payload: dict) -> EnrichedAlert:
        alert = self._parse_alert(raw_payload)
        logger.info(f"Processing alert: {alert.name} | severity: {alert.severity}")

        recent_logs = self._fetch_recent_logs(alert)
        metrics = self._fetch_related_metrics(alert)
        resources = self._extract_affected_resources(alert)
        similar = self._find_similar_incidents(alert)

        return EnrichedAlert(
            alert=alert,
            recent_logs=recent_logs,
            related_metrics=metrics,
            affected_resources=resources,
            similar_past_incidents=similar,
        )

    def _parse_alert(self, payload: dict) -> Alert:
        data = payload.get("data", {})
        essentials = data.get("essentials", {})
        return Alert(
            id=essentials.get("alertId", "unknown"),
            name=essentials.get("alertRule", "unknown"),
            severity=AlertSeverity(essentials.get("severity", "Sev2")),
            description=essentials.get("description", ""),
            fired_at=essentials.get("firedDateTime", ""),
            resource_id=essentials.get("targetResourceIds", [""])[0],
            resource_type=essentials.get("targetResourceType", ""),
            monitor_condition=essentials.get("monitorCondition", "Fired"),
            signal_type=essentials.get("signalType", "Metric"),
        )

    def _fetch_recent_logs(self, alert: Alert) -> list[dict]:
        """Query last 30 minutes of error/warning logs for affected resource."""
        query = f"""
        union
            AppExceptions,
            AppTraces,
            AzureActivity,
            ContainerLog
        | where TimeGenerated > ago(30m)
        | where SeverityLevel >= 2 or Level in ('Error','Warning','Critical')
        | where _ResourceId =~ @"{alert.resource_id}" or isempty(_ResourceId)
        | project TimeGenerated, Type, Message, SeverityLevel,
                  OperationName, ResourceGroup, _ResourceId
        | order by TimeGenerated desc
        | take 50
        """
        try:
            result = self.logs_client.query_workspace(
                workspace_id=self.workspace_id,
                query=query,
                timespan=("PT30M"),
            )
            if result.status == LogsQueryStatus.SUCCESS:
                rows = result.tables[0].rows if result.tables else []
                cols = result.tables[0].columns if result.tables else []
                return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            logger.warning(f"Log fetch failed: {e}")
        return []

    def _fetch_related_metrics(self, alert: Alert) -> dict:
        """Summarise recent metric trends from KQL."""
        query = """
        AzureMetrics
        | where TimeGenerated > ago(1h)
        | where MetricName in ('Percentage CPU','Http5xx','MemoryWorkingSet',
                               'RequestCount','AverageResponseTime')
        | summarize
            avg_val = avg(Average),
            max_val = max(Maximum),
            trend = series_slope(makelist(Average))
          by MetricName, bin(TimeGenerated, 5m)
        | order by TimeGenerated desc
        """
        try:
            result = self.logs_client.query_workspace(
                workspace_id=self.workspace_id,
                query=query,
                timespan=("PT1H"),
            )
            if result.status == LogsQueryStatus.SUCCESS and result.tables:
                rows = result.tables[0].rows
                cols = result.tables[0].columns
                return {"metrics": [dict(zip(cols, r)) for r in rows]}
        except Exception as e:
            logger.warning(f"Metrics fetch failed: {e}")
        return {}

    def _extract_affected_resources(self, alert: Alert) -> list[str]:
        return [alert.resource_id] if alert.resource_id else []

    def _find_similar_incidents(self, alert: Alert) -> list[dict]:
        """Find similar past incidents in Log Analytics (incident history table)."""
        query = f"""
        AIOpsIncidentHistory
        | where TimeGenerated > ago(30d)
        | where AlertName =~ @"{alert.name}"
            or ResourceType =~ @"{alert.resource_type}"
        | project TimeGenerated, AlertName, RootCause, Resolution,
                  MeanTimeToResolve, WasAutoRemediated
        | order by TimeGenerated desc
        | take 5
        """
        try:
            result = self.logs_client.query_workspace(
                workspace_id=self.workspace_id,
                query=query,
                timespan=("P30D"),
            )
            if result.status == LogsQueryStatus.SUCCESS and result.tables:
                rows = result.tables[0].rows
                cols = result.tables[0].columns
                return [dict(zip(cols, r)) for r in rows]
        except Exception:
            pass
        return []
