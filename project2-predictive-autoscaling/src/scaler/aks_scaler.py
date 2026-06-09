"""
aks_scaler.py
Applies scale decisions to AKS node pool using Azure SDK.
Includes safety guards: min/max bounds, cooldown period, gradual scale-in.
"""
import logging
import os
from datetime import datetime, timedelta
from azure.identity import DefaultAzureCredential
from azure.mgmt.containerservice import ContainerServiceClient
from ..forecasting.predictor import ForecastResult

logger = logging.getLogger(__name__)


class AKSScaler:
    COOLDOWN_MINUTES = 10    # min gap between scale actions
    MIN_NODES = 1
    MAX_NODES = 20

    def __init__(self):
        self.credential     = DefaultAzureCredential()
        self.subscription   = os.environ["AZURE_SUBSCRIPTION_ID"]
        self.resource_group = os.environ["AKS_RESOURCE_GROUP"]
        self.cluster_name   = os.environ["AKS_CLUSTER_NAME"]
        self.node_pool_name = os.environ.get("AKS_NODE_POOL", "userpool")
        self.client = ContainerServiceClient(self.credential, self.subscription)
        self._last_scale_time: datetime | None = None

    def apply(self, forecast: ForecastResult) -> dict:
        """Apply the forecast-driven scale recommendation."""
        if forecast.scale_action == "no_change":
            return {"status": "no_change", "reason": forecast.reasoning}

        if self._in_cooldown():
            return {"status": "skipped", "reason": f"In cooldown period (last scale: {self._last_scale_time})"}

        target = max(self.MIN_NODES, min(forecast.recommended_node_count, self.MAX_NODES))
        current = self._get_current_node_count()

        if target == current:
            return {"status": "no_change", "reason": "Target == current node count"}

        # Gradual scale-in: never remove more than 2 nodes at once
        if forecast.scale_action == "scale_in" and (current - target) > 2:
            target = current - 2
            logger.info(f"Gradual scale-in: reducing by max 2 nodes → {target}")

        logger.info(f"Scaling AKS {self.cluster_name} nodepool {self.node_pool_name}: {current} → {target}")
        self._scale_node_pool(target)
        self._last_scale_time = datetime.utcnow()

        return {
            "status":     "applied",
            "action":     forecast.scale_action,
            "from_nodes": current,
            "to_nodes":   target,
            "reason":     forecast.reasoning,
            "predicted_cpu_30min": forecast.predicted_cpu_pct,
        }

    def _get_current_node_count(self) -> int:
        agent_pool = self.client.agent_pools.get(
            self.resource_group, self.cluster_name, self.node_pool_name
        )
        return agent_pool.count

    def _scale_node_pool(self, target_count: int):
        self.client.agent_pools.begin_create_or_update(
            self.resource_group,
            self.cluster_name,
            self.node_pool_name,
            {"count": target_count},
        ).result(timeout=300)

    def _in_cooldown(self) -> bool:
        if self._last_scale_time is None:
            return False
        return datetime.utcnow() - self._last_scale_time < timedelta(minutes=self.COOLDOWN_MINUTES)
