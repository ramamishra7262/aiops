"""
alert_correlator.py
Groups related anomalies into correlated incidents to reduce alert noise.
Uses time-window + resource proximity + causal graph correlation.
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from ..detector.anomaly_detector import AnomalyEvent

logger = logging.getLogger(__name__)

# Known causal relationships between metrics
# If A anomalous → B anomaly is likely a symptom, not a separate incident
CAUSAL_GRAPH = {
    "Percentage CPU":     ["AverageResponseTime", "Http5xx", "pod_restarts"],
    "MemoryWorkingSet":   ["Http5xx", "pod_restarts", "GC_pause_time"],
    "DiskUsage":          ["pod_restarts", "log_write_errors"],
    "network_latency":    ["AverageResponseTime", "Http5xx", "dependency_failures"],
    "pod_restarts":       ["Http5xx", "AverageResponseTime"],
}


@dataclass
class CorrelatedIncident:
    incident_id: str
    root_metric: str               # metric that triggered the correlation
    anomalies: list[AnomalyEvent]  # all related anomalies
    resource_ids: list[str]
    start_time: str
    severity_score: float          # max across anomalies
    likely_cause: str
    is_customer_impacting: bool
    suppressed_count: int = 0      # how many noise alerts were suppressed


class AlertCorrelator:
    """
    Receives a stream of AnomalyEvents, groups them into CorrelatedIncidents.
    Correlation window: 10 minutes. Resources within same resource group are co-related.
    """

    CORRELATION_WINDOW_MINUTES = 10
    CUSTOMER_FACING_METRICS = {"Http5xx", "AverageResponseTime", "Availability"}

    def __init__(self):
        self._buffer: list[AnomalyEvent] = []
        self._last_flush = datetime.utcnow()

    def add(self, anomaly: AnomalyEvent):
        self._buffer.append(anomaly)

    def flush(self) -> list[CorrelatedIncident]:
        """Group buffered anomalies into correlated incidents."""
        if not self._buffer:
            return []

        incidents = []
        processed = set()
        buffer = list(self._buffer)
        self._buffer.clear()

        for i, anchor in enumerate(buffer):
            if i in processed:
                continue

            group = [anchor]
            processed.add(i)

            for j, candidate in enumerate(buffer):
                if j in processed or j == i:
                    continue
                if self._are_correlated(anchor, candidate):
                    group.append(candidate)
                    processed.add(j)

            incident = self._build_incident(group)
            incidents.append(incident)

        suppressed = len(buffer) - len(incidents)
        if suppressed > 0:
            logger.info(f"Correlation reduced {len(buffer)} anomalies → {len(incidents)} incidents ({suppressed} suppressed)")

        return incidents

    def _are_correlated(self, a: AnomalyEvent, b: AnomalyEvent) -> bool:
        # Same resource
        if a.resource_id == b.resource_id:
            return True

        # Same resource group (Azure resource IDs share prefix up to RG name)
        rg_a = self._extract_rg(a.resource_id)
        rg_b = self._extract_rg(b.resource_id)
        if rg_a and rg_b and rg_a == rg_b:
            # Within time window
            try:
                ta = datetime.fromisoformat(a.timestamp.rstrip("Z"))
                tb = datetime.fromisoformat(b.timestamp.rstrip("Z"))
                if abs((ta - tb).total_seconds()) <= self.CORRELATION_WINDOW_MINUTES * 60:
                    # Causal relationship?
                    causes = CAUSAL_GRAPH.get(a.metric_name, [])
                    if b.metric_name in causes:
                        return True
            except Exception:
                pass

        return False

    def _build_incident(self, group: list[AnomalyEvent]) -> CorrelatedIncident:
        import uuid
        root = max(group, key=lambda a: a.severity_score)
        resources = list({a.resource_id for a in group})
        customer_impacting = any(
            a.metric_name in self.CUSTOMER_FACING_METRICS for a in group
        )

        causes = CAUSAL_GRAPH.get(root.metric_name, [])
        symptoms = [a.metric_name for a in group if a.metric_name in causes]
        likely_cause = (
            f"Root: {root.metric_name} deviation {root.deviation_pct:.1f}% "
            + (f"→ symptoms: {', '.join(symptoms)}" if symptoms else "")
        )

        return CorrelatedIncident(
            incident_id=f"INC-{uuid.uuid4().hex[:8].upper()}",
            root_metric=root.metric_name,
            anomalies=group,
            resource_ids=resources,
            start_time=min(a.timestamp for a in group),
            severity_score=root.severity_score,
            likely_cause=likely_cause,
            is_customer_impacting=customer_impacting,
            suppressed_count=len(group) - 1,
        )

    def _extract_rg(self, resource_id: str) -> Optional[str]:
        parts = resource_id.lower().split("/")
        try:
            idx = parts.index("resourcegroups")
            return parts[idx + 1]
        except (ValueError, IndexError):
            return None
