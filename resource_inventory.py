"""Deterministic simulated resource inventory for v0.4.4."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Iterable

from .domain import WorkerProfile


@dataclass(frozen=True, slots=True)
class ResourcePool:
    pool_id: str
    provider: str
    local: bool
    slots_total: int
    cpu_cores_total: float
    gpu_count_total: int
    vram_mb_total: int
    queue_limit: int
    token_capacity: float
    token_refill_per_second: float
    state: str = "available"

    def to_dict(self) -> dict:
        return asdict(self)


def pool_id(provider: str, local: bool) -> str:
    digest = hashlib.sha256(
        f"{provider}:{'local' if local else 'cloud'}".encode("utf-8")
    ).hexdigest()[:24]
    return f"pool_{digest}"


class ResourceInventory:
    @staticmethod
    def from_workers(
        workers: Iterable[WorkerProfile],
        *,
        max_concurrency: int,
    ) -> list[ResourcePool]:
        groups: dict[tuple[str, bool], list[WorkerProfile]] = {}
        for worker in workers:
            groups.setdefault((worker.provider, worker.local), []).append(worker)
        pools: list[ResourcePool] = []
        for (provider, local), members in sorted(groups.items()):
            supports_gpu = any(
                {"image", "video"} & set(worker.modalities)
                for worker in members
            )
            slots = (
                max(1, min(int(max_concurrency), max(1, len(members)), 4))
                if local
                else max(2, min(16, int(max_concurrency) * 2))
            )
            pools.append(
                ResourcePool(
                    pool_id=pool_id(provider, local),
                    provider=provider,
                    local=local,
                    slots_total=slots,
                    cpu_cores_total=float(max(2, slots * 2)) if local else 0.0,
                    gpu_count_total=1 if local and supports_gpu else 0,
                    vram_mb_total=16_384 if local and supports_gpu else 0,
                    queue_limit=max(8, slots * 8),
                    token_capacity=2_000_000.0 if local else 5_000_000.0,
                    token_refill_per_second=100_000.0 if local else 250_000.0,
                )
            )
        return pools
