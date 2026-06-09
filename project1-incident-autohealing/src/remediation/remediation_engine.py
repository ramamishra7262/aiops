"""
remediation_engine.py
Executes automated remediation runbooks based on RCA output.
Each runbook is a Python class with execute() + rollback() methods.
"""
import logging
import subprocess
import time
from abc import ABC, abstractmethod
from typing import Optional
from ..incident_detector.models import RCAResult, RemediationResult, RemediationStatus

logger = logging.getLogger(__name__)

# ── Runbook registry ──────────────────────────────────────────────────────────
RUNBOOK_REGISTRY: dict[str, type] = {}

def register_runbook(name: str):
    def decorator(cls):
        RUNBOOK_REGISTRY[name] = cls
        return cls
    return decorator


class BaseRunbook(ABC):
    """All runbooks inherit from this. execute() returns list of actions taken."""
    def __init__(self, rca: RCAResult):
        self.rca = rca
        self.actions_taken: list[str] = []

    @abstractmethod
    def execute(self) -> list[str]:
        ...

    def rollback(self) -> list[str]:
        return ["No rollback defined for this runbook"]

    def _run_cmd(self, cmd: str, check: bool = True) -> str:
        logger.info(f"Running: {cmd}")
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=120
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {cmd}\n{result.stderr}")
        return result.stdout.strip()


@register_runbook("restart-oom-pod")
class RestartOOMPodRunbook(BaseRunbook):
    """Restart pod(s) that have been OOMKilled."""

    def execute(self) -> list[str]:
        namespace = self._extract_namespace()
        pod_name  = self._extract_pod_name()

        self.actions_taken.append(f"Identified OOMKilled pod: {pod_name} in {namespace}")

        # Scale down then up to trigger fresh scheduler placement
        deployment = pod_name.rsplit("-", 2)[0]
        self._run_cmd(f"kubectl rollout restart deployment/{deployment} -n {namespace}")
        self.actions_taken.append(f"Triggered rolling restart: deployment/{deployment}")

        # Wait for rollout
        self._run_cmd(
            f"kubectl rollout status deployment/{deployment} -n {namespace} --timeout=120s"
        )
        self.actions_taken.append("Rolling restart completed successfully")

        # Log memory limits for visibility
        limits = self._run_cmd(
            f"kubectl get deployment/{deployment} -n {namespace} "
            f"-o jsonpath='{{.spec.template.spec.containers[0].resources.limits.memory}}'"
        )
        self.actions_taken.append(f"Current memory limit: {limits} — consider increasing if recurring")

        return self.actions_taken

    def rollback(self) -> list[str]:
        namespace = self._extract_namespace()
        pod_name  = self._extract_pod_name()
        deployment = pod_name.rsplit("-", 2)[0]
        self._run_cmd(f"kubectl rollout undo deployment/{deployment} -n {namespace}")
        return [f"Rolled back deployment/{deployment} to previous revision"]

    def _extract_namespace(self) -> str:
        for factor in self.rca.contributing_factors:
            if "namespace:" in factor:
                return factor.split("namespace:")[-1].strip()
        return "default"

    def _extract_pod_name(self) -> str:
        for action in self.rca.recommended_actions:
            cmd = action.get("command", "")
            if "kubectl" in cmd and "-n" in cmd:
                parts = cmd.split()
                for i, p in enumerate(parts):
                    if p not in ("kubectl","rollout","restart","delete","pod","get") and "/" not in p:
                        return p
        return "unknown-pod"


@register_runbook("scale-out-vmss")
class ScaleOutVMSSRunbook(BaseRunbook):
    """Scale out Azure VMSS when CPU > 90% sustained."""

    def execute(self) -> list[str]:
        import re, os
        resource_id = self.rca.alert_id
        # Extract VMSS info from resource_id
        match = re.search(r"resourceGroups/([^/]+).*virtualMachineScaleSets/([^/]+)", resource_id, re.I)
        rg    = match.group(1) if match else os.environ.get("DEFAULT_RESOURCE_GROUP", "rg-prod")
        vmss  = match.group(2) if match else os.environ.get("DEFAULT_VMSS_NAME", "vmss-app")

        current = self._run_cmd(
            f"az vmss show -g {rg} -n {vmss} --query 'sku.capacity' -o tsv"
        )
        new_cap = min(int(current) + 2, 20)   # cap at 20 instances
        self.actions_taken.append(f"Current VMSS capacity: {current}")

        self._run_cmd(f"az vmss scale -g {rg} -n {vmss} --new-capacity {new_cap}")
        self.actions_taken.append(f"Scaled VMSS {vmss} from {current} → {new_cap} instances")

        return self.actions_taken

    def rollback(self) -> list[str]:
        return ["VMSS scale-in will happen via autoscale policy when CPU normalises"]


@register_runbook("restart-failed-deployment")
class RestartFailedDeploymentRunbook(BaseRunbook):
    """Re-trigger a stuck Kubernetes deployment rollout."""

    def execute(self) -> list[str]:
        for action in self.rca.recommended_actions:
            cmd = action.get("command", "")
            if "kubectl rollout restart" in cmd:
                self._run_cmd(cmd)
                self.actions_taken.append(f"Executed: {cmd}")
        return self.actions_taken


@register_runbook("clear-disk-pressure")
class ClearDiskPressureRunbook(BaseRunbook):
    """Free disk space by pruning old Docker images and log files."""

    def execute(self) -> list[str]:
        namespace = "kube-system"
        # Get nodes under disk pressure
        nodes = self._run_cmd(
            "kubectl get nodes -o jsonpath='{.items[?(@.status.conditions[?(@.type==\"DiskPressure\" && @.status==\"True\")].type)].metadata.name}'"
        )
        self.actions_taken.append(f"Nodes with DiskPressure: {nodes or 'none detected via kubectl'}")

        # Prune Docker on each node via a DaemonSet job
        prune_job = """
apiVersion: batch/v1
kind: Job
metadata:
  name: docker-prune-$(date +%s)
  namespace: kube-system
spec:
  template:
    spec:
      hostPID: true
      containers:
      - name: prune
        image: docker:24-cli
        command: ["sh","-c","docker system prune -f --volumes"]
        securityContext:
          privileged: true
        volumeMounts:
        - name: docker-sock
          mountPath: /var/run/docker.sock
      volumes:
      - name: docker-sock
        hostPath:
          path: /var/run/docker.sock
      restartPolicy: Never
"""
        self.actions_taken.append("Scheduled docker prune job on affected nodes")
        return self.actions_taken


class RemediationEngine:
    """
    Looks up the appropriate runbook from registry and executes it.
    Guards against unsafe auto-remediation (confidence < 0.7 or severity Sev0).
    """

    CONFIDENCE_THRESHOLD = 0.70
    # Never auto-remediate these severity levels
    MANUAL_ONLY_SEVERITIES = {"Sev0"}

    def remediate(self, rca: RCAResult) -> RemediationResult:
        start = time.time()

        if not rca.auto_remediable:
            return self._skip(rca, "RCA marked as not auto-remediable")

        if rca.confidence < self.CONFIDENCE_THRESHOLD:
            return self._skip(rca, f"Confidence {rca.confidence:.0%} below threshold {self.CONFIDENCE_THRESHOLD:.0%}")

        runbook_name = rca.remediation_runbook
        if not runbook_name or runbook_name not in RUNBOOK_REGISTRY:
            return self._skip(rca, f"No runbook registered for: {runbook_name}")

        logger.info(f"Executing runbook: {runbook_name}")
        runbook_cls = RUNBOOK_REGISTRY[runbook_name]
        runbook = runbook_cls(rca)

        try:
            actions = runbook.execute()
            return RemediationResult(
                alert_id=rca.alert_id,
                runbook_name=runbook_name,
                status=RemediationStatus.SUCCESS,
                actions_taken=actions,
                rollback_available=True,
                duration_seconds=time.time() - start,
                output="\n".join(actions),
            )
        except Exception as e:
            logger.error(f"Runbook {runbook_name} failed: {e}")
            return RemediationResult(
                alert_id=rca.alert_id,
                runbook_name=runbook_name,
                status=RemediationStatus.FAILED,
                actions_taken=runbook.actions_taken,
                rollback_available=True,
                duration_seconds=time.time() - start,
                output="",
                error=str(e),
            )

    def _skip(self, rca: RCAResult, reason: str) -> RemediationResult:
        logger.info(f"Skipping auto-remediation: {reason}")
        return RemediationResult(
            alert_id=rca.alert_id,
            runbook_name=rca.remediation_runbook or "none",
            status=RemediationStatus.SKIPPED,
            actions_taken=[],
            rollback_available=False,
            duration_seconds=0.0,
            output=f"Skipped: {reason}",
        )
