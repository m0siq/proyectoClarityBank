"""Azure ML Pipeline definition for monthly fastText retraining (SPEC §7).

Schedule: CRON "0 3 5 * *" (03:00 UTC on the 5th of each month).
Components:
  extract_feedback → train_fasttext → evaluate → publish_model
"""

from __future__ import annotations

import os
from datetime import datetime, UTC

from azure.ai.ml import Input, MLClient, Output, dsl, load_component
from azure.ai.ml.constants import AssetTypes
from azure.ai.ml.entities import RecurrenceTrigger, Schedule
from azure.identity import DefaultAzureCredential

# ── Load components ───────────────────────────────────────────────────────────

BASE = os.path.dirname(__file__)

extract_feedback = load_component(
    source=os.path.join(BASE, "components/extract_feedback/component.yaml")
)
train_fasttext = load_component(
    source=os.path.join(BASE, "components/train_fasttext/component.yaml")
)
evaluate_model = load_component(
    source=os.path.join(BASE, "components/evaluate/component.yaml")
)
publish_model = load_component(
    source=os.path.join(BASE, "components/publish_model/component.yaml")
)


# ── Pipeline definition ───────────────────────────────────────────────────────

@dsl.pipeline(
    name="fasttext-monthly-retraining",
    description="Monthly fastText retraining from feedback_loop data",
    compute="cpu-cluster-low",
)
def retraining_pipeline(year_month: str) -> None:
    extract_step = extract_feedback(year_month=year_month)

    train_step = train_fasttext(
        feedback_parquet=extract_step.outputs.feedback_parquet,
    )

    evaluate_step = evaluate_model(
        new_metrics_path=train_step.outputs.metrics_path,
    )
    # evaluate_step depends on train_step implicitly via outputs

    publish_step = publish_model(
        model_path=train_step.outputs.model_path,
        metrics_path=train_step.outputs.metrics_path,
    )
    publish_step.after(evaluate_step)  # only publish after evaluation passes


# ── Register pipeline + schedule ─────────────────────────────────────────────

def register_pipeline() -> None:
    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    resource_group = os.environ["AZURE_RESOURCE_GROUP"]
    workspace = os.environ["AML_WORKSPACE"]

    credential = DefaultAzureCredential()
    ml_client = MLClient(credential, subscription_id, resource_group, workspace)

    year_month = datetime.now(UTC).strftime("%Y-%m")
    pipeline_job = retraining_pipeline(year_month=year_month)
    pipeline_job.settings.default_compute = "cpu-cluster-low"

    # Submit once to register
    registered = ml_client.jobs.create_or_update(pipeline_job, experiment_name="fasttext-retraining")
    print(f"Pipeline job submitted: {registered.name}")

    # Create monthly schedule (SPEC §7.3): 03:00 UTC on the 5th of each month
    schedule = Schedule(
        name="fasttext-monthly-schedule",
        trigger=RecurrenceTrigger(
            frequency="month",
            interval=1,
            schedule={"hours": [3], "minutes": [0], "month_days": [5]},
        ),
        create_job=retraining_pipeline(year_month="PLACEHOLDER"),
    )
    ml_client.schedules.begin_create_or_update(schedule).result()
    print("Monthly schedule registered.")


if __name__ == "__main__":
    register_pipeline()
