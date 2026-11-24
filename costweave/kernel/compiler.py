"""Verifiable-cut hypergraph compiler (CostWeave Next)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING
import math
import re
from typing import Any

from .contracts import Job, Operation, canonical_json
from .verifiers import CRCCalibrator, CRCThresholdResult, CalibrationSample


V0_VALIDATORS = frozenset({"equals", "json_object", "json_schema_strict", "invariants", "python_check"})
_CLEAN_NAME_RE = re.compile(r"[^A-Za-z0-9._:-]+")


def _safe_id(raw: str) -> str:
    cleaned = _CLEAN_NAME_RE.sub("_", raw).strip("_")
    return cleaned[:150] or "item"


def estimate_tokens(value: Any) -> int:
    """Deterministic UTF-8 byte-based token estimator (~4 bytes/token)."""
    if isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = canonical_json(value).encode("utf-8")
    return max(1, (len(raw) + 3) // 4)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    executor: str
    provider_family: str
    input_price_per_mtok_usd: float
    output_price_per_mtok_usd: float
    cached_input_price_per_mtok_usd: float | None = None
    reasoning_price_per_mtok_usd: float | None = None
    context_window_tokens: int = 128_000
    quote_output_tokens: int = 1024
    latency_p50_ms: int = 1200
    latency_p95_ms: int = 2400
    base_pass_prob: float = 0.85
    routable: bool = True
    modalities: tuple[str, ...] = ("text",)

    def __post_init__(self) -> None:
        if not self.id or not self.executor or not self.provider_family:
            raise ValueError("ModelSpec requires non-empty id, executor, and provider_family")
        if self.input_price_per_mtok_usd < 0 or self.output_price_per_mtok_usd < 0:
            raise ValueError("ModelSpec prices must be non-negative")
        if self.context_window_tokens < 1 or self.quote_output_tokens < 1:
            raise ValueError("ModelSpec token limits must be positive")
        if not 0.0 < self.base_pass_prob <= 1.0:
            raise ValueError("ModelSpec base_pass_prob must be in (0, 1]")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ModelSpec:
        return cls(
            id=str(raw["id"]),
            executor=str(raw["executor"]),
            provider_family=str(raw["provider_family"]),
            input_price_per_mtok_usd=float(raw["input_price_per_mtok_usd"]),
            output_price_per_mtok_usd=float(raw["output_price_per_mtok_usd"]),
            cached_input_price_per_mtok_usd=(
                float(raw["cached_input_price_per_mtok_usd"])
                if raw.get("cached_input_price_per_mtok_usd") is not None
                else None
            ),
            reasoning_price_per_mtok_usd=(
                float(raw["reasoning_price_per_mtok_usd"])
                if raw.get("reasoning_price_per_mtok_usd") is not None
                else None
            ),
            context_window_tokens=int(raw.get("context_window_tokens", 128_000)),
            quote_output_tokens=int(raw.get("quote_output_tokens", 1024)),
            latency_p50_ms=int(raw.get("latency_p50_ms", 1200)),
            latency_p95_ms=int(raw.get("latency_p95_ms", 2400)),
            base_pass_prob=float(raw.get("base_pass_prob", 0.85)),
            routable=bool(raw.get("routable", True)),
            modalities=tuple(str(m) for m in raw.get("modalities", ("text",))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "executor": self.executor,
            "provider_family": self.provider_family,
            "input_price_per_mtok_usd": self.input_price_per_mtok_usd,
            "output_price_per_mtok_usd": self.output_price_per_mtok_usd,
            "cached_input_price_per_mtok_usd": self.cached_input_price_per_mtok_usd,
            "reasoning_price_per_mtok_usd": self.reasoning_price_per_mtok_usd,
            "context_window_tokens": self.context_window_tokens,
            "quote_output_tokens": self.quote_output_tokens,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "base_pass_prob": self.base_pass_prob,
            "routable": self.routable,
            "modalities": list(self.modalities),
        }


@dataclass(frozen=True, slots=True)
class SubtaskSpec:
    name: str
    prompt: str
    requires: tuple[str, ...] = ()
    validator: str = "present"
    validator_config: dict[str, Any] = field(default_factory=dict)
    expected_output_tokens: int | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SubtaskSpec:
        return cls(
            name=str(raw["name"]),
            prompt=str(raw["prompt"]),
            requires=tuple(str(x) for x in raw.get("requires", ())),
            validator=str(raw.get("validator", "present")),
            validator_config=dict(raw.get("validator_config", {})),
            expected_output_tokens=(
                int(raw["expected_output_tokens"])
                if raw.get("expected_output_tokens") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "prompt": self.prompt,
            "requires": list(self.requires),
            "validator": self.validator,
            "validator_config": dict(self.validator_config),
            "expected_output_tokens": self.expected_output_tokens,
        }


@dataclass(frozen=True, slots=True)
class TaskSpec:
    goal_name: str
    prompt: str
    inputs: dict[str, Any]
    budget_units: int
    acceptance_validator: str = "present"
    acceptance_config: dict[str, Any] = field(default_factory=dict)
    max_risk_alpha: float = 1.0
    failure_penalty_units: int | None = None
    corruption_penalty_units: int = 0
    deadline_ms: int | None = None
    latency_weight_units_per_sec: float = 0.0
    max_parallel: int = 4
    required_modalities: tuple[str, ...] = ("text",)
    subtasks: tuple[SubtaskSpec, ...] = ()
    chunks_input_key: str | None = None
    reduce_prompt: str | None = None
    consensus_mode: str = "exact_canonical"
    consensus_threshold: float = 1.0
    search_limit: int = 10_000

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TaskSpec:
        return cls(
            goal_name=str(raw["goal_name"]),
            prompt=str(raw["prompt"]),
            inputs=dict(raw.get("inputs", {})),
            budget_units=int(raw["budget_units"]),
            acceptance_validator=str(raw.get("acceptance_validator", "present")),
            acceptance_config=dict(raw.get("acceptance_config", {})),
            max_risk_alpha=float(raw.get("max_risk_alpha", 1.0)),
            failure_penalty_units=(
                int(raw["failure_penalty_units"])
                if raw.get("failure_penalty_units") is not None
                else None
            ),
            corruption_penalty_units=int(raw.get("corruption_penalty_units", 0)),
            deadline_ms=int(raw["deadline_ms"]) if raw.get("deadline_ms") is not None else None,
            latency_weight_units_per_sec=float(raw.get("latency_weight_units_per_sec", 0.0)),
            max_parallel=int(raw.get("max_parallel", 4)),
            required_modalities=tuple(str(m) for m in raw.get("required_modalities", ("text",))),
            subtasks=tuple(SubtaskSpec.from_dict(item) for item in raw.get("subtasks", ())),
            chunks_input_key=(
                str(raw["chunks_input_key"]) if raw.get("chunks_input_key") is not None else None
            ),
            reduce_prompt=(
                str(raw["reduce_prompt"]) if raw.get("reduce_prompt") is not None else None
            ),
            consensus_mode=str(raw.get("consensus_mode", "exact_canonical")),
            consensus_threshold=float(raw.get("consensus_threshold", 1.0)),
            search_limit=int(raw.get("search_limit", 10_000)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_name": self.goal_name,
            "prompt": self.prompt,
            "inputs": dict(self.inputs),
            "budget_units": self.budget_units,
            "acceptance_validator": self.acceptance_validator,
            "acceptance_config": dict(self.acceptance_config),
            "max_risk_alpha": self.max_risk_alpha,
            "failure_penalty_units": self.failure_penalty_units,
            "corruption_penalty_units": self.corruption_penalty_units,
            "deadline_ms": self.deadline_ms,
            "latency_weight_units_per_sec": self.latency_weight_units_per_sec,
            "max_parallel": self.max_parallel,
            "required_modalities": list(self.required_modalities),
            "subtasks": [s.to_dict() for s in self.subtasks],
            "chunks_input_key": self.chunks_input_key,
            "reduce_prompt": self.reduce_prompt,
            "consensus_mode": self.consensus_mode,
            "consensus_threshold": self.consensus_threshold,
            "search_limit": self.search_limit,
        }


def _price_micro_usd(model: ModelSpec, prompt_tokens: int, output_tokens: int) -> int:
    if model.input_price_per_mtok_usd == 0.0 and model.output_price_per_mtok_usd == 0.0:
        return 0
    total = (
        Decimal(max(0, prompt_tokens)) * Decimal(str(model.input_price_per_mtok_usd))
        + Decimal(max(0, output_tokens)) * Decimal(str(model.output_price_per_mtok_usd))
    )
    units = int(total.to_integral_value(rounding=ROUND_CEILING))
    return max(1, units)


def _route_estimates(
    model: ModelSpec,
    prompt_text: str,
    inputs_payload: Mapping[str, Any],
    prompt_tokens: int,
    default_out_tokens: int,
    beliefs: Any | None,
) -> tuple[float, float, int]:
    """Return (pass_prob, expected_units, quote_units)."""
    if beliefs is not None and hasattr(beliefs, "predict_route"):
        p_lcb, exp_u, quote_u = beliefs.predict_route(
            model,
            prompt_text,
            inputs_payload,
            prompt_tokens=prompt_tokens,
            default_output_tokens=default_out_tokens,
        )
        p_clamped = min(0.9999, max(0.01, float(p_lcb)))
        q_int = max(0, int(quote_u))
        exp_val = min(float(q_int), max(0.0, float(exp_u)))
        return p_clamped, exp_val, q_int

    exp_out = min(model.quote_output_tokens, max(16, int(default_out_tokens)))
    quote_units = _price_micro_usd(model, prompt_tokens, model.quote_output_tokens)
    exp_units = float(_price_micro_usd(model, prompt_tokens, exp_out))
    return min(0.9999, max(0.01, float(model.base_pass_prob))), min(float(quote_units), exp_units), quote_units


def _validator_payload(
    validator: str,
    raw_config: Mapping[str, Any],
    prompt_text: str,
    requires: Sequence[str],
    *,
    is_artifact_acceptance: bool = False,
) -> tuple[str, dict[str, Any]]:
    cfg = dict(raw_config)
    if validator == "json_schema_strict":
        schema_body = cfg.get("schema", cfg)
        out_cfg: dict[str, Any] = {"schema": dict(schema_body)}
    elif validator == "invariants":
        out_cfg = dict(cfg)
        if "exact_substring_grounding" in out_cfg:
            specs = out_cfg["exact_substring_grounding"]
            spec_list = specs if isinstance(specs, list) else [specs]
            if is_artifact_acceptance or any(s.get("source_artifact") not in requires for s in spec_list if isinstance(s, Mapping)):
                out_cfg.pop("exact_substring_grounding", None)
    else:
        out_cfg = dict(cfg)
    if not is_artifact_acceptance:
        out_cfg["prompt"] = prompt_text
    return validator, out_cfg


def _pick_distinct_cheapest_to_strongest(models: Sequence[ModelSpec], limit: int = 3) -> list[ModelSpec]:
    if not models:
        return []
    ordered = sorted(models, key=lambda m: (_price_micro_usd(m, 500, m.quote_output_tokens), -m.base_pass_prob, m.id))
    if len(ordered) <= limit:
        return list(ordered)
    strongest = max(ordered, key=lambda m: (m.base_pass_prob, _price_micro_usd(m, 500, m.quote_output_tokens), m.id))
    picks: list[ModelSpec] = [ordered[0]]
    mid = ordered[len(ordered) // 2]
    if mid.id != ordered[0].id and mid.id != strongest.id:
        picks.append(mid)
    if all(p.id != strongest.id for p in picks):
        picks.append(strongest)
    for cand in ordered:
        if len(picks) >= limit:
            break
        if all(p.id != cand.id for p in picks):
            picks.append(cand)
    return picks[:limit]


def compile_task_hypergraph(
    task: TaskSpec,
    models: Sequence[ModelSpec],
    *,
    beliefs: Any | None = None,
    crc: CRCThresholdResult | Sequence[CalibrationSample] | CRCCalibrator | None = None,
) -> Job:
    """Compile a high-level TaskSpec into a BSAO*-ready artifact Job hypergraph."""
    if not task.goal_name or task.goal_name in task.inputs:
        raise ValueError("TaskSpec goal_name must be non-empty and not pre-supplied in inputs")

    req_mods = set(task.required_modalities)
    base_routable = [
        m for m in models
        if m.routable and req_mods <= set(m.modalities)
    ]

    full_input_tokens = estimate_tokens(task.prompt) + estimate_tokens(task.inputs)
    input_keys = tuple(sorted(task.inputs.keys()))
    has_v0 = task.acceptance_validator in V0_VALIDATORS

    eligible_full = sorted(
        (m for m in base_routable if full_input_tokens + m.quote_output_tokens <= m.context_window_tokens),
        key=lambda m: (_price_micro_usd(m, full_input_tokens, m.quote_output_tokens), -m.base_pass_prob, m.id),
    )

    operations: list[Operation] = []
    seen_op_ids: set[str] = set()

    def add_op(op: Operation) -> None:
        if op.id not in seen_op_ids:
            seen_op_ids.add(op.id)
            operations.append(op)

    goal_val, goal_acc_cfg = _validator_payload(
        task.acceptance_validator,
        task.acceptance_config,
        task.prompt,
        input_keys,
        is_artifact_acceptance=True,
    )
    acceptance: dict[str, dict[str, Any]] = {
        task.goal_name: {"validator": goal_val, "config": goal_acc_cfg},
    }

    op_val, op_cfg = _validator_payload(
        task.acceptance_validator,
        task.acceptance_config,
        task.prompt,
        input_keys,
        is_artifact_acceptance=False,
    )

    # 1. Monolithic Baselines (up to 3 cost-distinct options)
    monolithic = _pick_distinct_cheapest_to_strongest(eligible_full, limit=3)
    strongest_full = max(
        eligible_full,
        key=lambda m: (m.base_pass_prob, _price_micro_usd(m, full_input_tokens, m.quote_output_tokens), m.id),
    ) if eligible_full else None

    for idx, model in enumerate(monolithic):
        p_pass, exp_u, quote_u = _route_estimates(
            model,
            task.prompt,
            task.inputs,
            full_input_tokens,
            min(model.quote_output_tokens, 512),
            beliefs,
        )
        risk = 0.0 if has_v0 else max(0.0, round(1.0 - p_pass, 6))
        role = "speculative" if (idx == 0 and len(monolithic) > 1) else ("fallback" if model == strongest_full else "standard")
        add_op(
            Operation(
                id=_safe_id(f"direct_{model.id}"),
                requires=input_keys,
                produces=task.goal_name,
                executor=model.executor,
                validator=op_val,
                cost_units=quote_u,
                config=dict(op_cfg),
                route_role=role,
                expected_units=exp_u,
                pass_prob=p_pass,
                false_pass_risk=risk,
                latency_p50_ms=model.latency_p50_ms,
                latency_p95_ms=model.latency_p95_ms,
                provider_family=model.provider_family,
                max_attempts=1,
            )
        )

    # 2. Asymmetric GVR Cascade (cheap draft -> mid/strong repair) when V0 is present
    if has_v0 and len(eligible_full) >= 2:
        cheap_gvr = eligible_full[0]
        repair_model = eligible_full[1] if len(eligible_full) > 2 else strongest_full
        if repair_model is not None and repair_model.id != cheap_gvr.id:
            draft_prompt = f"{task.prompt}\n[Draft phase: return compact structured candidate JSON]"
            p_draft, exp_draft, quote_draft = _route_estimates(
                cheap_gvr,
                draft_prompt,
                task.inputs,
                full_input_tokens,
                min(cheap_gvr.quote_output_tokens, 384),
                beliefs,
            )
            add_op(
                Operation(
                    id=_safe_id(f"gvr_draft_{cheap_gvr.id}"),
                    requires=input_keys,
                    produces="gvr_draft",
                    executor=cheap_gvr.executor,
                    validator="present",
                    cost_units=quote_draft,
                    config={"prompt": draft_prompt},
                    route_role="speculative",
                    expected_units=exp_draft,
                    pass_prob=max(0.8, p_draft),
                    false_pass_risk=0.0,
                    latency_p50_ms=max(50, int(cheap_gvr.latency_p50_ms * 0.7)),
                    latency_p95_ms=max(50, int(cheap_gvr.latency_p95_ms * 0.7)),
                    provider_family=cheap_gvr.provider_family,
                )
            )
            repair_requires = tuple(sorted((*input_keys, "gvr_draft")))
            repair_prompt = f"{task.prompt}\n[Repair/Verify draft artifact against contract]"
            _, repair_cfg = _validator_payload(
                task.acceptance_validator,
                task.acceptance_config,
                repair_prompt,
                repair_requires,
                is_artifact_acceptance=False,
            )
            p_rep, exp_rep, quote_rep = _route_estimates(
                repair_model,
                repair_prompt,
                task.inputs,
                full_input_tokens + 256,
                min(repair_model.quote_output_tokens, 256),
                beliefs,
            )
            add_op(
                Operation(
                    id=_safe_id(f"gvr_repair_{repair_model.id}"),
                    requires=repair_requires,
                    produces=task.goal_name,
                    executor=repair_model.executor,
                    validator=op_val,
                    cost_units=quote_rep,
                    config=repair_cfg,
                    route_role="standard",
                    expected_units=exp_rep,
                    pass_prob=min(0.9999, max(p_rep, 0.94)),
                    false_pass_risk=0.0,
                    latency_p50_ms=max(50, int(repair_model.latency_p50_ms * 0.6)),
                    latency_p95_ms=max(50, int(repair_model.latency_p95_ms * 0.6)),
                    provider_family=repair_model.provider_family,
                )
            )

    # 3. Context-Slicing Map-Reduce when chunks_input_key has 2..12 items
    chunks = (
        task.inputs.get(task.chunks_input_key)
        if task.chunks_input_key is not None and task.chunks_input_key in task.inputs
        else None
    )
    if isinstance(chunks, list) and 2 <= len(chunks) <= 12:
        chunk_prompt_base = f"{task.prompt}\n[Map phase: extract verifiable facts from this single chunk only]"
        max_chunk_tokens = max(estimate_tokens(c) for c in chunks) + estimate_tokens(chunk_prompt_base)
        eligible_map = sorted(
            (m for m in base_routable if max_chunk_tokens + min(m.quote_output_tokens, 384) <= m.context_window_tokens),
            key=lambda m: (_price_micro_usd(m, max_chunk_tokens, min(m.quote_output_tokens, 384)), -m.base_pass_prob, m.id),
        )
        reduce_prompt = task.reduce_prompt or f"{task.prompt}\n[Reduce phase: merge mapped chunk outputs into final contract]"
        reduce_tokens = estimate_tokens(reduce_prompt) + len(chunks) * 256
        eligible_reduce = sorted(
            (m for m in base_routable if reduce_tokens + m.quote_output_tokens <= m.context_window_tokens),
            key=lambda m: (-m.base_pass_prob, _price_micro_usd(m, reduce_tokens, m.quote_output_tokens), m.id),
        )
        if eligible_map and eligible_reduce:
            map_model = eligible_map[0]
            reduce_model = eligible_reduce[0]
            mapped_names: list[str] = []
            for idx, chunk_val in enumerate(chunks):
                chunk_name = f"chunk_{idx}"
                mapped_name = f"mapped_{idx}"
                mapped_names.append(mapped_name)
                add_op(
                    Operation(
                        id=_safe_id(f"slice_{idx}"),
                        requires=(task.chunks_input_key,),
                        produces=chunk_name,
                        executor="pick",
                        validator="present",
                        cost_units=0,
                        config={"source": task.chunks_input_key, "path": [idx]},
                        expected_units=0.0,
                        pass_prob=1.0,
                        false_pass_risk=0.0,
                        latency_p50_ms=0,
                        latency_p95_ms=0,
                        provider_family="local",
                    )
                )
                c_tokens = estimate_tokens(chunk_val) + estimate_tokens(chunk_prompt_base)
                p_map, exp_map, quote_map = _route_estimates(
                    map_model,
                    chunk_prompt_base,
                    {chunk_name: chunk_val},
                    c_tokens,
                    min(map_model.quote_output_tokens, 256),
                    beliefs,
                )
                add_op(
                    Operation(
                        id=_safe_id(f"map_{idx}_{map_model.id}"),
                        requires=(chunk_name,),
                        produces=mapped_name,
                        executor=map_model.executor,
                        validator="present",
                        cost_units=quote_map,
                        config={"prompt": f"{chunk_prompt_base} (chunk {idx})"},
                        expected_units=exp_map,
                        pass_prob=min(0.9999, max(0.85, p_map)),
                        false_pass_risk=0.0,
                        latency_p50_ms=max(50, int(map_model.latency_p50_ms * 0.5)),
                        latency_p95_ms=max(50, int(map_model.latency_p95_ms * 0.5)),
                        provider_family=map_model.provider_family,
                    )
                )
            add_op(
                Operation(
                    id="collect_mapped",
                    requires=tuple(mapped_names),
                    produces="mapped_bundle",
                    executor="collect",
                    validator="present",
                    cost_units=0,
                    config={},
                    expected_units=0.0,
                    pass_prob=1.0,
                    false_pass_risk=0.0,
                    latency_p50_ms=0,
                    latency_p95_ms=0,
                    provider_family="local",
                )
            )
            red_reqs = ("mapped_bundle",)
            red_val, red_cfg = _validator_payload(
                task.acceptance_validator,
                task.acceptance_config,
                reduce_prompt,
                red_reqs,
                is_artifact_acceptance=False,
            )
            p_red, exp_red, quote_red = _route_estimates(
                reduce_model,
                reduce_prompt,
                {"mapped_bundle": [f"mapped_{i}" for i in range(len(chunks))]},
                reduce_tokens,
                min(reduce_model.quote_output_tokens, 384),
                beliefs,
            )
            add_op(
                Operation(
                    id=_safe_id(f"reduce_{reduce_model.id}"),
                    requires=red_reqs,
                    produces=task.goal_name,
                    executor=reduce_model.executor,
                    validator=red_val,
                    cost_units=quote_red,
                    config=red_cfg,
                    expected_units=exp_red,
                    pass_prob=p_red,
                    false_pass_risk=0.0 if has_v0 else max(0.0, round(1.0 - p_red, 6)),
                    latency_p50_ms=max(50, int(reduce_model.latency_p50_ms * 0.6)),
                    latency_p95_ms=max(50, int(reduce_model.latency_p95_ms * 0.6)),
                    provider_family=reduce_model.provider_family,
                )
            )

    # 4. Contract-Locked Parallel Subtask Fan-Out
    if len(task.subtasks) >= 2 and base_routable:
        fanout_models = _pick_distinct_cheapest_to_strongest(eligible_full or base_routable, limit=2)
        synth_model = strongest_full or eligible_full[-1] if eligible_full else max(base_routable, key=lambda m: m.base_pass_prob)
        sub_artifacts: list[str] = []
        fanout_valid = True
        pending_fanout: list[Operation] = []
        pending_acc: dict[str, dict[str, Any]] = {}

        for sub in task.subtasks:
            art_name = f"sub_{_safe_id(sub.name)}"
            sub_artifacts.append(art_name)
            sub_reqs = sub.requires if sub.requires else input_keys
            sub_tokens = estimate_tokens(sub.prompt) + sum(estimate_tokens(task.inputs[k]) for k in sub_reqs if k in task.inputs)
            sub_eligible = [m for m in fanout_models if sub_tokens + m.quote_output_tokens <= m.context_window_tokens]
            if not sub_eligible:
                fanout_valid = False
                break
            sub_acc_val, sub_acc_cfg = _validator_payload(
                sub.validator, sub.validator_config, sub.prompt, sub_reqs, is_artifact_acceptance=True
            )
            sub_op_val, sub_op_cfg = _validator_payload(
                sub.validator, sub.validator_config, sub.prompt, sub_reqs, is_artifact_acceptance=False
            )
            if len(sub_eligible) > 1 or sub.validator in V0_VALIDATORS:
                pending_acc[art_name] = {"validator": sub_acc_val, "config": sub_acc_cfg}
            for m_sub in sub_eligible:
                out_tok = sub.expected_output_tokens or min(m_sub.quote_output_tokens, 384)
                p_sub, exp_sub, quote_sub = _route_estimates(
                    m_sub, sub.prompt, {k: task.inputs[k] for k in sub_reqs if k in task.inputs},
                    sub_tokens, out_tok, beliefs,
                )
                sub_v0 = sub.validator in V0_VALIDATORS
                pending_fanout.append(
                    Operation(
                        id=_safe_id(f"{art_name}_{m_sub.id}"),
                        requires=sub_reqs,
                        produces=art_name,
                        executor=m_sub.executor,
                        validator=sub_op_val,
                        cost_units=quote_sub,
                        config=dict(sub_op_cfg),
                        expected_units=exp_sub,
                        pass_prob=p_sub,
                        false_pass_risk=0.0 if sub_v0 else max(0.0, round(1.0 - p_sub, 6)),
                        latency_p50_ms=m_sub.latency_p50_ms,
                        latency_p95_ms=m_sub.latency_p95_ms,
                        provider_family=m_sub.provider_family,
                    )
                )

        if fanout_valid:
            synth_reqs = tuple(dict.fromkeys((*input_keys, *sub_artifacts))) if any(
                s.get("source_artifact") in input_keys
                for s in (
                    task.acceptance_config.get("exact_substring_grounding", [])
                    if isinstance(task.acceptance_config.get("exact_substring_grounding"), list)
                    else [task.acceptance_config.get("exact_substring_grounding")]
                )
                if isinstance(s, Mapping)
            ) else tuple(sub_artifacts)
            synth_prompt = task.reduce_prompt or f"{task.prompt}\n[Synthesize verified sub-artifacts]"
            synth_tokens = estimate_tokens(synth_prompt) + len(sub_artifacts) * 256
            synth_val, synth_cfg = _validator_payload(
                task.acceptance_validator, task.acceptance_config, synth_prompt, synth_reqs, is_artifact_acceptance=False
            )
            p_syn, exp_syn, quote_syn = _route_estimates(
                synth_model, synth_prompt, {"subtasks": sub_artifacts}, synth_tokens, 256, beliefs,
            )
            for op in pending_fanout:
                add_op(op)
            acceptance.update(pending_acc)
            add_op(
                Operation(
                    id=_safe_id(f"synth_{synth_model.id}"),
                    requires=synth_reqs,
                    produces=task.goal_name,
                    executor=synth_model.executor,
                    validator=synth_val,
                    cost_units=quote_syn,
                    config=synth_cfg,
                    expected_units=exp_syn,
                    pass_prob=min(0.9999, max(0.95, p_syn)),
                    false_pass_risk=0.0 if has_v0 else max(0.0, round(1.0 - p_syn, 6)),
                    latency_p50_ms=max(50, int(synth_model.latency_p50_ms * 0.5)),
                    latency_p95_ms=max(50, int(synth_model.latency_p95_ms * 0.5)),
                    provider_family=synth_model.provider_family,
                )
            )

    # 5. Cross-Family Orthogonal Dual-Cheap Consensus V1 (when no objective V0 exists)
    if not has_v0 and len(eligible_full) >= 2:
        m_left = eligible_full[0]
        m_right = next(
            (m for m in eligible_full[1:] if m.provider_family.lower() != m_left.provider_family.lower()),
            None,
        )
        if m_right is not None:
            p_l, exp_l, q_l = _route_estimates(m_left, task.prompt, task.inputs, full_input_tokens, 384, beliefs)
            p_r, exp_r, q_r = _route_estimates(m_right, task.prompt, task.inputs, full_input_tokens, 384, beliefs)
            add_op(
                Operation(
                    id=_safe_id(f"cons_left_{m_left.id}"),
                    requires=input_keys,
                    produces="cons_cand_left",
                    executor=m_left.executor,
                    validator="present",
                    cost_units=q_l,
                    config={"prompt": task.prompt},
                    expected_units=exp_l,
                    pass_prob=max(0.9, p_l),
                    false_pass_risk=0.0,
                    latency_p50_ms=m_left.latency_p50_ms,
                    latency_p95_ms=m_left.latency_p95_ms,
                    provider_family=m_left.provider_family,
                )
            )
            add_op(
                Operation(
                    id=_safe_id(f"cons_right_{m_right.id}"),
                    requires=input_keys,
                    produces="cons_cand_right",
                    executor=m_right.executor,
                    validator="present",
                    cost_units=q_r,
                    config={"prompt": task.prompt},
                    expected_units=exp_r,
                    pass_prob=max(0.9, p_r),
                    false_pass_risk=0.0,
                    latency_p50_ms=m_right.latency_p50_ms,
                    latency_p95_ms=m_right.latency_p95_ms,
                    provider_family=m_right.provider_family,
                )
            )
            v1_threshold = task.consensus_threshold
            if isinstance(crc, CRCThresholdResult):
                if crc.admissible and math.isfinite(crc.threshold):
                    v1_risk = round(crc.risk_ucb, 6)
                    v1_threshold = max(task.consensus_threshold, min(1.0, crc.threshold))
                else:
                    v1_risk = 1.0
            elif isinstance(crc, Sequence):
                cal = CRCCalibrator().calibrate(crc, alpha=min(1.0, max(0.0, task.max_risk_alpha)))
                if cal.admissible and math.isfinite(cal.threshold):
                    v1_risk = round(cal.risk_ucb, 6)
                    v1_threshold = max(task.consensus_threshold, min(1.0, cal.threshold))
                else:
                    v1_risk = 1.0
            else:
                v1_risk = 1.0
            agree_prob = min(0.95, max(0.50, round(p_l * p_r + 0.05, 4)))
            add_op(
                Operation(
                    id="cons_gate",
                    requires=("cons_cand_left", "cons_cand_right"),
                    produces=task.goal_name,
                    executor="consensus_pick",
                    validator="orthogonal_consensus",
                    cost_units=0,
                    config={
                        "left_artifact": "cons_cand_left",
                        "right_artifact": "cons_cand_right",
                        "left_family": m_left.provider_family,
                        "right_family": m_right.provider_family,
                        "mode": task.consensus_mode,
                        "min_threshold": v1_threshold,
                    },
                    expected_units=0.0,
                    pass_prob=agree_prob,
                    false_pass_risk=v1_risk,
                    latency_p50_ms=1,
                    latency_p95_ms=5,
                    provider_family="consensus",
                )
            )

    if not operations:
        raise ValueError("no routable models satisfy modality and context-window constraints")

    # Guarantee every multi-producer artifact has an acceptance rule.
    counts = Counter(op.produces for op in operations)
    for art, count in counts.items():
        if count > 1 and art not in acceptance:
            acceptance[art] = {"validator": "present", "config": {}}

    return Job(
        goals=(task.goal_name,),
        inputs=task.inputs,
        budget_units=task.budget_units,
        operations=operations,
        search_limit=task.search_limit,
        acceptance=acceptance,
        planning_policy="bsao",
        max_risk_alpha=task.max_risk_alpha,
        failure_penalty_units=task.failure_penalty_units,
        corruption_penalty_units=task.corruption_penalty_units,
        deadline_ms=task.deadline_ms,
        latency_weight_units_per_sec=task.latency_weight_units_per_sec,
        max_parallel=task.max_parallel,
    )
