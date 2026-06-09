# 🤖 Project 1: Intelligent Incident Detection & Auto-Remediation

An AIOps system that receives **Azure Monitor alerts**, enriches them with real-time log and metric context from **Log Analytics**, uses **Azure OpenAI GPT-4o** to generate a root cause analysis, and automatically executes safe **remediation runbooks** — all within seconds of an alert firing.

## 🏛️ Architecture

```
Azure Monitor Alert fires
    │
    ▼ (HTTP webhook)
Azure Function (AIOpsIncidentHandler)
    │
    ├── 1. AlertProcessor
    │       └── Fetches last 30m logs + metrics from Log Analytics
    │           Finds similar past incidents (incident history table)
    │
    ├── 2. RCAEngine (Azure OpenAI GPT-4o)
    │       └── Sends enriched context → receives structured JSON:
    │           root_cause, confidence, contributing_factors,
    │           recommended_actions, auto_remediable, runbook
    │
    ├── 3. RemediationEngine
    │       ├── confidence >= 70%? → execute runbook
    │       ├── Runbooks: restart-oom-pod, scale-out-vmss,
    │       │            restart-failed-deployment, clear-disk-pressure
    │       └── On failure → rollback available
    │
    ├── 4. Slack notification (full RCA + actions taken)
    └── 5. Log incident to custom table (future ML training data)
```

## 🛠️ Tech Stack

| Component | Azure Service |
|-----------|--------------|
| AI / RCA Engine | Azure OpenAI (GPT-4o) |
| Serverless compute | Azure Functions (Python 3.11) |
| Alerting source | Azure Monitor + Action Groups |
| Log enrichment | Log Analytics (KQL queries) |
| Secrets management | Azure Key Vault (Key Vault references) |
| Observability | Application Insights |
| IaC | Bicep |
| CI/CD | GitHub Actions |

## 📁 Structure

```
project1-incident-autohealing/
├── src/
│   ├── function_app.py               # Azure Function entry point (webhook handler)
│   ├── incident_detector/
│   │   ├── alert_processor.py        # Parses alert + fetches Log Analytics context
│   │   └── models.py                 # Alert, RCAResult, RemediationResult dataclasses
│   ├── openai_rca/
│   │   └── rca_engine.py             # GPT-4o RCA with structured JSON output
│   └── remediation/
│       └── remediation_engine.py     # 4 runbooks + safety guards
├── bicep/main.bicep                  # OpenAI + Function + Key Vault + Log Analytics
├── tests/test_rca_engine.py          # pytest unit tests
└── .github/workflows/deploy.yml     # Test → Bicep deploy → Function deploy
```

## 🔐 Safety Guards

- **Confidence threshold (70%)** — low-confidence RCA skips auto-remediation
- **Manual-only Sev0** — critical incidents always require human approval
- **Runbook registry** — only whitelisted runbooks can execute
- **Rollback capability** — every runbook implements `rollback()`
- **Timeout (120s)** — runbooks abort if they exceed 2 minutes

## 📊 Key AIOps Concepts

- **AI-augmented alerting** — every alert gets a GPT-4o root cause analysis automatically
- **Structured LLM output** — `response_format: json_object` ensures parseable RCA
- **Historical context** — similar past incidents fed to GPT-4o improve accuracy
- **Closed-loop remediation** — auto-heals known patterns without human intervention
- **Incident telemetry** — every incident logged to custom table for future ML training
- **Low-temperature inference** — temperature=0.1 for deterministic, factual RCA
