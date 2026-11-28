"""Factorized diagonal Laplace 2PL-IRT beliefs, output length forecaster, and Clipped-DR safe promotion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import json
import math
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .contracts import canonical_json
from .verifiers import clopper_pearson_upper


FEATURE_DIM = 16
_CODE_MARKERS = ("def ", "class ", "function ", "return ", "import ", "=>", "```", "interface ", "struct ")
_JSON_MARKERS = ("json", "schema", "required", "properties", "fields", "object", "array", "validate")
_MATH_MARKERS = ("sum", "total", "calculate", "count", "ratio", "average", "usd", "tax", "amount")
_EXTRACT_MARKERS = ("extract", "clause", "quote", "ground", "chunk", "section", "summarize", "bundle")
_REASON_MARKERS = ("compliance", "risk", "assess", "judge", "policy", "audit", "why", "analyze")
_CONSTRAINT_MARKERS = ("must", "strict", "exact", "only", "invariant", "never", "verified")


def _sigmoid(z: float) -> float:
    if z >= 35.0:
        return 1.0 - 1e-15
    if z <= -35.0:
        return 1e-15
    return 1.0 / (1.0 + math.exp(-z))


def _logit(p: float) -> float:
    clamped = min(1.0 - 1e-6, max(1e-6, float(p)))
    return math.log(clamped / (1.0 - clamped))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker_density(text_lower: str, markers: tuple[str, ...]) -> float:
    hits = sum(text_lower.count(m) for m in markers)
    return min(1.0, hits / max(2.0, len(markers) * 0.6))


def _inspect_structure(value: Any, depth: int = 0) -> tuple[int, int]:
    """Return (max_depth, max_list_length)."""
    if depth >= 16:
        return depth, 0
    if isinstance(value, Mapping):
        max_d, max_l = depth, 0
        for item in value.values():
            cd, cl = _inspect_structure(item, depth + 1)
            max_d = max(max_d, cd)
            max_l = max(max_l, cl)
        return max_d, max_l
    if isinstance(value, list):
        max_d, max_l = depth + 1, len(value)
        for item in value[:32]:
            cd, cl = _inspect_structure(item, depth + 1)
            max_d = max(max_d, cd)
            max_l = max(max_l, cl)
        return max_d, max_l
    return depth, 0


def extract_features(prompt: str, inputs: Mapping[str, Any]) -> tuple[float, ...]:
    """Deterministic zero-call 16-D context feature vector in [-1.0, 1.0]."""
    prompt_str = str(prompt or "")
    inputs_dict = dict(inputs or {})
    prompt_bytes = len(prompt_str.encode("utf-8"))
    inputs_bytes = len(canonical_json(inputs_dict).encode("utf-8"))
    total_bytes = prompt_bytes + inputs_bytes
    combined_lower = (prompt_str + "\n" + canonical_json(inputs_dict)[:4000]).lower()
    digits = sum(1 for ch in combined_lower[:2000] if ch.isdigit())
    braces = sum(combined_lower[:2000].count(ch) for ch in "{}[]();=")
    unique_syms = len(set(combined_lower[:1500].split()))
    max_depth, max_list = _inspect_structure(inputs_dict)
    line_count = prompt_str.count("\n") + 1

    return (
        1.0,
        min(1.0, math.log1p(prompt_bytes) / 10.0),
        min(1.0, math.log1p(inputs_bytes) / 14.0),
        min(1.0, math.log1p(total_bytes) / 14.0),
        _marker_density(combined_lower, _CODE_MARKERS),
        _marker_density(combined_lower, _JSON_MARKERS),
        _marker_density(combined_lower, _MATH_MARKERS),
        _marker_density(combined_lower, _EXTRACT_MARKERS),
        _marker_density(combined_lower, _REASON_MARKERS),
        _marker_density(combined_lower, _CONSTRAINT_MARKERS),
        min(1.0, digits / 60.0),
        min(1.0, braces / 80.0),
        min(1.0, len(inputs_dict) / 16.0),
        min(1.0, max_list / 16.0),
        min(1.0, max_depth / 8.0),
        min(1.0, (line_count / 32.0 + unique_syms / 128.0) * 0.5),
    )


@dataclass
class _RoutePosterior:
    mu: list[float]
    precision: list[float]
    prior_mu0: float
    observations: int = 0

    @classmethod
    def create(cls, prior_prob: float = 0.85) -> _RoutePosterior:
        m0 = _logit(prior_prob)
        mu = [m0] + [0.0] * (FEATURE_DIM - 1)
        prec = [4.0] + [3.0] * (FEATURE_DIM - 1)
        return cls(mu=mu, precision=prec, prior_mu0=m0, observations=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mu": list(self.mu),
            "precision": list(self.precision),
            "prior_mu0": self.prior_mu0,
            "observations": self.observations,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> _RoutePosterior:
        return cls(
            mu=[float(x) for x in raw["mu"]],
            precision=[float(x) for x in raw["precision"]],
            prior_mu0=float(raw.get("prior_mu0", _logit(0.85))),
            observations=int(raw.get("observations", 0)),
        )


class LaplaceIRTModel:
    """Factorized diagonal Laplace 2PL-IRT success probability estimator with LCB uncertainty."""

    def __init__(self, *, prior_slope_precision: float = 1.0) -> None:
        self._prior_slope_precision = float(prior_slope_precision)
        self._routes: dict[str, _RoutePosterior] = {}

    def _get(self, route_id: str, prior_prob: float) -> _RoutePosterior:
        post = self._routes.get(route_id)
        if post is None:
            post = _RoutePosterior.create(prior_prob=prior_prob)
            post.precision = [3.0] + [self._prior_slope_precision] * (FEATURE_DIM - 1)
            self._routes[route_id] = post
        return post

    def predict_pass_prob(
        self,
        route_id: str,
        features: Sequence[float],
        *,
        prior_prob: float = 0.85,
        lcb_z: float = 1.28,
    ) -> tuple[float, float, float]:
        """Return (mean_prob, lcb_prob, ucb_prob)."""
        if len(features) != FEATURE_DIM:
            raise ValueError(f"expected {FEATURE_DIM} features, got {len(features)}")
        post = self._routes.get(route_id)
        if post is None:
            mean_z = _logit(prior_prob)
            precs = [3.0] + [self._prior_slope_precision] * (FEATURE_DIM - 1)
            var_z = math.fsum((x * x) / pr for x, pr in zip(features, precs))
        else:
            mean_z = math.fsum(w * x for w, x in zip(post.mu, features))
            var_z = math.fsum((x * x) / max(1e-6, pr) for x, pr in zip(features, post.precision))
        std_z = math.sqrt(max(0.0, var_z))
        return (
            _sigmoid(mean_z),
            _sigmoid(mean_z - lcb_z * std_z),
            _sigmoid(mean_z + lcb_z * std_z),
        )

    def update(
        self,
        route_id: str,
        features: Sequence[float],
        passed: bool,
        *,
        prior_prob: float = 0.85,
        weight: float = 1.0,
    ) -> None:
        if len(features) != FEATURE_DIM:
            raise ValueError(f"expected {FEATURE_DIM} features, got {len(features)}")
        post = self._get(route_id, prior_prob=prior_prob)
        logit = math.fsum(w * x for w, x in zip(post.mu, features))
        p = _sigmoid(logit)
        target = 1.0 if passed else 0.0
        grad = (target - p) * float(weight)
        hess = max(1e-4, p * (1.0 - p)) * float(weight)
        prior_precs = [3.0] + [self._prior_slope_precision] * (FEATURE_DIM - 1)
        prior_mus = [post.prior_mu0] + [0.0] * (FEATURE_DIM - 1)
        for j, x in enumerate(features):
            new_prec = min(1_000_000.0, max(prior_precs[j], post.precision[j] + hess * x * x))
            eff_denom = max(1.0, min(new_prec, 12.0 if j == 0 else 4.0))
            step = (grad * x - 0.005 * prior_precs[j] * (post.mu[j] - prior_mus[j])) / eff_denom
            post.mu[j] = max(-8.0, min(8.0, post.mu[j] + max(-1.5, min(1.5, step))))
            post.precision[j] = new_prec
        post.observations += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "prior_slope_precision": self._prior_slope_precision,
            "routes": {k: v.to_dict() for k, v in sorted(self._routes.items())},
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LaplaceIRTModel:
        model = cls(prior_slope_precision=float(raw.get("prior_slope_precision", 1.0)))
        for k, v in (raw.get("routes") or {}).items():
            model._routes[str(k)] = _RoutePosterior.from_dict(v)
        return model


@dataclass
class _LengthState:
    weights: list[float]
    ema_var: float
    observations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": list(self.weights),
            "ema_var": self.ema_var,
            "observations": self.observations,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> _LengthState:
        return cls(
            weights=[float(x) for x in raw["weights"]],
            ema_var=float(raw["ema_var"]),
            observations=int(raw.get("observations", 0)),
        )


class OutputLengthForecaster:
    """Online regularized log-linear output token forecaster with P95 upper safety bound."""

    def __init__(self, *, lr: float = 0.25, l2: float = 0.02) -> None:
        self._lr = float(lr)
        self._l2 = float(l2)
        self._routes: dict[str, _LengthState] = {}

    def predict_tokens(
        self,
        route_id: str,
        features: Sequence[float],
        *,
        default_tokens: int = 384,
        max_tokens: int = 4096,
    ) -> tuple[int, int]:
        """Return (expected_tokens, p95_quote_tokens)."""
        if len(features) != FEATURE_DIM:
            raise ValueError(f"expected {FEATURE_DIM} features, got {len(features)}")
        state = self._routes.get(route_id)
        if state is None:
            y_hat = math.log1p(max(16, default_tokens))
            var = 0.20
        else:
            y_hat = math.fsum(w * x for w, x in zip(state.weights, features))
            var = max(0.02, state.ema_var)
        std = math.sqrt(var)
        exp_tok = max(16, min(max_tokens, round(math.expm1(y_hat))))
        quote_tok = max(exp_tok, min(max_tokens, math.ceil(math.expm1(y_hat + 1.645 * std))))
        return exp_tok, quote_tok

    def update(self, route_id: str, features: Sequence[float], completion_tokens: int, *, default_tokens: int = 384) -> None:
        if len(features) != FEATURE_DIM:
            raise ValueError(f"expected {FEATURE_DIM} features, got {len(features)}")
        target_y = math.log1p(max(1, int(completion_tokens)))
        state = self._routes.get(route_id)
        if state is None:
            w = [target_y] + [0.0] * (FEATURE_DIM - 1)
            state = _LengthState(weights=w, ema_var=0.16, observations=1)
            self._routes[route_id] = state
            return
        y_hat = math.fsum(wi * xi for wi, xi in zip(state.weights, features))
        err = target_y - y_hat
        norm2 = 1.0 + math.fsum(xi * xi for xi in features)
        eff_lr = self._lr / norm2
        for j, xi in enumerate(features):
            penalty = 0.0 if j == 0 else self._l2 * state.weights[j]
            state.weights[j] = max(-2.0, min(14.0, state.weights[j] + eff_lr * (err * xi - penalty)))
        state.ema_var = max(0.02, min(1.5, 0.85 * state.ema_var + 0.15 * (err * err)))
        state.observations += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "lr": self._lr,
            "l2": self._l2,
            "routes": {k: v.to_dict() for k, v in sorted(self._routes.items())},
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> OutputLengthForecaster:
        obj = cls(lr=float(raw.get("lr", 0.25)), l2=float(raw.get("l2", 0.02)))
        for k, v in (raw.get("routes") or {}).items():
            obj._routes[str(k)] = _LengthState.from_dict(v)
        return obj


@dataclass
class BeliefEngine:
    """Unified runtime belief interface consumed by compiler.py and store/promotion pipelines."""

    irt: LaplaceIRTModel = field(default_factory=LaplaceIRTModel)
    length_forecaster: OutputLengthForecaster = field(default_factory=OutputLengthForecaster)
    lcb_z: float = 1.28

    def predict_route(
        self,
        model: Any,
        prompt_text: str,
        inputs_payload: Mapping[str, Any],
        *,
        prompt_tokens: int,
        default_output_tokens: int = 384,
    ) -> tuple[float, float, int]:
        """Return (pass_prob_lcb, expected_units, quote_units)."""
        features = extract_features(prompt_text, inputs_payload)
        _, p_lcb, _ = self.irt.predict_pass_prob(
            model.id,
            features,
            prior_prob=model.base_pass_prob,
            lcb_z=self.lcb_z,
        )
        exp_out, quote_out = self.length_forecaster.predict_tokens(
            model.id,
            features,
            default_tokens=default_output_tokens,
            max_tokens=model.quote_output_tokens,
        )
        if model.input_price_per_mtok_usd == 0.0 and model.output_price_per_mtok_usd == 0.0:
            return p_lcb, 0.0, 0
        in_d = Decimal(max(0, prompt_tokens)) * Decimal(str(model.input_price_per_mtok_usd))
        out_price = Decimal(str(model.output_price_per_mtok_usd))
        exp_u = max(1, int((in_d + Decimal(exp_out) * out_price).to_integral_value(rounding=ROUND_CEILING)))
        quote_u = max(exp_u, int((in_d + Decimal(quote_out) * out_price).to_integral_value(rounding=ROUND_CEILING)))
        return p_lcb, float(exp_u), quote_u

    def observe(
        self,
        route_id: str,
        prompt_text: str,
        inputs_payload: Mapping[str, Any],
        *,
        passed: bool,
        completion_tokens: int,
        prior_prob: float = 0.85,
        default_tokens: int = 384,
    ) -> None:
        features = extract_features(prompt_text, inputs_payload)
        self.irt.update(route_id, features, passed, prior_prob=prior_prob)
        self.length_forecaster.update(route_id, features, completion_tokens, default_tokens=default_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lcb_z": self.lcb_z,
            "irt": self.irt.to_dict(),
            "length_forecaster": self.length_forecaster.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BeliefEngine:
        return cls(
            irt=LaplaceIRTModel.from_dict(raw.get("irt") or {}),
            length_forecaster=OutputLengthForecaster.from_dict(raw.get("length_forecaster") or {}),
            lcb_z=float(raw.get("lcb_z", 1.28)),
        )


def should_probe(
    uncertainty_width: float,
    baseline_units: int,
    probe_units: int,
    remaining_budget: int,
    *,
    expected_future_reuses: float = 20.0,
) -> bool:
    """Amortized Value-of-Information (VoI) shadow probe admission check."""
    if remaining_budget < probe_units or probe_units < 0 or uncertainty_width <= 0.02:
        return False
    voi = 0.25 * float(uncertainty_width) * max(1.0, float(baseline_units)) * max(1.0, float(expected_future_reuses))
    return voi > float(probe_units)


def clipped_dr_empirical_bernstein(
    observations: Sequence[Mapping[str, Any]],
    *,
    clip_max: float = 10.0,
    delta: float = 0.05,
    score_bounds: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Bound the clipped-score mean on independent evaluation observations.

    The protocol must supply a range covering both clipped and unclipped
    population scores, including unobserved losses. Without it, only
    descriptive statistics are available. Promotion rejects a sample with
    any clipped weight. On the remaining samples the reported bound equals
    the empirical bound computed on the original, unclipped scores.
    """
    n = len(observations)
    if not 0.0 < delta < 1.0:
        raise ValueError("delta must be in (0.0, 1.0)")
    if not math.isfinite(clip_max) or clip_max <= 0:
        raise ValueError("clip_max must be finite and positive")
    if score_bounds is not None:
        low, high = score_bounds
        if not math.isfinite(low) or not math.isfinite(high) or not math.isfinite(high - low) or high <= low:
            raise ValueError("score_bounds must declare a finite increasing range")
    psis: list[float] = []
    clipped = 0
    for obs in observations:
        y = float(obs["utility"])
        q_cand = float(obs.get("q_cand", 0.0))
        v_base = float(obs.get("baseline_value", 0.0))
        if "ips_weight" in obs:
            raw_w = float(obs["ips_weight"])
        else:
            chosen = bool(obs.get("action_matches", True))
            log_p = float(obs.get("propensity", 1.0))
            target_p = float(obs.get("target_propensity", 1.0 if chosen else 0.0))
            if not 0.0 < log_p <= 1.0 or not 0.0 <= target_p <= 1.0:
                raise ValueError("invalid logged or target propensity")
            raw_w = (target_p / log_p) if chosen else 0.0
        if not math.isfinite(raw_w) or raw_w < 0.0:
            raise ValueError("importance weights must be finite and nonnegative")
        clipped += int(raw_w > clip_max)
        correction = y - q_cand
        raw_score = (q_cand - v_base) + raw_w * correction
        score = (q_cand - v_base) + min(clip_max, raw_w) * correction
        if (not math.isfinite(raw_score) or not math.isfinite(score)
                or (score_bounds is not None and not low <= min(raw_score, score) <= max(raw_score, score) <= high)):
            raise ValueError("non-finite score or observation outside declared score_bounds")
        psis.append(score)

    mean_val = math.fsum(psis) / n if n else None
    var_val = math.fsum((x - mean_val) ** 2 for x in psis) / (n - 1) if n > 1 else 0.0
    stats = {"n": n, "mean": mean_val, "std": math.sqrt(var_val), "lcb": None, "ucb": None,
             "score_bounds": list(score_bounds) if score_bounds is not None else None, "clipped_count": clipped}
    if n < 2 or score_bounds is None:
        return stats
    span = high - low
    # Allocate delta across both displayed one-sided bounds.
    t = math.log(4.0 / delta)
    margin = math.sqrt((2.0 * var_val * t) / n) + (7.0 * span * t) / (3.0 * (n - 1))
    return {**stats, "lcb": max(low, mean_val - margin), "ucb": min(high, mean_val + margin)}


_BELIEF_SCHEMA = """
CREATE TABLE IF NOT EXISTS kernel_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    route_id TEXT NOT NULL,
    task_family TEXT NOT NULL,
    passed INTEGER NOT NULL,
    false_pass INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    utility REAL NOT NULL,
    baseline_utility REAL NOT NULL,
    q_hat REAL NOT NULL,
    propensity REAL NOT NULL,
    features_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kernel_belief_versions (
    version_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    engine_json TEXT NOT NULL,
    parent_version_id TEXT,
    promotion_report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kernel_active_belief (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version_id TEXT NOT NULL
);
"""


class BeliefStore:
    """SQLite-backed versioned store for safe Clipped-DR belief promotion and rollback."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_BELIEF_SCHEMA)
        self._ensure_initial_version()

    def close(self) -> None:
        self._conn.close()

    def _ensure_initial_version(self) -> None:
        with self._conn:
            row = self._conn.execute("SELECT version_id FROM kernel_active_belief WHERE id = 1").fetchone()
            if row is None:
                vid = "v0-baseline"
                self._conn.execute(
                    "INSERT OR IGNORE INTO kernel_belief_versions (version_id, created_at, engine_json, parent_version_id, promotion_report_json) VALUES (?, ?, ?, ?, ?)",
                    (vid, _utc_now(), json.dumps(BeliefEngine().to_dict()), None, json.dumps({"bootstrap": True})),
                )
                self._conn.execute(
                    "INSERT INTO kernel_active_belief (id, version_id) VALUES (1, ?)",
                    (vid,),
                )

    def active_version(self) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT b.version_id, b.created_at, b.parent_version_id, b.promotion_report_json
            FROM kernel_active_belief a
            JOIN kernel_belief_versions b ON a.version_id = b.version_id
            WHERE a.id = 1
            """
        ).fetchone()
        obs_count = self._conn.execute("SELECT COUNT(*) FROM kernel_observations").fetchone()[0]
        return {
            "version_id": row["version_id"],
            "created_at": row["created_at"],
            "parent_version_id": row["parent_version_id"],
            "promotion_report": json.loads(row["promotion_report_json"]),
            "observation_count": int(obs_count),
        }

    def load_active_engine(self) -> BeliefEngine:
        row = self._conn.execute(
            """
            SELECT b.engine_json
            FROM kernel_active_belief a
            JOIN kernel_belief_versions b ON a.version_id = b.version_id
            WHERE a.id = 1
            """
        ).fetchone()
        return BeliefEngine.from_dict(json.loads(row["engine_json"]))

    def record_observation(
        self,
        *,
        route_id: str,
        task_family: str,
        passed: bool,
        false_pass: bool = False,
        completion_tokens: int = 0,
        utility: float = 0.0,
        baseline_utility: float = 0.0,
        q_hat: float = 0.0,
        propensity: float = 1.0,
        features: Sequence[float] | None = None,
    ) -> None:
        feat_list = list(features) if features is not None else [1.0] + [0.0] * (FEATURE_DIM - 1)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO kernel_observations
                (created_at, route_id, task_family, passed, false_pass, completion_tokens, utility, baseline_utility, q_hat, propensity, features_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    route_id,
                    task_family,
                    1 if passed else 0,
                    1 if false_pass else 0,
                    max(0, int(completion_tokens)),
                    float(utility),
                    float(baseline_utility),
                    float(q_hat),
                    float(propensity),
                    json.dumps(feat_list),
                ),
            )

    def evaluate_and_promote(
        self,
        candidate: BeliefEngine,
        observations: Sequence[Mapping[str, Any]],
        *,
        clip_max: float = 10.0,
        delta: float = 0.05,
        min_samples: int = 20,
        version_id: str | None = None,
        score_bounds: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        stats = clipped_dr_empirical_bernstein(observations, clip_max=clip_max, delta=delta,
                                               score_bounds=score_bounds)
        promoted = (stats["n"] >= min_samples and stats["lcb"] is not None
                    and stats["lcb"] > 0.0 and stats["clipped_count"] == 0)
        new_vid: str | None = None
        if promoted:
            new_vid = version_id or f"v-{uuid4().hex[:12]}"
            with self._conn:
                cur_row = self._conn.execute("SELECT version_id FROM kernel_active_belief WHERE id = 1").fetchone()
                parent_vid = cur_row["version_id"] if cur_row is not None else None
                report = {**stats, "clip_max": clip_max, "delta": delta, "min_samples": min_samples}
                self._conn.execute(
                    """
                    INSERT INTO kernel_belief_versions (version_id, created_at, engine_json, parent_version_id, promotion_report_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (new_vid, _utc_now(), json.dumps(candidate.to_dict()), parent_vid, json.dumps(report)),
                )
                self._conn.execute(
                    "UPDATE kernel_active_belief SET version_id = ? WHERE id = 1",
                    (new_vid,),
                )
        return {
            "promoted": promoted,
            "version_id": new_vid,
            "stats": stats,
            "active_version": self.active_version()["version_id"],
        }

    def rollback(self) -> dict[str, Any]:
        with self._conn:
            row = self._conn.execute(
                """
                SELECT b.version_id, b.parent_version_id
                FROM kernel_active_belief a
                JOIN kernel_belief_versions b ON a.version_id = b.version_id
                WHERE a.id = 1
                """
            ).fetchone()
            if row is None or not row["parent_version_id"]:
                return {
                    "rolled_back": False,
                    "reason": "no_parent_version",
                    "active_version": row["version_id"] if row else None,
                }
            parent_vid = row["parent_version_id"]
            self._conn.execute(
                "UPDATE kernel_active_belief SET version_id = ? WHERE id = 1",
                (parent_vid,),
            )
            return {
                "rolled_back": True,
                "previous_version": row["version_id"],
                "active_version": parent_vid,
            }

    def check_drift_and_rollback(
        self,
        false_passes: int,
        sample_count: int,
        *,
        max_alpha: float = 0.05,
        delta: float = 0.05,
    ) -> dict[str, Any]:
        if sample_count <= 0:
            return {"drift_detected": False, "ucb": 1.0, "rolled_back": False}
        ucb = clopper_pearson_upper(float(false_passes), int(sample_count), delta=delta)
        emp_rate = float(false_passes) / float(sample_count)
        drift = emp_rate > max_alpha or (sample_count >= 20 and ucb > max_alpha * 1.5)
        if drift:
            rb = self.rollback()
            return {"drift_detected": True, "empirical_rate": emp_rate, "ucb": ucb, **rb}
        return {"drift_detected": False, "empirical_rate": emp_rate, "ucb": ucb, "rolled_back": False}
