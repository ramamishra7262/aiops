"""Tests for the RCA engine."""
import json
import pytest
from unittest.mock import MagicMock, patch
from src.openai_rca.rca_engine import RCAEngine
from src.incident_detector.models import Alert, AlertSeverity, RCAResult
from src.incident_detector.alert_processor import EnrichedAlert


def make_enriched_alert():
    alert = Alert(
        id="test-alert-001",
        name="HighMemoryUsage",
        severity=AlertSeverity.SEV2,
        description="Memory usage exceeded 90%",
        fired_at="2024-01-15T10:30:00Z",
        resource_id="/subscriptions/xxx/resourceGroups/rg-prod/providers/Microsoft.ContainerService/managedClusters/aks-prod",
        resource_type="Microsoft.ContainerService/managedClusters",
        monitor_condition="Fired",
        signal_type="Metric",
    )
    return EnrichedAlert(
        alert=alert,
        recent_logs=[
            {"TimeGenerated": "2024-01-15T10:28:00Z", "Message": "OOMKilled: backend-pod-abc", "SeverityLevel": 3},
            {"TimeGenerated": "2024-01-15T10:27:00Z", "Message": "Container memory limit reached", "SeverityLevel": 2},
        ],
        related_metrics={"metrics": [{"MetricName": "MemoryWorkingSet", "avg_val": 950, "max_val": 1024}]},
        affected_resources=["/subscriptions/xxx/resourceGroups/rg-prod"],
        similar_past_incidents=[
            {"AlertName": "HighMemoryUsage", "RootCause": "Memory leak in backend v1.2.3", "Resolution": "Restart pod"}
        ],
    )


@patch.dict("os.environ", {
    "AZURE_OPENAI_ENDPOINT": "https://oai-test.openai.azure.com/",
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
})
def test_rca_parse_valid_response():
    engine = RCAEngine()
    mock_response = json.dumps({
        "root_cause": "Pod OOMKilled due to memory leak in backend container",
        "confidence": 0.92,
        "contributing_factors": ["Memory limit set to 512Mi", "Gradual leak over 2h"],
        "recommended_actions": [
            {"priority": 1, "action": "Restart pod", "command": "kubectl rollout restart deployment/backend -n prod", "risk": "low"}
        ],
        "auto_remediable": True,
        "remediation_runbook": "restart-oom-pod",
        "estimated_impact": "~2% of requests failing for 5 minutes",
        "post_mortem_summary": "Backend OOMKilled due to memory leak. Auto-restarted successfully.",
    })

    result = engine._parse_response("test-alert-001", mock_response)

    assert result.root_cause == "Pod OOMKilled due to memory leak in backend container"
    assert result.confidence == 0.92
    assert result.auto_remediable is True
    assert result.remediation_runbook == "restart-oom-pod"
    assert len(result.contributing_factors) == 2


def test_rca_parse_invalid_json():
    engine = RCAEngine.__new__(RCAEngine)
    result = engine._parse_response("test-001", "not json {{{")
    assert result.confidence == 0.0
    assert result.auto_remediable is False
    assert "manual investigation" in result.root_cause


def test_remediation_skipped_low_confidence():
    from src.remediation.remediation_engine import RemediationEngine
    from src.incident_detector.models import RCAResult, RemediationStatus

    rca = RCAResult(
        alert_id="test-001",
        root_cause="Unknown",
        confidence=0.45,   # below threshold
        contributing_factors=[],
        recommended_actions=[],
        auto_remediable=True,
        remediation_runbook="restart-oom-pod",
        estimated_impact="Unknown",
    )
    engine = RemediationEngine()
    result = engine.remediate(rca)
    assert result.status == RemediationStatus.SKIPPED
    assert "threshold" in result.output
