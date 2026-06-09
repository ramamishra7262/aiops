"""
function_app.py
Azure Function: runs every 5 minutes, fetches latest metrics,
generates 30-min-ahead forecast, and applies AKS scaling decision.
"""
import azure.functions as func
import logging
import json
import os
import pandas as pd
from src.data_pipeline.metric_collector import AKSMetricCollector
from src.forecasting.predictor import CPUPredictor
from src.scaler.aks_scaler import AKSScaler

logger = logging.getLogger(__name__)
app = func.FunctionApp()

collector = AKSMetricCollector()
predictor = CPUPredictor(model_dir=os.environ.get("MODEL_DIR", "/home/site/wwwroot/model"))
scaler    = AKSScaler()


@app.function_name("PredictiveAutoscaler")
@app.schedule(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=True)
def predictive_autoscaler(timer: func.TimerRequest) -> None:
    """Runs every 5 minutes: predict → decide → scale."""
    logger.info("Predictive autoscaler triggered")

    # 1. Collect last 2 hours of metrics for feature generation
    try:
        features = collector.collect(lookback_days=0.083)  # 2 hours
    except Exception as e:
        logger.error(f"Metric collection failed: {e}")
        return

    # 2. Generate forecast
    current_nodes = int(os.environ.get("CURRENT_NODE_COUNT", "3"))
    forecast = predictor.predict(features, current_nodes)
    logger.info(
        f"Forecast: {forecast.predicted_cpu_pct:.1f}% CPU in 30min "
        f"[{forecast.confidence_lower:.1f}-{forecast.confidence_upper:.1f}] "
        f"→ {forecast.scale_action}"
    )

    # 3. Apply scaling decision
    result = scaler.apply(forecast)
    logger.info(f"Scale result: {result}")

    # 4. Emit custom metric to App Insights for dashboarding
    _emit_telemetry(forecast, result)


def _emit_telemetry(forecast, result):
    """Push forecast metrics to Application Insights custom dimensions."""
    import opencensus.ext.azure.log_exporter
    properties = {
        "predicted_cpu_30min": forecast.predicted_cpu_pct,
        "confidence_lower":    forecast.confidence_lower,
        "confidence_upper":    forecast.confidence_upper,
        "scale_action":        forecast.scale_action,
        "recommended_nodes":   forecast.recommended_node_count,
        "scale_status":        result.get("status", "unknown"),
    }
    logger.info("AIOPS_FORECAST", extra={"custom_dimensions": properties})
