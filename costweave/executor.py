from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

from .domain import TaskContract, WorkerProfile


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SimulatedExecutor:
    """Demonstrates the runtime contract without calling any external model."""

    async def execute(
        self,
        task: TaskContract,
        worker: WorkerProfile,
        dependency_results: list[dict],
        inject_fatal_error: bool = False,
    ) -> dict:
        seed = int(hashlib.sha256(f"{task.id}:{task.attempt}".encode()).hexdigest()[:6], 16)
        jitter = (seed % 180) / 1000
        await asyncio.sleep(max(.12, task.estimated_latency_ms / 1000 + jitter))

        if inject_fatal_error:
            return {
                "summary": "执行中发现核心假设与任务目标冲突。",
                "evidence": ["半程验收：依赖口径不一致", "模拟故障：要求全局重新规划"],
                "confidence": .22,
                "fatal_error": "核心假设失效，继续原计划预计成功率低于阈值",
            }

        confidence = min(.98, worker.reliability * .72 + task.predicted_success * .28)
        evidence = [
            f"任务合同 {task.id} 已读取",
            f"执行者 {worker.name} 完成结构化交付",
            f"已消费 {len(dependency_results)} 份上游验收成果",
        ]
        result = {
            "summary": f"{worker.name} 已完成“{task.title}”。",
            "deliverable": {
                "objective": task.objective,
                "covered_scope": task.include,
                "acceptance_criteria": task.acceptance_criteria,
                "note": "这是离线模拟成果，用于验证产品编排闭环；未调用真实AI模型。",
            },
            "evidence": evidence,
            "assumptions": ["当前为模拟执行模式", "真实模型适配器将在后续版本接入"],
            "confidence": round(confidence, 3),
        }
        if task.task_type in {"planning", "validation", "risk", "safety"}:
            result["decision"] = "pass"
            result["criteria_results"] = [
                {
                    "criterion": criterion,
                    "passed": True,
                    "evidence_refs": [evidence[0]],
                }
                for criterion in task.acceptance_criteria
            ]
        list_fields = {
            "requirements", "constraints", "open_questions", "plan_adjustments",
            "risks", "findings", "checks", "hazards", "human_review",
        }
        for field in task.output_schema:
            if field in result:
                continue
            if field in list_fields:
                result[field] = [f"{field} 已按 {task.id} 合同生成模拟条目"]
            elif field == "decision":
                result[field] = "pass"
            else:
                result[field] = f"{field} 的模拟结构化内容"
        return result
