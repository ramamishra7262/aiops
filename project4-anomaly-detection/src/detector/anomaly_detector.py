"""
anomaly_detector.py
Real-time metric anomaly detection pipeline using Azure Anomaly Detector API.
Processes metric streams from Azure Event Hub, detects anomalies,
and enriches them with contextual severity scoring.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from azure.ai.anomalydetector import AnomalyDetectorClient
from azure.ai.anomalydetector.models import (
    TimeSeriesPoint, DetectChangePointRequest, TimeGranularity,
    UnivariateDetectionOptions, UnivariateLastDetectionResult,
)
from azure.core.credentials import AzureKeyCredential

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    metric_name: str
    resource_id: str
    timestamp: datetime
    value: float
    unit: str = ""
    labels: dict = field(default_factory=dict)


@dataclass
class AnomalyEvent:
    metric_name: str
    resource_id: str
    timestamp: str
    current_value: float
    expected_value: float
    deviation_pct: float
    is_anomaly: bool
    is_negative_anomaly: bool   # spike (bad) vs dip (possibly also bad)
    severity_score: float       # 0.0 – 1.0
    upper_margin: float
    lower_margin: float
    anomaly_type: str           # "spike", "dip", "level_shift", "trend_change"
    context: dict = field(default_factory=dict)


class AzureAnomalyDetectorService:
    """
    Wraps Azure Anomaly Detector REST API.
    Uses both:
    - detect_univariate_last_point: real-time single-point detection
    - detect_univariate_change_point: detects level shifts and trend changes
    """

    def __init__(self):
        endpoint   = os.environ["ANOMALY_DETECTOR_ENDPOINT"]
        api_key    = os.environ["ANOMALY_DETECTOR_KEY"]
        self.client = AnomalyDetectorClient(endpoint, AzureKeyCredential(api_key))

    def detect(self, metric_name: str, series: list[MetricPoint]) -> Optional[AnomalyEvent]:
        if len(series) < 12:    # need minimum 12 points for detection
            return None

        ts_points = [
            TimeSeriesPoint(timestamp=p.timestamp, value=p.value)
            for p in series
        ]

        options = UnivariateDetectionOptions(
            series=ts_points,
            granularity=TimeGranularity.FIVE_MINUTES,
            sensitivity=85,     # 85 = moderate; 99 = very sensitive
            max_anomaly_ratio=0.25,
        )

        try:
            result: UnivariateLastDetectionResult = self.client.detect_univariate_last_point(options)

            if not result.is_anomaly:
                return None

            latest = series[-1]
            expected = result.expected_value or latest.value
            deviation = abs(latest.value - expected) / max(expected, 0.001) * 100

            anomaly_type = self._classify_anomaly(latest.value, expected,
                                                   result.upper_margin, result.lower_margin)

            return AnomalyEvent(
                metric_name=metric_name,
                resource_id=series[-1].resource_id,
                timestamp=latest.timestamp.isoformat() + "Z",
                current_value=latest.value,
                expected_value=round(expected, 4),
                deviation_pct=round(deviation, 2),
                is_anomaly=True,
                is_negative_anomaly=result.is_negative_anomaly,
                severity_score=self._compute_severity(deviation, metric_name),
                upper_margin=result.upper_margin or 0,
                lower_margin=result.lower_margin or 0,
                anomaly_type=anomaly_type,
            )
        except Exception as e:
            logger.error(f"Anomaly detection failed for {metric_name}: {e}")
            return None

    def _classify_anomaly(self, value, expected, upper, lower) -> str:
        if value > (expected + upper) * 1.5:
            return "spike"
        elif value < (expected - abs(lower)) * 0.5:
            return "dip"
        elif abs(value - expected) / max(expected, 0.001) > 0.3:
            return "level_shift"
        return "trend_change"

    def _compute_severity(self, deviation_pct: float, metric_name: str) -> float:
        """Higher weight for customer-facing metrics."""
        weights = {
            "Http5xx": 1.5, "AverageResponseTime": 1.3,
            "Percentage CPU": 1.0, "MemoryWorkingSet": 0.9,
            "DiskUsage": 0.7,
        }
        weight = weights.get(metric_name, 1.0)
        raw = min(deviation_pct / 100.0, 1.0) * weight
        return round(min(raw, 1.0), 3)
