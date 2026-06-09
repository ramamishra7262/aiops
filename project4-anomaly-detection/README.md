# 🔍 Project 4: AIOps Anomaly Detection & Intelligent Alert Correlation

A real-time AIOps pipeline that ingests metric streams via **Azure Event Hub**, detects anomalies using **Azure Anomaly Detector**, correlates related alerts to reduce noise by up to 80%, and automatically creates **Azure DevOps tickets** with GPT-4o-generated descriptions for customer-impacting incidents.

## 🏛️ Architecture

```
Azure Monitor Metrics
    │
    ▼ (Diagnostic Settings export)
Azure Event Hub (aiops-metrics, 4 partitions)
    │
    ▼ (Event Hub trigger, batch)
Azure Function (AnomalyDetectionPipeline)
    │
    ├── 1. AzureAnomalyDetectorService
    │       └── detect_univariate_last_point (sensitivity: 85)
    │           → AnomalyEvent: deviation%, severity_score, anomaly_type
    │
    ├── 2. AlertCorrelator (10-minute window)
    │       ├── Same resource → correlated
    │       ├── Same resource group + causal graph → correlated
    │       └── Groups N anomalies → 1 CorrelatedIncident
    │           (suppression reduces noise by ~80%)
    │
    ├── 3. AutoTicketCreator
    │       ├── customer_impacting + severity >= 0.6 → create ticket
    │       ├── GPT-4o: generate title + structured description
    │       └── Azure DevOps REST API: create Bug work item
    │
    └── Log all incidents to Log Analytics for trend analysis
```

## 🛠️ Tech Stack

| Component | Azure Service |
|-----------|--------------|
| Anomaly Detection | Azure Anomaly Detector (Cognitive Services) |
| Metric Streaming | Azure Event Hub |
| Serverless Compute | Azure Functions (Elastic Premium for consistent throughput) |
| Ticket Generation | Azure OpenAI GPT-4o |
| Ticketing System | Azure DevOps REST API |
| IaC | Bicep |

## 📁 Structure

```
project4-anomaly-detection/
├── src/
│   ├── detector/
│   │   └── anomaly_detector.py      # Azure Anomaly Detector wrapper + severity scoring
│   ├── correlator/
│   │   └── alert_correlator.py      # Time-window + causal-graph alert correlation
│   ├── ticketing/
│   │   └── auto_ticket.py           # GPT-4o ticket generation + ADO REST API
│   └── function_app.py              # Event Hub trigger: ingest → detect → correlate → ticket
├── bicep/main.bicep                  # Event Hub + Anomaly Detector + Function App
└── tests/test_correlator.py         # pytest: correlation logic tests
```

## 📊 Anomaly Types Detected

| Type | Description |
|------|-------------|
| `spike` | Value > 1.5× expected upper margin |
| `dip` | Value < 0.5× expected lower margin |
| `level_shift` | Sustained 30%+ deviation from expected |
| `trend_change` | Gradual drift detected by Azure API |

## 🔗 Causal Graph (Alert Suppression)

If **CPU spikes**, then Http5xx and latency anomalies fired in the same window are treated as **symptoms** of the same incident — not separate alerts. This is defined in the `CAUSAL_GRAPH` dict and reduces alert storms dramatically.

## 📊 Key AIOps Concepts

- **Azure Anomaly Detector API** — production-grade ML without building your own model
- **Event Hub batch processing** — high-throughput metric ingestion (thousands/sec)
- **Causal correlation** — domain knowledge encodes which metrics cause others
- **Noise suppression** — N correlated alerts → 1 incident (reduce alert fatigue)
- **Customer-impact routing** — only Http5xx / latency / availability anomalies page on-call
- **AI-generated tickets** — GPT-4o writes clear, structured work items automatically
- **Elastic Premium Functions** — consistent throughput, no cold-start for Event Hub triggers
