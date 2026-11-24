"""Tiered verifiers (V0 deterministic, V1 cross-family consensus) and Split Conformal Risk Control."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .contracts import Operation, canonical_json

if TYPE_CHECKING:
    from .ports import Registry


_MAX_SCHEMA_DEPTH = 16
_MAX_PYTHON_OUTPUT_BYTES = 65_536


# ---------------------------------------------------------------------------
# V0 Deterministic Validators
# ---------------------------------------------------------------------------

def _check_json_schema_config(op: Operation) -> None:
    schema = op.config.get("schema", op.config)
    if not isinstance(schema, dict):
        raise ValueError(f"{op.id} requires a JSON schema object")


def _validate_schema_node(value: Any, schema: Mapping[str, Any], depth: int = 0) -> bool:
    if depth > _MAX_SCHEMA_DEPTH:
        return False
    expected_type = schema.get("type")
    if expected_type is not None:
        if expected_type == "object":
            if not isinstance(value, dict):
                return False
        elif expected_type == "array":
            if not isinstance(value, list):
                return False
        elif expected_type == "string":
            if not isinstance(value, str):
                return False
        elif expected_type == "integer":
            if type(value) is not int:
                return False
        elif expected_type == "number":
            if type(value) not in (int, float) or not math.isfinite(value):
                return False
        elif expected_type == "boolean":
            if type(value) is not bool:
                return False
        elif expected_type == "null":
            if value is not None:
                return False
        else:
            return False

    if "enum" in schema:
        encoded = canonical_json(value)
        if not any(canonical_json(item) == encoded for item in schema["enum"]):
            return False

    if isinstance(value, (int, float)) and type(value) is not bool:
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False

    if isinstance(value, str):
        if "min_length" in schema and len(value) < schema["min_length"]:
            return False
        if "max_length" in schema and len(value) > schema["max_length"]:
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False

    if isinstance(value, list):
        if "min_items" in schema and len(value) < schema["min_items"]:
            return False
        if "max_items" in schema and len(value) > schema["max_items"]:
            return False
        if "items" in schema and isinstance(schema["items"], Mapping):
            if not all(_validate_schema_node(item, schema["items"], depth + 1) for item in value):
                return False

    if isinstance(value, dict):
        required = schema.get("required", ())
        if not all(key in value for key in required):
            return False
        props = schema.get("properties", {})
        if schema.get("additional_properties", True) is False:
            if set(value.keys()) - set(props.keys()):
                return False
        for key, sub_schema in props.items():
            if key in value and isinstance(sub_schema, Mapping):
                if not _validate_schema_node(value[key], sub_schema, depth + 1):
                    return False
    return True


def verify_json_schema_strict(op: Operation, value: Any, inputs: Mapping[str, Any]) -> bool:
    schema = op.config.get("schema", op.config)
    return _validate_schema_node(value, schema)


def _check_invariants_config(op: Operation) -> None:
    cfg = op.config
    if not isinstance(cfg, dict):
        raise ValueError(f"{op.id} invariants config must be a JSON object")
    for item in _as_list(cfg.get("exact_substring_grounding", ())):
        if not isinstance(item, Mapping) or item.get("source_artifact") not in op.requires:
            # Allow empty requires when used as artifact acceptance rule; checked at runtime if missing.
            if op.requires:
                raise ValueError(f"{op.id} exact_substring_grounding source_artifact must be in requires")


def _as_list(raw: Any) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    return []


def verify_invariants(op: Operation, value: Any, inputs: Mapping[str, Any]) -> bool:
    if not isinstance(value, Mapping):
        return False
    cfg = op.config

    for field in cfg.get("non_empty_fields", ()):
        val = value.get(field)
        if val is None or (isinstance(val, (str, list, dict)) and len(val) == 0):
            return False

    for spec in _as_list(cfg.get("sum_equals")):
        items = value.get(spec["items_field"])
        total = value.get(spec["total_field"])
        if not isinstance(items, list) or type(total) not in (int, float) or type(total) is bool:
            return False
        item_key = spec.get("item_key")
        acc = 0.0
        for row in items:
            num = row.get(item_key) if (item_key and isinstance(row, Mapping)) else row
            if type(num) not in (int, float) or type(num) is bool or not math.isfinite(num):
                return False
            acc += float(num)
        tol = float(spec.get("abs_tol", 1e-6))
        if abs(acc - float(total)) > tol:
            return False

    for spec in _as_list(cfg.get("exact_substring_grounding")):
        src_name = spec["source_artifact"]
        if src_name not in inputs:
            return False
        source_obj = inputs[src_name]
        if "source_field" in spec and isinstance(source_obj, Mapping):
            source_obj = source_obj.get(spec["source_field"])
        source_text = source_obj if isinstance(source_obj, str) else canonical_json(source_obj)
        quotes = value.get(spec["quotes_field"])
        if not isinstance(quotes, list) or (spec.get("require_non_empty", True) and not quotes):
            return False
        for quote in quotes:
            if not isinstance(quote, str) or not quote.strip() or quote not in source_text:
                return False

    return True


def _check_python_check_config(op: Operation) -> None:
    code = op.config.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError(f"{op.id} python_check requires non-empty config.code")
    timeout = op.config.get("timeout_seconds", 3.0)
    if type(timeout) not in (int, float) or not 0.1 <= float(timeout) <= 10.0:
        raise ValueError(f"{op.id} python_check timeout_seconds must be in [0.1, 10.0]")


_SANDBOX_RUNNER = """
import json, sys
payload = json.loads(sys.stdin.read())
ns = {"value": payload["value"], "inputs": payload["inputs"]}
exec(payload["code"], ns)
"""


def verify_python_check(op: Operation, value: Any, inputs: Mapping[str, Any]) -> bool:
    code = op.config["code"]
    timeout = float(op.config.get("timeout_seconds", 3.0))
    safe_env = {"PYTHONDONTWRITEBYTECODE": "1"}
    for keep in ("PATH", "SYSTEMROOT", "WINDIR"):
        if keep in os.environ:
            safe_env[keep] = os.environ[keep]
    stdin_payload = json.dumps({"value": value, "inputs": dict(inputs), "code": code}, allow_nan=False)
    with tempfile.TemporaryDirectory(prefix="cw-v0-") as workdir:
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-B", "-c", _SANDBOX_RUNNER],
                input=stdin_payload,
                cwd=workdir,
                env=safe_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
    if len(proc.stdout.encode("utf-8", "ignore")) + len(proc.stderr.encode("utf-8", "ignore")) > _MAX_PYTHON_OUTPUT_BYTES:
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# V1 Cross-Family Orthogonal Consensus
# ---------------------------------------------------------------------------

def _numeric_leaves(obj: Any) -> list[float] | None:
    if type(obj) in (int, float) and type(obj) is not bool:
        return [float(obj)] if math.isfinite(obj) else None
    if isinstance(obj, list):
        out: list[float] = []
        for item in obj:
            sub = _numeric_leaves(item)
            if sub is None:
                return None
            out.extend(sub)
        return out
    if isinstance(obj, Mapping):
        out = []
        for k in sorted(obj.keys()):
            sub = _numeric_leaves(obj[k])
            if sub is None:
                return None
            out.extend(sub)
        return out
    return None


def _tokens_for(obj: Any) -> set[str]:
    if isinstance(obj, Mapping):
        return {f"{k}:{canonical_json(obj[k])}" for k in obj.keys()}
    if isinstance(obj, list):
        return {canonical_json(item) for item in obj}
    return {t for t in re.split(r"\s+", str(obj).strip().lower()) if t}


def agreement_score(left: Any, right: Any, *, mode: str = "exact_canonical", rel_tol: float = 1e-6) -> float:
    if mode == "exact_canonical":
        return 1.0 if canonical_json(left) == canonical_json(right) else 0.0
    if mode == "numeric_rel_tol":
        l_nums = _numeric_leaves(left)
        r_nums = _numeric_leaves(right)
        if l_nums is None or r_nums is None or len(l_nums) != len(r_nums) or not l_nums:
            return 1.0 if canonical_json(left) == canonical_json(right) else 0.0
        matches = sum(1 for a, b in zip(l_nums, r_nums) if math.isclose(a, b, rel_tol=rel_tol, abs_tol=1e-9))
        return matches / len(l_nums)
    if mode == "key_jaccard":
        lt = _tokens_for(left)
        rt = _tokens_for(right)
        if not lt and not rt:
            return 1.0
        if not lt or not rt:
            return 0.0
        return len(lt & rt) / len(lt | rt)
    raise ValueError(f"unsupported consensus mode: {mode}")


def _check_orthogonal_config(op: Operation) -> None:
    cfg = op.config
    left = cfg.get("left_artifact")
    right = cfg.get("right_artifact")
    lf = cfg.get("left_family")
    rf = cfg.get("right_family")
    if not isinstance(left, str) or not isinstance(right, str) or left == right:
        raise ValueError(f"{op.id} orthogonal_consensus requires distinct left_artifact and right_artifact")
    if op.requires and (left not in op.requires or right not in op.requires):
        raise ValueError(f"{op.id} left_artifact and right_artifact must be declared in requires")
    if not isinstance(lf, str) or not isinstance(rf, str) or not lf.strip() or not rf.strip():
        raise ValueError(f"{op.id} requires left_family and right_family")
    if lf.strip().lower() == rf.strip().lower():
        raise ValueError(f"{op.id} orthogonal_consensus requires distinct provider families")
    thresh = cfg.get("min_threshold", 1.0)
    if type(thresh) not in (int, float) or not 0.0 <= float(thresh) <= 1.0:
        raise ValueError(f"{op.id} min_threshold must be in [0.0, 1.0]")


def verify_orthogonal_consensus(op: Operation, value: Any, inputs: Mapping[str, Any]) -> bool:
    cfg = op.config
    left = cfg["left_artifact"]
    right = cfg["right_artifact"]
    if left not in inputs or right not in inputs:
        return False
    mode = str(cfg.get("mode", "exact_canonical"))
    rel_tol = float(cfg.get("rel_tol", 1e-6))
    min_threshold = float(cfg.get("min_threshold", 1.0))
    score = agreement_score(inputs[left], inputs[right], mode=mode, rel_tol=rel_tol)
    return score >= min_threshold


def register_tiered_verifiers(registry: "Registry") -> None:
    registry.register_validator("json_schema_strict", verify_json_schema_strict, check_config=_check_json_schema_config)
    registry.register_validator("invariants", verify_invariants, check_config=_check_invariants_config)
    registry.register_validator("python_check", verify_python_check, check_config=_check_python_check_config)
    registry.register_validator("orthogonal_consensus", verify_orthogonal_consensus, check_config=_check_orthogonal_config)
    registry.register_executor(
        "consensus_pick",
        lambda op, inputs, *, attempt_id: inputs[op.config["left_artifact"]],
        check_config=_check_orthogonal_config,
    )


# ---------------------------------------------------------------------------
# Split Conformal Risk Control (Fixed-Sequence Learn-Then-Test / Clopper-Pearson)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CalibrationSample:
    score: float
    loss: float

    def __post_init__(self) -> None:
        if type(self.score) not in (int, float) or not math.isfinite(self.score) or not 0.0 <= float(self.score) <= 1.0:
            raise ValueError("CalibrationSample.score must be finite in [0.0, 1.0]")
        if type(self.loss) not in (int, float) or not math.isfinite(self.loss) or not 0.0 <= float(self.loss) <= 1.0:
            raise ValueError("CalibrationSample.loss must be finite in [0.0, 1.0]")


@dataclass(frozen=True, slots=True)
class CRCThresholdResult:
    admissible: bool
    threshold: float
    empirical_risk: float
    risk_ucb: float
    coverage: float
    sample_count: int


def _binomial_cdf_le(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    log_p = math.log(p)
    log_q = math.log1p(-p)
    log_n_fact = math.lgamma(n + 1)
    total = 0.0
    for j in range(0, k + 1):
        log_prob = log_n_fact - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * log_p + (n - j) * log_q
        total += math.exp(log_prob)
    return min(1.0, total)


def clopper_pearson_upper(loss_sum: float, n: int, delta: float = 0.05) -> float:
    """Exact one-sided finite-sample upper confidence bound for bounded losses in [0, 1]."""
    if n <= 0:
        return 1.0
    k = math.ceil(loss_sum - 1e-12)
    if k >= n:
        return 1.0
    low = max(0.0, k / n)
    high = 1.0
    for _ in range(52):
        mid = 0.5 * (low + high)
        if _binomial_cdf_le(k, n, mid) > delta:
            low = mid
        else:
            high = mid
    return high


class CRCCalibrator:
    """Finite-sample Split Conformal Risk Control calibrator with math-native cold-start fail-closed."""

    def __init__(self, grid_steps: int = 100):
        if grid_steps < 10:
            raise ValueError("grid_steps must be >= 10")
        self.thresholds = tuple(round(1.0 - i / grid_steps, 6) for i in range(grid_steps + 1))

    def calibrate(
        self,
        samples: Sequence[CalibrationSample],
        *,
        alpha: float,
        delta: float = 0.05,
    ) -> CRCThresholdResult:
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("alpha must be in [0.0, 1.0]")
        if not 0.0 < delta < 1.0:
            raise ValueError("delta must be in (0.0, 1.0)")
        n = len(samples)
        if n == 0:
            return CRCThresholdResult(False, math.inf, 1.0, 1.0, 0.0, 0)

        best: CRCThresholdResult | None = None
        for tau in self.thresholds:
            accepted = [s for s in samples if s.score >= tau - 1e-12]
            loss_sum = math.fsum(s.loss for s in accepted)
            ucb = clopper_pearson_upper(loss_sum, n, delta=delta)
            if ucb > alpha + 1e-12:
                # Fixed-sequence Learn-Then-Test stops at the first threshold breach to preserve FWER <= delta.
                break
            best = CRCThresholdResult(
                admissible=True,
                threshold=tau,
                empirical_risk=loss_sum / n,
                risk_ucb=ucb,
                coverage=len(accepted) / n,
                sample_count=n,
            )

        if best is None:
            top_ucb = clopper_pearson_upper(
                math.fsum(s.loss for s in samples if s.score >= 1.0 - 1e-12),
                n,
                delta=delta,
            )
            return CRCThresholdResult(False, math.inf, 1.0, top_ucb, 0.0, n)
        return best
