"""Data models for AIOps incident detection."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AlertSeverity(str, Enum):
    SEV0 = "Sev0"
    SEV1 = "Sev1"
    SEV2 = "Sev2"
    SEV3 = "Sev3"
    SEV4 = "Sev4"


class RemediationStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCESS   = "success"
    FAILED    = "failed"
    SKIPPED   = "skipped"


@dataclass
class Alert:
    id: str
    name: str
    severity: AlertSeverity
    description: str
    fired_at: str
    resource_id: str
    resource_type: str
    monitor_condition: str
    signal_type: str


@dataclass
class RCAResult:
    alert_id: str
    root_cause: str
    confidence: float          # 0.0 – 1.0
    contributing_factors: list[str]
    recommended_actions: list[str]
    auto_remediable: bool
    remediation_runbook: Optional[str]
    estimated_impact: str
    similar_incident_ids: list[str] = field(default_factory=list)
    tokens_used: int = 0


@dataclass
class RemediationResult:
    alert_id: str
    runbook_name: str
    status: RemediationStatus
    actions_taken: list[str]
    rollback_available: bool
    duration_seconds: float
    output: str
    error: Optional[str] = None
