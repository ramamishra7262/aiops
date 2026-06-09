"""
function_app.py
Azure Function — Event Hub trigger.
Processes metric batches from Azure Monitor → Event Hub → anomaly detection pipeline.
"""
import azure.functions as func
import json
import logging
from src.detector.anomaly_detector import AzureAnomalyDetectorService, MetricPoint
from src.correlator.alert_correlator import AlertCorrelator
from src.ticketing.auto_ticket import AutoTicketCreator
from datetime import datetime

logger = logging.getLogger(__name__)
app = func.FunctionApp()

detector   = AzureAnomalyDetectorService()
correlator = AlertCorrelator()
ticketer   = AutoTicketCreator()

# In-memory metric buffer (use Redis in production for multi-instance Functions)
_metric_buffer: dict[str, list[MetricPoint]] = {}


@app.function_name("AnomalyDetectionPipeline")
@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="aiops-metrics",
    connection="EventHubConnection",
    consumer_group="anomaly-detector",
    cardinality=func.Cardinality.MANY,   # batch processing
)
def anomaly_pipeline(events: list[func.EventHubEvent]) -> None:
    """Process a batch of metric events from Event Hub."""
    logger.info(f"Processing {len(events)} metric events")
    new_anomalies = []

    for event in events:
        try:
            payload = json.loads(event.get_body().decode())
            point = MetricPoint(
                metric_name=payload["metricName"],
                resource_id=payload["resourceId"],
                timestamp=datetime.fromisoformat(payload["timestamp"].rstrip("Z")),
                value=float(payload["value"]),
                unit=payload.get("unit", ""),
                labels=payload.get("labels", {}),
            )

            key = f"{point.resource_id}:{point.metric_name}"
            if key not in _metric_buffer:
                _metric_buffer[key] = []
            _metric_buffer[key].append(point)

            # Keep last 288 points (24h at 5-min intervals)
            _metric_buffer[key] = _metric_buffer[key][-288:]

            # Detect anomaly if enough history
            if len(_metric_buffer[key]) >= 12:
                anomaly = detector.detect(point.metric_name, _metric_buffer[key])
                if anomaly:
                    logger.warning(
                        f"Anomaly: {anomaly.metric_name} on {anomaly.resource_id} "
                        f"({anomaly.deviation_pct:.1f}% deviation, type: {anomaly.anomaly_type})"
                    )
                    correlator.add(anomaly)
                    new_anomalies.append(anomaly)

        except Exception as e:
            logger.error(f"Event processing error: {e}")

    # Correlate and deduplicate
    if new_anomalies:
        incidents = correlator.flush()
        for incident in incidents:
            logger.info(
                f"Incident {incident.incident_id}: {len(incident.anomalies)} anomalies correlated "
                f"(severity: {incident.severity_score:.2f}, customer-impacting: {incident.is_customer_impacting})"
            )
            # Auto-create ticket for customer-impacting incidents
            ticket = ticketer.create_ticket(incident)
            if ticket:
                logger.info(f"Created ADO ticket: {ticket.ticket_id} — {ticket.url}")
