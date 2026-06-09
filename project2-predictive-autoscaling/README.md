# 📈 Project 2: ML-based Predictive Autoscaling for AKS

An AIOps system that uses **Azure Machine Learning** to train a **LightGBM** time-series forecasting model on 30 days of AKS metrics, then predicts CPU load **30 minutes ahead** and pre-scales node pools **before traffic hits** — eliminating reactive lag in traditional autoscaling.

## 🏛️ Architecture

```
Log Analytics (30 days of metrics)
    │
    ▼  Azure ML Pipeline (weekly retrain)
    ├── Step 1: Collect AKS metrics (CPU, memory, pods, RPS)
    ├── Step 2: Feature engineering (lag features, cyclical time encoding)
    ├── Step 3: Train LightGBM (TimeSeriesSplit CV, MLflow tracking)
    └── Step 4: Evaluate vs champion → register in Model Registry

Azure Function (every 5 minutes)
    │
    ├── Collect last 2h of live metrics
    ├── Load model from Azure ML Registry
    ├── Generate 30-min forecast + confidence interval
    ├── Scale decision: scale_out / scale_in / no_change
    │       └── Safety: cooldown, min/max bounds, gradual scale-in
    └── Apply to AKS node pool via Azure SDK
```

## 📊 Model Details

| Property | Value |
|----------|-------|
| Algorithm | LightGBM Regressor |
| Target | CPU% in 30 minutes |
| Features | 21 features: CPU/memory/RPS lags, cyclical time encodings, rolling stats |
| Validation | TimeSeriesSplit (5-fold, no data leakage) |
| Tracking | MLflow (Azure ML backend) |
| Retraining | Weekly via Azure ML Pipeline (Cron: Monday 02:00 UTC) |

## 📁 Structure

```
project2-predictive-autoscaling/
├── src/
│   ├── data_pipeline/
│   │   └── metric_collector.py    # Pulls 30-day KQL metrics + adds time features
│   ├── model_training/
│   │   └── train.py               # LightGBM with TimeSeriesSplit CV + MLflow
│   ├── forecasting/
│   │   └── predictor.py           # Loads model, generates forecast + confidence interval
│   ├── scaler/
│   │   └── aks_scaler.py          # Applies AKS node pool scaling with safety guards
│   └── function_app.py            # Timer trigger: collect → predict → scale every 5m
├── pipelines/
│   └── training_pipeline.py       # Azure ML DSL pipeline + weekly schedule
└── requirements.txt
```

## 📊 Key AIOps Concepts

- **Predictive vs reactive** — scales 30 minutes before load arrives
- **Time-series CV** — TimeSeriesSplit prevents look-ahead bias
- **Cyclical features** — sin/cos hour/day encodings capture daily/weekly seasonality
- **Confidence intervals** — ±1.5×MAE bounds guide urgency of scale decision
- **Gradual scale-in** — never removes >2 nodes at once to prevent disruption
- **Cooldown guard** — 10-minute cooldown prevents scale oscillation
- **MLflow tracking** — every training run tracked with params, metrics, artefacts
- **Champion/challenger** — new model only registered if it beats the current champion's MAE
