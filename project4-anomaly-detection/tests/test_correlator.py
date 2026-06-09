"""Tests for the alert correlator."""
import pytest
from datetime import datetime
from src.detector.anomaly_detector import AnomalyEvent
from src.correlator.alert_correlator import AlertCorrelator


def make_anomaly(metric, resource_id, severity=0.8, timestamp="2024-01-15T10:30:00Z"):
    return AnomalyEvent(
        metric_name=metric,
        resource_id=resource_id,
        timestamp=timestamp,
        current_value=90.0,
        expected_value=50.0,
        deviation_pct=80.0,
        is_anomaly=True,
        is_negative_anomaly=True,
        severity_score=severity,
        upper_margin=10.0,
        lower_margin=10.0,
        anomaly_type="spike",
    )


SHARED_RG = "/subscriptions/xxx/resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachineScaleSets/vmss-app"
SHARED_RG2 = "/subscriptions/xxx/resourceGroups/rg-prod/providers/Microsoft.Web/sites/app-prod"


def test_same_resource_correlated():
    correlator = AlertCorrelator()
    a1 = make_anomaly("Percentage CPU", SHARED_RG)
    a2 = make_anomaly("AverageResponseTime", SHARED_RG)
    correlator.add(a1)
    correlator.add(a2)
    incidents = correlator.flush()
    assert len(incidents) == 1
    assert len(incidents[0].anomalies) == 2


def test_different_resources_not_correlated():
    correlator = AlertCorrelator()
    a1 = make_anomaly("Percentage CPU",  "/subscriptions/xxx/resourceGroups/rg-prod/vmss1")
    a2 = make_anomaly("Percentage CPU",  "/subscriptions/xxx/resourceGroups/rg-staging/vmss2")
    correlator.add(a1)
    correlator.add(a2)
    incidents = correlator.flush()
    assert len(incidents) == 2


def test_severity_score_is_max():
    correlator = AlertCorrelator()
    a1 = make_anomaly("Percentage CPU", SHARED_RG, severity=0.5)
    a2 = make_anomaly("Http5xx",        SHARED_RG, severity=0.9)
    correlator.add(a1)
    correlator.add(a2)
    incidents = correlator.flush()
    assert incidents[0].severity_score == 0.9


def test_customer_impacting_detection():
    correlator = AlertCorrelator()
    a = make_anomaly("Http5xx", SHARED_RG, severity=0.8)
    correlator.add(a)
    incidents = correlator.flush()
    assert incidents[0].is_customer_impacting is True


def test_non_customer_facing_not_impacting():
    correlator = AlertCorrelator()
    a = make_anomaly("DiskUsage", SHARED_RG, severity=0.6)
    correlator.add(a)
    incidents = correlator.flush()
    assert incidents[0].is_customer_impacting is False
