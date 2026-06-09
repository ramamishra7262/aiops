"""
training_pipeline.py
Azure ML Pipeline: collect data → feature engineering → train → evaluate → register model.
Scheduled to retrain weekly with fresh data.
"""
from azure.ai.ml import MLClient, command, dsl, Input, Output
from azure.ai.ml.constants import AssetTypes
from azure.ai.ml.entities import (
    AmlCompute, Environment, RecurrenceTrigger, RecurrencePattern,
    JobSchedule, CronTrigger,
)
from azure.identity import DefaultAzureCredential
import os


def get_ml_client() -> MLClient:
    return MLClient(
        DefaultAzureCredential(),
        subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
        resource_group_name=os.environ["AZURE_RESOURCE_GROUP"],
        workspace_name=os.environ["AZURE_ML_WORKSPACE"],
    )


def create_compute(ml_client: MLClient):
    """Create CPU compute cluster for training."""
    compute = AmlCompute(
        name="cpu-cluster",
        size="Standard_DS3_v2",
        min_instances=0,
        max_instances=4,
        idle_time_before_scale_down=120,
    )
    ml_client.compute.begin_create_or_update(compute).result()


def create_environment(ml_client: MLClient) -> str:
    """Register training environment with all dependencies."""
    env = Environment(
        name="aiops-training-env",
        description="AIOps predictive autoscaling training environment",
        conda_file="environment.yml",
        image="mcr.microsoft.com/azureml/openmpi4.1.0-ubuntu20.04",
        version="1.0",
    )
    ml_client.environments.create_or_update(env)
    return "aiops-training-env@1.0"


@dsl.pipeline(
    name="predictive-autoscaling-training",
    description="Train LightGBM model to predict AKS CPU 30 minutes ahead",
    compute="cpu-cluster",
)
def training_pipeline(workspace_id: str):

    # ── Step 1: Collect metrics from Log Analytics ───────────────────────────
    collect_step = command(
        name="collect-metrics",
        display_name="Collect AKS Metrics (30 days)",
        code="./src/data_pipeline",
        command="python metric_collector.py --workspace-id ${{inputs.workspace_id}} --output-path ${{outputs.raw_data}}",
        inputs={"workspace_id": workspace_id},
        outputs={"raw_data": Output(type=AssetTypes.URI_FOLDER)},
        environment="aiops-training-env@1.0",
    )

    # ── Step 2: Feature engineering ──────────────────────────────────────────
    feature_step = command(
        name="feature-engineering",
        display_name="Feature Engineering",
        code="./src/data_pipeline",
        command="python feature_engineer.py --input-path ${{inputs.raw_data}} --output-path ${{outputs.features}}",
        inputs={"raw_data": collect_step.outputs.raw_data},
        outputs={"features": Output(type=AssetTypes.URI_FOLDER)},
        environment="aiops-training-env@1.0",
    )

    # ── Step 3: Train LightGBM model ─────────────────────────────────────────
    train_step = command(
        name="train-model",
        display_name="Train LightGBM Forecast Model",
        code="./src/model_training",
        command="python train.py --data-path ${{inputs.features}} --model-output-path ${{outputs.model}}",
        inputs={"features": feature_step.outputs.features},
        outputs={"model": Output(type=AssetTypes.URI_FOLDER)},
        environment="aiops-training-env@1.0",
        resources={"instance_count": 1, "instance_type": "Standard_DS3_v2"},
    )

    # ── Step 4: Evaluate + register if better than champion ──────────────────
    evaluate_step = command(
        name="evaluate-register",
        display_name="Evaluate & Register Model",
        code="./src/model_training",
        command=(
            "python evaluate_register.py "
            "--model-path ${{inputs.model}} "
            "--workspace-name $AZURE_ML_WORKSPACE "
            "--model-name predictive-autoscaler"
        ),
        inputs={"model": train_step.outputs.model},
        environment="aiops-training-env@1.0",
    )

    return {}


def schedule_weekly_retraining(ml_client: MLClient):
    """Schedule weekly pipeline run every Monday at 02:00 UTC."""
    pipeline_job = training_pipeline(
        workspace_id=os.environ["LOG_ANALYTICS_WORKSPACE_ID"]
    )
    pipeline_job = ml_client.jobs.create_or_update(pipeline_job, experiment_name="predictive-autoscaling")

    schedule = JobSchedule(
        name="weekly-retrain",
        create_job=pipeline_job,
        trigger=CronTrigger(expression="0 2 * * 1"),  # Monday 02:00 UTC
    )
    ml_client.schedules.begin_create_or_update(schedule).result()
    print(f"✅ Weekly retraining scheduled: {schedule.name}")


if __name__ == "__main__":
    ml_client = get_ml_client()
    create_compute(ml_client)
    create_environment(ml_client)
    schedule_weekly_retraining(ml_client)
