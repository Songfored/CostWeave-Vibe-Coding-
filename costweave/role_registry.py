"""Provider-neutral planner roles and deterministic catalog assignments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .domain import WorkerProfile
from .planning_contracts import RoleSpec


DOMAIN_PLANNER = RoleSpec(
    role_id="domain-planner",
    title="领域与拆解规划者",
    objective="从领域正确性和依赖关系出发提出可执行任务结构。",
    required_capabilities=("planning", "analysis"),
    evaluation_focus=("decomposition", "dependencies", "domain_correctness"),
)

ECONOMICS_PLANNER = RoleSpec(
    role_id="economics-planner",
    title="成本与时延规划者",
    objective="从总成本、关键路径、并行收益和容量约束出发评价任务结构。",
    required_capabilities=("planning", "analysis"),
    evaluation_focus=("cost", "latency", "capacity", "parallelism"),
)

RISK_PLANNER = RoleSpec(
    role_id="risk-planner",
    title="风险与验收规划者",
    objective="从失败损失、不可逆性、证据和验收边界出发审查任务结构。",
    required_capabilities=("risk", "validation", "planning"),
    evaluation_focus=("risk", "verification", "rollback", "evidence"),
)


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    role_id: str
    worker_id: str | None
    model_id: str | None
    provider: str | None
    deployment: str | None
    score: float
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return {
            "role_id": self.role_id,
            "worker_id": self.worker_id,
            "model_id": self.model_id,
            "provider": self.provider,
            "deployment": self.deployment,
            "score": round(self.score, 4),
            "rationale": self.rationale,
        }


class RoleRegistry:
    """Maps stable management roles onto an editable model catalog."""

    def __init__(self, roles: Iterable[RoleSpec] | None = None) -> None:
        selected = tuple(roles or (
            DOMAIN_PLANNER,
            ECONOMICS_PLANNER,
            RISK_PLANNER,
        ))
        if len({role.role_id for role in selected}) != len(selected):
            raise ValueError("planner role ids must be unique")
        self._roles = {role.role_id: role for role in selected}

    def get(self, role_id: str) -> RoleSpec:
        return self._roles[role_id]

    def list(self) -> list[RoleSpec]:
        return list(self._roles.values())

    def assign(
        self,
        role_ids: Iterable[str],
        workers: Iterable[WorkerProfile],
        *,
        prefer_diversity: bool = True,
    ) -> list[RoleAssignment]:
        available = [worker for worker in workers if worker.routable]
        used_providers: set[str] = set()
        used_workers: set[str] = set()
        assignments: list[RoleAssignment] = []
        for role_id in role_ids:
            role = self.get(role_id)
            candidates: list[tuple[float, WorkerProfile]] = []
            for worker in available:
                capability = sum(
                    float(worker.capabilities.get(item, 0.0))
                    for item in role.required_capabilities
                ) / len(role.required_capabilities)
                diversity = (
                    0.09
                    if prefer_diversity and worker.provider not in used_providers
                    else 0.0
                )
                uniqueness = 0.04 if worker.id not in used_workers else -0.08
                score = (
                    capability * 0.52
                    + float(worker.reliability) * 0.22
                    + float(worker.reasoning) * 0.16
                    + diversity
                    + uniqueness
                    - min(0.08, float(worker.cost_per_task) * 0.02)
                )
                candidates.append((score, worker))
            candidates.sort(key=lambda item: (-item[0], item[1].id))
            if not candidates:
                assignments.append(RoleAssignment(
                    role_id=role_id,
                    worker_id=None,
                    model_id=None,
                    provider=None,
                    deployment=None,
                    score=0.0,
                    rationale="目录中没有可路由候选；保留角色合同但不触发调用。",
                ))
                continue
            score, worker = candidates[0]
            used_providers.add(worker.provider)
            used_workers.add(worker.id)
            assignments.append(RoleAssignment(
                role_id=role_id,
                worker_id=worker.id,
                model_id=worker.model_id,
                provider=worker.provider,
                deployment="local" if worker.local else "remote",
                score=max(0.0, min(1.0, score)),
                rationale=(
                    "按角色能力、可靠性、推理强度、成本和组合多样性评分；"
                    "核心逻辑不依赖厂商名称。"
                ),
            ))
        return assignments

