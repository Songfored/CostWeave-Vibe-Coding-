"""Compile task contracts into a validated Artifact dependency graph."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .domain import Plan, TaskContract


class GraphCompileError(ValueError):
    pass


class DependencyKind(StrEnum):
    HARD = "hard"
    SOFT = "soft"
    VALIDATION = "validation"


@dataclass(frozen=True, slots=True)
class CompiledGraph:
    run_id: str
    plan_revision: int
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "plan_revision": self.plan_revision,
            "nodes": [dict(node) for node in self.nodes],
            "edges": [dict(edge) for edge in self.edges],
        }


class GraphCompiler:
    """The deterministic gate between a planner proposal and runtime state."""

    GATE_TYPES = frozenset({"validation", "risk", "safety"})

    def compile(self, run_id: str, plan: Plan) -> CompiledGraph:
        task_by_id = {task.id: task for task in plan.tasks}
        if len(task_by_id) != len(plan.tasks):
            raise GraphCompileError("task producer IDs must be unique")

        for task in plan.tasks:
            task.plan_version = int(plan.version)
            self._normalize_task_contract(task)

        edges: list[dict[str, Any]] = []
        for task in plan.tasks:
            for dependency in task.dependencies:
                producer = task_by_id.get(dependency)
                if producer is None:
                    raise GraphCompileError(
                        f"missing producer task {dependency} for {task.id}"
                    )
                raw_kind = task.dependency_types.get(dependency, DependencyKind.HARD)
                try:
                    kind = DependencyKind(raw_kind)
                except ValueError as exc:
                    raise GraphCompileError(
                        f"unsupported dependency kind {raw_kind!r}"
                    ) from exc
                expected_schema = task.input_artifact_schemas.get(dependency)
                produced_schemas = {
                    str(spec["schema_ref"]) for spec in producer.output_artifact_specs
                }
                if expected_schema and expected_schema not in produced_schemas:
                    raise GraphCompileError(
                        f"schema conflict: {task.id} requires {expected_schema} "
                        f"from {dependency}, producer offers {sorted(produced_schemas)}"
                    )
                for spec in producer.output_artifact_specs:
                    sensitivity = str(spec.get("sensitivity", "internal"))
                    if sensitivity not in task.data_permissions:
                        raise GraphCompileError(
                            f"permission conflict: {task.id} cannot consume "
                            f"{sensitivity} artifact from {dependency}"
                        )
                edges.append(
                    {
                        "from_task": dependency,
                        "to_task": task.id,
                        "kind": kind.value,
                        "required_schema": expected_schema,
                    }
                )

        if self._has_cycle(plan.tasks):
            raise GraphCompileError("artifact task graph contains a cycle")

        nodes = tuple(
            {
                "task_id": task.id,
                "task_type": task.task_type,
                "status": task.status.value,
                "output_artifact_specs": [
                    dict(spec) for spec in task.output_artifact_specs
                ],
                "artifact_ids": list(task.artifact_ids),
            }
            for task in plan.tasks
        )
        return CompiledGraph(run_id, plan.version, nodes, tuple(edges))

    def affected_subgraph(
        self,
        plan: Plan,
        changed_task_ids: set[str] | list[str] | tuple[str, ...],
    ) -> set[str]:
        changed = set(changed_task_ids)
        known = {task.id for task in plan.tasks}
        unknown = changed - known
        if unknown:
            raise GraphCompileError(f"unknown changed tasks: {sorted(unknown)}")
        children: dict[str, list[str]] = defaultdict(list)
        for task in plan.tasks:
            for dependency in task.dependencies:
                children[dependency].append(task.id)
        affected = set(changed)
        queue = deque(changed)
        while queue:
            current = queue.popleft()
            for child in children[current]:
                if child not in affected:
                    affected.add(child)
                    queue.append(child)
        return affected

    def _normalize_task_contract(self, task: TaskContract) -> None:
        if {"image", "video"} & set(task.required_modalities):
            task.gpu_count = max(1, task.gpu_count)
            task.vram_mb = max(8_192, task.vram_mb)
            task.cpu_cores = max(2.0, task.cpu_cores)
        if not task.output_artifact_specs:
            task.output_artifact_specs = [
                {
                    "name": "primary",
                    "artifact_type": f"{task.task_type}_result",
                    "schema_ref": f"artifact://schemas/{task.task_type}/1.0",
                    "sensitivity": "internal",
                }
            ]
        names: set[str] = set()
        for spec in task.output_artifact_specs:
            required = {"name", "artifact_type", "schema_ref"}
            missing = required - set(spec)
            if missing:
                raise GraphCompileError(
                    f"{task.id} output artifact spec missing {sorted(missing)}"
                )
            name = str(spec["name"])
            if name in names:
                raise GraphCompileError(
                    f"{task.id} has duplicate artifact producer name {name}"
                )
            names.add(name)
            sensitivity = str(spec.get("sensitivity", "internal"))
            if sensitivity not in {"public", "internal", "sensitive"}:
                raise GraphCompileError(
                    f"{task.id} has unsupported artifact sensitivity"
                )
            spec["sensitivity"] = sensitivity
        for dependency in task.dependencies:
            if dependency not in task.dependency_types:
                task.dependency_types[dependency] = (
                    DependencyKind.VALIDATION.value
                    if task.task_type in self.GATE_TYPES
                    else DependencyKind.HARD.value
                )

    @staticmethod
    def _has_cycle(tasks: list[TaskContract]) -> bool:
        indegree = {task.id: len(task.dependencies) for task in tasks}
        children: dict[str, list[str]] = defaultdict(list)
        for task in tasks:
            for dependency in task.dependencies:
                children[dependency].append(task.id)
        queue = deque(task_id for task_id, count in indegree.items() if count == 0)
        visited = 0
        while queue:
            current = queue.popleft()
            visited += 1
            for child in children[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        return visited != len(tasks)
