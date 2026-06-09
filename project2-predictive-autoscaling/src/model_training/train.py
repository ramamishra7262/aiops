"""
train.py
Azure ML pipeline step: trains a LightGBM time-series forecasting model
to predict CPU load 30 minutes ahead, then registers it in Azure ML Model Registry.
"""
import argparse
import logging
import os
import json
import joblib
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_absolute_error, mean_squared_error
import lightgbm as lgb
import mlflow
import mlflow.lightgbm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEATURE_COLS = [
    "cpu_percent", "memory_percent", "pod_count", "request_rate",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "is_weekend", "is_business_hour",
    "cpu_lag_1", "cpu_lag_3", "cpu_lag_6", "cpu_lag_12",
    "rps_lag_1", "rps_lag_3", "rps_lag_6", "rps_lag_12",
    "cpu_rolling_mean_30m", "cpu_rolling_std_30m", "rps_rolling_mean_30m",
]
TARGET_COL = "cpu_percent_future_6"   # CPU 30 min ahead (6 × 5min steps)


def create_target(df: pd.DataFrame, horizon: int = 6) -> pd.DataFrame:
    """Shift CPU column forward by `horizon` steps to create prediction target."""
    df[TARGET_COL] = df["cpu_percent"].shift(-horizon)
    return df.dropna()


def train(data_path: str, model_output_path: str):
    mlflow.set_experiment("predictive-autoscaling")

    with mlflow.start_run(run_name="lgbm-cpu-forecast"):
        # ── Load data ────────────────────────────────────────────────────────
        df = pd.read_parquet(data_path)
        df = create_target(df)
        logger.info(f"Training data: {len(df)} rows, {len(FEATURE_COLS)} features")

        X = df[FEATURE_COLS].values
        y = df[TARGET_COL].values

        # ── Time-series cross-validation (no shuffle!) ────────────────────
        tscv = TimeSeriesSplit(n_splits=5, gap=6)
        cv_maes, cv_rmses = [], []

        for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
            X_tr, X_val = X[train_idx], X[val_idx]
            y_tr, y_val = y[train_idx], y[val_idx]

            scaler = StandardScaler()
            X_tr_s  = scaler.fit_transform(X_tr)
            X_val_s = scaler.transform(X_val)

            model = lgb.LGBMRegressor(
                n_estimators=500,
                learning_rate=0.05,
                num_leaves=63,
                max_depth=8,
                min_child_samples=30,
                feature_fraction=0.8,
                bagging_fraction=0.8,
                bagging_freq=5,
                reg_alpha=0.1,
                reg_lambda=0.1,
                random_state=42,
                verbose=-1,
            )
            model.fit(
                X_tr_s, y_tr,
                eval_set=[(X_val_s, y_val)],
                callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(100)],
            )

            preds = model.predict(X_val_s)
            mae  = mean_absolute_error(y_val, preds)
            rmse = np.sqrt(mean_squared_error(y_val, preds))
            cv_maes.append(mae)
            cv_rmses.append(rmse)
            logger.info(f"Fold {fold+1}: MAE={mae:.2f} RMSE={rmse:.2f}")

        # ── Final model on full data ─────────────────────────────────────
        scaler_final = StandardScaler()
        X_scaled = scaler_final.fit_transform(X)

        final_model = lgb.LGBMRegressor(
            n_estimators=600, learning_rate=0.05, num_leaves=63,
            max_depth=8, random_state=42, verbose=-1,
        )
        final_model.fit(X_scaled, y)

        # ── Log metrics ──────────────────────────────────────────────────
        mean_mae  = np.mean(cv_maes)
        mean_rmse = np.mean(cv_rmses)
        mlflow.log_metrics({
            "cv_mae":  mean_mae,
            "cv_rmse": mean_rmse,
            "n_samples": len(df),
            "n_features": len(FEATURE_COLS),
            "forecast_horizon_min": 30,
        })
        mlflow.log_params(final_model.get_params())

        logger.info(f"Final CV — MAE: {mean_mae:.2f}% CPU | RMSE: {mean_rmse:.2f}% CPU")

        # ── Feature importance ────────────────────────────────────────────
        importance = dict(zip(FEATURE_COLS, final_model.feature_importances_))
        top_features = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:10]
        logger.info("Top features: " + ", ".join(f"{k}:{v}" for k, v in top_features))
        mlflow.log_dict({"feature_importance": importance}, "feature_importance.json")

        # ── Save model + scaler ───────────────────────────────────────────
        os.makedirs(model_output_path, exist_ok=True)
        joblib.dump(final_model, f"{model_output_path}/model.joblib")
        joblib.dump(scaler_final, f"{model_output_path}/scaler.joblib")
        joblib.dump(FEATURE_COLS,  f"{model_output_path}/feature_cols.joblib")

        meta = {
            "forecast_horizon_min": 30,
            "cv_mae_cpu_pct": round(mean_mae, 4),
            "cv_rmse_cpu_pct": round(mean_rmse, 4),
            "feature_cols": FEATURE_COLS,
            "target_col": TARGET_COL,
            "model_type": "LightGBM",
        }
        with open(f"{model_output_path}/metadata.json", "w") as f:
            json.dump(meta, f, indent=2)

        mlflow.lightgbm.log_model(final_model, "model")
        logger.info(f"Model saved to {model_output_path}")
        return mean_mae


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-path",         required=True)
    parser.add_argument("--model-output-path", required=True)
    args = parser.parse_args()
    train(args.data_path, args.model_output_path)
