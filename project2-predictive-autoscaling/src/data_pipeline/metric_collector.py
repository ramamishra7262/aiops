"""
metric_collector.py
Collects historical AKS metrics from Azure Monitor / Log Analytics
and prepares a feature-rich DataFrame for ML model training.
"""
import pandas as pd
import numpy as np
import logging
import os
from datetime import datetime, timedelta
from azure.monitor.query import LogsQueryClient, MetricsQueryClient, LogsQueryStatus
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)


class AKSMetricCollector:
    """
    Pulls 30-day historical metrics at 5-min granularity:
    - CPU utilisation (node + pod level)
    - Memory utilisation
    - Active pod count
    - HTTP request rate
    - Network ingress/egress
    Adds time-based features for seasonality learning.
    """

    METRIC_QUERIES = {
        "cpu_percent": """
            Perf
            | where ObjectName == 'K8SNode' and CounterName == 'cpuUsageNanoCores'
            | summarize avg_cpu = avg(CounterValue) / 10000000
              by bin(TimeGenerated, 5m)
            | order by TimeGenerated asc
        """,
        "memory_percent": """
            Perf
            | where ObjectName == 'K8SNode' and CounterName == 'memoryWorkingSetBytes'
            | summarize avg_mem_mb = avg(CounterValue) / 1048576
              by bin(TimeGenerated, 5m)
            | order by TimeGenerated asc
        """,
        "pod_count": """
            KubePodInventory
            | where Namespace == 'prod' and PodStatus == 'Running'
            | summarize pod_count = dcount(Name)
              by bin(TimeGenerated, 5m)
            | order by TimeGenerated asc
        """,
        "request_rate": """
            requests
            | summarize rps = count() / 300.0
              by bin(timestamp, 5m)
            | order by timestamp asc
        """,
    }

    def __init__(self):
        self.credential = DefaultAzureCredential()
        self.logs_client = LogsQueryClient(self.credential)
        self.workspace_id = os.environ["LOG_ANALYTICS_WORKSPACE_ID"]

    def collect(self, lookback_days: int = 30) -> pd.DataFrame:
        logger.info(f"Collecting {lookback_days} days of AKS metrics")
        dfs = {}
        timespan = timedelta(days=lookback_days)

        for metric_name, query in self.METRIC_QUERIES.items():
            df = self._query_metric(metric_name, query, timespan)
            if df is not None:
                dfs[metric_name] = df

        if not dfs:
            raise RuntimeError("No metrics collected — check Log Analytics workspace ID")

        # Merge all metrics on timestamp
        merged = list(dfs.values())[0]
        for df in list(dfs.values())[1:]:
            merged = pd.merge(merged, df, on="timestamp", how="outer")

        merged = merged.sort_values("timestamp").fillna(method="ffill").dropna()
        merged = self._add_time_features(merged)
        logger.info(f"Collected {len(merged)} data points across {merged['timestamp'].nunique()} timestamps")
        return merged

    def _query_metric(self, name: str, query: str, timespan) -> pd.DataFrame | None:
        try:
            result = self.logs_client.query_workspace(
                workspace_id=self.workspace_id,
                query=query,
                timespan=timespan,
            )
            if result.status == LogsQueryStatus.SUCCESS and result.tables:
                rows = result.tables[0].rows
                cols = result.tables[0].columns
                df = pd.DataFrame(rows, columns=cols)
                ts_col = next((c for c in cols if "time" in c.lower()), cols[0])
                val_col = [c for c in cols if c != ts_col][0]
                return df.rename(columns={ts_col: "timestamp", val_col: name})
        except Exception as e:
            logger.warning(f"Failed to collect {name}: {e}")
        return None

    def _add_time_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add cyclical time encodings so the model learns daily/weekly patterns."""
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df["hour"]         = df["timestamp"].dt.hour
        df["day_of_week"]  = df["timestamp"].dt.dayofweek
        df["is_weekend"]   = (df["day_of_week"] >= 5).astype(int)
        df["is_business_hour"] = ((df["hour"] >= 9) & (df["hour"] <= 17)).astype(int)

        # Cyclical encoding (sin/cos) — avoids discontinuity at midnight/Sunday
        df["hour_sin"]  = np.sin(2 * np.pi * df["hour"] / 24)
        df["hour_cos"]  = np.cos(2 * np.pi * df["hour"] / 24)
        df["dow_sin"]   = np.sin(2 * np.pi * df["day_of_week"] / 7)
        df["dow_cos"]   = np.cos(2 * np.pi * df["day_of_week"] / 7)

        # Lag features (t-1, t-3, t-6 = 5min, 15min, 30min ago)
        for lag in [1, 3, 6, 12]:
            df[f"cpu_lag_{lag}"]     = df["cpu_percent"].shift(lag)
            df[f"rps_lag_{lag}"]     = df["request_rate"].shift(lag)

        # Rolling statistics
        df["cpu_rolling_mean_30m"] = df["cpu_percent"].rolling(6).mean()
        df["cpu_rolling_std_30m"]  = df["cpu_percent"].rolling(6).std()
        df["rps_rolling_mean_30m"] = df["request_rate"].rolling(6).mean()

        return df.dropna()
