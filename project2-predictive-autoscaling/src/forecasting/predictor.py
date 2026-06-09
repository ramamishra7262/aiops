"""
predictor.py
Loads the trained LightGBM model and generates 30-minute-ahead CPU forecasts.
Called every 5 minutes by a scheduled Azure Function.
"""
import joblib
import logging
import json
import numpy as np
import pandas as pd
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ForecastResult:
    timestamp: str
    predicted_cpu_pct: float
    confidence_lower: float
    confidence_upper: float
    recommended_node_count: int
    scale_action: str         # "scale_out", "scale_in", "no_change"
    urgency: str              # "immediate", "gradual", "none"
    reasoning: str


class CPUPredictor:
    """
    Loads model artefacts and produces scaled node count recommendations.
    Scale-out is triggered BEFORE load hits (predictive, not reactive).
    """

    CPU_PER_NODE   = 4.0   # vCPUs per node (Standard_D2s_v3 has 2 vCPU = 200% CPU)
    SAFETY_MARGIN  = 0.15  # keep headroom above forecast
    SCALE_OUT_THRESHOLD = 0.70   # scale out when predicted > 70%
    SCALE_IN_THRESHOLD  = 0.40   # scale in when predicted < 40%

    def __init__(self, model_dir: str):
        self.model       = joblib.load(f"{model_dir}/model.joblib")
        self.scaler      = joblib.load(f"{model_dir}/scaler.joblib")
        self.feature_cols = joblib.load(f"{model_dir}/feature_cols.joblib")
        with open(f"{model_dir}/metadata.json") as f:
            self.metadata = json.load(f)
        logger.info(f"Model loaded — horizon: {self.metadata['forecast_horizon_min']}min, MAE: {self.metadata['cv_mae_cpu_pct']:.2f}%")

    def predict(self, features: pd.DataFrame, current_nodes: int) -> ForecastResult:
        """
        features: latest 5-min window with all feature columns
        current_nodes: current AKS node pool count
        """
        X = features[self.feature_cols].values[-1:].reshape(1, -1)
        X_scaled = self.scaler.transform(X)

        pred_cpu = float(self.model.predict(X_scaled)[0])
        pred_cpu = max(0.0, min(pred_cpu, 100.0))   # clamp

        # Uncertainty estimate: ±1.5 × MAE
        mae = self.metadata["cv_mae_cpu_pct"]
        lower = max(0.0, pred_cpu - 1.5 * mae)
        upper = min(100.0, pred_cpu + 1.5 * mae)

        # Required nodes for predicted load + safety margin
        target_util = self.SCALE_OUT_THRESHOLD - self.SAFETY_MARGIN
        required_nodes = max(1, int(np.ceil((pred_cpu / 100.0) * current_nodes / target_util)))

        # Scale decision
        if pred_cpu > self.SCALE_OUT_THRESHOLD * 100:
            action   = "scale_out"
            urgency  = "immediate" if pred_cpu > 85 else "gradual"
            reasoning = (
                f"Predicted CPU {pred_cpu:.1f}% in 30 minutes exceeds {self.SCALE_OUT_THRESHOLD*100:.0f}% threshold. "
                f"Pre-scaling from {current_nodes} → {required_nodes} nodes to prevent throttling."
            )
        elif pred_cpu < self.SCALE_IN_THRESHOLD * 100 and required_nodes < current_nodes:
            action    = "scale_in"
            urgency   = "gradual"
            reasoning = (
                f"Predicted CPU {pred_cpu:.1f}% in 30 minutes is below {self.SCALE_IN_THRESHOLD*100:.0f}% threshold. "
                f"Safely scaling from {current_nodes} → {required_nodes} nodes to reduce cost."
            )
        else:
            action    = "no_change"
            urgency   = "none"
            required_nodes = current_nodes
            reasoning = f"Predicted CPU {pred_cpu:.1f}% — within normal range, no scaling needed."

        import datetime
        return ForecastResult(
            timestamp=datetime.datetime.utcnow().isoformat() + "Z",
            predicted_cpu_pct=round(pred_cpu, 2),
            confidence_lower=round(lower, 2),
            confidence_upper=round(upper, 2),
            recommended_node_count=required_nodes,
            scale_action=action,
            urgency=urgency,
            reasoning=reasoning,
        )
