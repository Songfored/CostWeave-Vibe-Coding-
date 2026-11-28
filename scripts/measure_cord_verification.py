"""Build blinded per-route measurements for CORD. Optional dependencies are listed in the benchmark guide."""

import argparse
import hashlib
import io
import json
import math
import re
import time
from decimal import Decimal
from pathlib import Path


REVISION = "7f0115a4b758a71d6473b8d085751692da2fef98"
HASHES = {"calibration": "0d0f6dac11fdcc549de2746aa9f53136a3bc22a2a1aff2b0b847f7622ad60c15",
          "holdout": "51c65f1788faff392abe2a0b55b023eb23e9be551c509138eaa3a832514224e7"}
ENG_SHA256 = "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
PRICES = {"direct": 1, "check": 2, "original": 4}
TOKEN = re.compile(r"(?<![\w.,:/-])(?:rp\.?\s*)?(\d+(?:[.,]\d+)*)(?![\w.,:/-])", re.I)
TOTAL = re.compile(r"\b(total|jumlah|tagihan|amount\s+due|amount\s+paid)\b", re.I)
EXCLUDE = re.compile(r"sub[\s-]*total|total\s*(item|qty|quantity|discount|diskon|tax|pajak|saving)", re.I)
CASH = re.compile(r"\b(tunai|cash)\b", re.I)
CHANGE = re.compile(r"\b(change|kembali|kembalian)\b", re.I)


def amount(text):
    """Normalize common IDR print conventions into integer hundredths."""
    if not isinstance(text, str):
        return None
    clean = re.sub(r"^(?:rp\.?|idr)\s*", "", text.strip(), flags=re.I).strip()
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", clean):
        return None
    parts = re.split(r"[.,]", clean)
    decimals = ""
    if len(parts) > 1 and len(parts[-1]) in (1, 2):
        decimals = parts.pop()
    if any(len(part) != 3 for part in parts[1:]):
        return None
    return int(Decimal("".join(parts) + "." + (decimals or "0")) * 100)


def read_candidates(text):
    totals, other, cash, change = [], [], [], []
    for line in text.splitlines():
        values = [amount(match.group(1)) for match in TOKEN.finditer(line)]
        values = [v for v in values if v is not None]
        if not values:
            continue
        value = values[-1]
        if TOTAL.search(line) and not EXCLUDE.search(line):
            totals.append(value)
        elif CHANGE.search(line):
            change.append(value)
        elif CASH.search(line):
            cash.append(value)
        elif value >= 10000:
            other.append(value)
    arithmetic = cash[-1] - change[-1] if cash and change and cash[-1] >= change[-1] else None
    return totals, other, arithmetic


def extract(image, route, engine):
    from PIL import Image, ImageOps
    from tesserocr import PSM

    def read(size, layout):
        gray = ImageOps.grayscale(image)
        gray.thumbnail((size, size), Image.Resampling.LANCZOS)
        engine.SetPageSegMode(layout)
        engine.SetImage(ImageOps.autocontrast(gray))
        try:
            return read_candidates(engine.GetUTF8Text())
        finally:
            engine.Clear()
            engine.ClearAdaptiveClassifier()

    if route == "direct":
        totals, other, _ = read(760, PSM.SINGLE_BLOCK)
        return (totals or other or [None])[-1]
    if route == "check":
        totals, _, arithmetic = read(1450, PSM.AUTO)
        if arithmetic is not None and (not totals or arithmetic in totals):
            return arithmetic
        return totals[-1] if totals else None
    first, _, cash_a = read(1750, PSM.SINGLE_BLOCK)
    second, _, cash_b = read(1750, PSM.AUTO)
    left = set(first) | ({cash_a} if cash_a is not None else set())
    right = set(second) | ({cash_b} if cash_b is not None else set())
    consensus = left & right
    return consensus.pop() if len(consensus) == 1 else None


def reference(raw):
    tree = json.loads(raw)["gt_parse"]
    total = tree.get("total")
    return amount(total.get("total_price")) if isinstance(total, dict) else None


def file_digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def build(split, source, tessdata):
    import pyarrow.parquet as pq
    import tesserocr
    from PIL import Image

    if file_digest(source) != HASHES[split] or file_digest(tessdata / "eng.traineddata") != ENG_SHA256:
        raise ValueError("Parquet or OCR language data differs from the pinned release")
    records, skipped = [], []
    with tesserocr.PyTessBaseAPI(path=str(tessdata.resolve()), lang="eng") as engine:
        batches = pq.ParquetFile(source).iter_batches(batch_size=1, columns=["image", "ground_truth"])
        for index, batch in enumerate(batches):
            row = batch.to_pylist()[0]
            case_id = f"cord-{split}-{index:03d}"
            raw = row["image"]["bytes"]
            group = hashlib.sha256(raw).hexdigest()
            # Measure each route from pixels only. Gold fields and annotation coordinates are never passed to OCR.
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                stratum = "tall" if image.height >= 2 * image.width else "normal"
                actions = {}
                for name, price in PRICES.items():
                    start = time.perf_counter_ns()
                    answer = extract(image, name, engine)
                    elapsed_ms = max(1, math.ceil((time.perf_counter_ns() - start) / 1_000_000))
                    actions[name] = dict(answer=answer, cost_units=price, elapsed_ms=elapsed_ms)
            gold = reference(row["ground_truth"])
            if gold is None:
                skipped.append(dict(id=case_id, reason="no unambiguous single annotated total"))
            else:
                records.append(dict(id=case_id, group=group, split=split, stratum=stratum, actions=actions,
                                    truth=gold, budget_units=4, deadline_ms=1200 if int(group[:2], 16) % 2 == 0 else 5000))
            if (index + 1) % 20 == 0:
                print(f"{split}: processed {index + 1} images", flush=True)
    return {"metadata": {"source": "naver-clova-ix/cord-v2", "revision": REVISION, "source_sha256": HASHES[split],
                         "ocr": tesserocr.tesseract_version().splitlines()[0], "eng_sha256": ENG_SHA256,
                         "elapsed_ms": "measured warm single-process OCR and extraction, excluding shared decode",
                         "cost_units": "illustrative quotes, no provider charges", "excluded": skipped},
            "records": records}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("split", choices=tuple(HASHES))
    parser.add_argument("parquet", type=Path)
    parser.add_argument("tessdata", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = build(args.split, args.parquet, args.tessdata)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Retained {len(result['records'])}; excluded {len(result['metadata']['excluded'])}.")
