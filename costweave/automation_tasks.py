"""Frozen synthetic tasks with independent, deterministic reference answers."""
import hashlib
import json
import random

TASKSET_VERSION = "costweave-auto-text-20260918-v1"
CALLS_PER_ROUND = 8
OUTPUT_TOKEN_LIMIT = 1024
INPUT_BYTE_LIMIT = 16384


def schema_for(value):
    if value is None:
        return {"type": "null"}
    if type(value) is int:
        return {"type": "integer"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array", "items": schema_for(value[0]) if value else {"type": "string"}}
    if isinstance(value, dict):
        return {"type": "object", "required": list(value), "additionalProperties": False,
                "properties": {k: schema_for(v) for k, v in value.items()}}
    raise TypeError("unsupported reference type")


def experiment_tasks():
    rng = random.Random(20260918)
    tasks = []
    def add(identity, category, prompt, expected):
        prompt += " Return exactly the requested JSON object, without Markdown or explanation."
        schema = schema_for(expected)
        # Both positive and absent-answer cases receive the SAME nullable schema;
        # a reference null must not reveal the answer through the prompt contract.
        if category in {"约束与边界", "依据提取与缺失信息"}:
            for key in expected:
                schema["properties"][key] = {"type": ["integer" if key == "cost" else "string", "null"]}
        tasks.append({"id": identity, "category": category, "prompt": prompt,
                      "expected": expected, "schema": schema,
                      "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
    for variant in range(2):
        a, b, c = rng.sample(range(17, 79), 3)
        word = ("CostWeave", "Automation")[variant]
        add(f"arithmetic_{variant}", "计算与格式",
            f"Compute ({a} * {b}) + {c}. Return keys answer (integer), checksum (string). "
            f"checksum is lowercase characters at 1-based odd positions of '{word}'.",
            {"answer": a*b+c, "checksum": word[::2].lower()})
        rows = [{"id": f"O{i}", "quantity": rng.randrange(1, 6),
                 "unit_price_cents": rng.randrange(100, 3000),
                 "status": "paid" if i % 3 and variant == 0 else "cancelled"}
                for i in range(8)]
        rows += [dict(rows[1]), {**rows[2], "status": "cancelled"}]
        latest = {row["id"]: row for row in rows}
        paid = [latest[key] for key in sorted(latest) if latest[key]["status"] == "paid"]
        add(f"aggregation_{variant}", "清洗与聚合",
            "For duplicate IDs use the LAST row. Keep only paid orders. Return ids sorted lexicographically, "
            "quantity=sum of quantities, revenue_cents=sum of quantity times unit_price_cents. "
            "No matches means [], 0, 0. Rows: " + json.dumps(rows),
            {"ids": [r["id"] for r in paid], "quantity": sum(r["quantity"] for r in paid),
             "revenue_cents": sum(r["quantity"]*r["unit_price_cents"] for r in paid)})
        candidates = [{"id": "A", "cost": 8, "quality": 91, "latency": 20},
                      {"id": "B", "cost": 4, "quality": 88, "latency": 15},
                      {"id": "C", "cost": 4, "quality": 93, "latency": 18},
                      {"id": "D", "cost": 4, "quality": 93, "latency": 18}]
        quality = (90, 99)[variant]
        valid = [r for r in candidates if r["quality"] >= quality and r["latency"] <= 20]
        chosen = min(valid, key=lambda r: (r["cost"], r["id"])) if valid else None
        add(f"constraints_{variant}", "约束与边界",
            f"Choose a candidate with quality >= {quality}, latency <= 20. Minimize cost; break ties by smallest ID. "
            "Return selected (ID or null), cost (integer or null). Candidates: " + json.dumps(candidates),
            {"selected": chosen["id"] if chosen else None, "cost": chosen["cost"] if chosen else None})
        facts = [{"source": f"D{i:02}", "item": f"part-{i:02}", "code": f"C{rng.randrange(1000,9999)}"}
                 for i in range(36)]
        target = "part-17" if variant == 0 else "part-99"
        match = next((r for r in facts if r["item"] == target), None)
        add(f"evidence_{variant}", "依据提取与缺失信息",
            f"Use ONLY these records. Find the code for {target}. Return code and source. "
            "If absent, return null for both; do not invent a value. Records: " + json.dumps(facts),
            {"code": match["code"] if match else None, "source": match["source"] if match else None})
    return tasks
