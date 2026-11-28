"""Measure two complete receipt readers on the same documents. No gold data enters either reader."""

import argparse
import hashlib
import io
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path

from costweave.receipt_image_reader import read_total
from scripts.measure_cord_verification import ENG_SHA256, HASHES, PRICES, REVISION, extract, file_digest, reference


PRIOR = (("validation.parquet", HASHES["calibration"]), ("test.parquet", HASHES["holdout"]))
CALIBRATION = (
    ("train-00000-of-00004-b4aaeceff1d90ecb.parquet", "da3994eee1bf9bd3c57f0d53a72c3a6812c8696c5ba26245987949ddf73483cc"),
    ("train-00001-of-00004-7dbbe248962764c5.parquet", "cce4def16a0d6a6c75f80be712f7494c56c318a8829b712f5c62650155c9e58e"),
)
HOLDOUT = (("train-00002-of-00004-688fe1305a55e5cc.parquet", "591e2db8fe8b1d364b054f46e8c375b7f00e72578914ba46a573b6858162cab2"),)
INDEPENDENT = (("train-00003-of-00004-2d0cd200555ed7fd.parquet", "1ffd9de8d6fbcee7630fd4cdfedff05b9b7fabc0fae4fc17557c5fe7cf178748"),)
READERS = {"old": extract, "new": read_total}


def rows(directory, spec, *, labels):
    import pyarrow.parquet as pq

    name, checksum = spec
    path = directory / name
    if file_digest(path) != checksum:
        raise ValueError(f"unexpected source file: {name}")
    columns = ["image", "ground_truth"] if labels else ["image"]
    for batch in pq.ParquetFile(path).iter_batches(batch_size=1, columns=columns):
        yield batch.to_pylist()[0]


def identities(raw, image):
    group = hashlib.sha256(raw).hexdigest()
    pixels = hashlib.sha256(f"RGB {image.width} {image.height}\n".encode())
    pixels.update(image.convert("RGB").tobytes())
    return group, pixels.hexdigest()


def measure(split, directory, output):
    from PIL import Image
    import tesserocr

    if file_digest(directory / "eng.traineddata") != ENG_SHA256:
        raise ValueError("unexpected OCR language data")
    if os.environ.get("OMP_THREAD_LIMIT") != "1":
        raise ValueError("set OMP_THREAD_LIMIT=1 to match the registered timing protocol")
    partitions = {"calibration": (PRIOR, CALIBRATION), "holdout": ((*PRIOR, *CALIBRATION), HOLDOUT),
                  "independent": ((*PRIOR, *CALIBRATION, *HOLDOUT), INDEPENDENT)}
    earlier, sources = partitions[split]
    seen_raw, seen_pixels = set(), set()
    for spec in earlier:
        for row in rows(directory, spec, labels=False):
            raw = row["image"]["bytes"]
            with Image.open(io.BytesIO(raw)) as image:
                group, pixels = identities(raw, image)
            seen_raw.add(group)
            seen_pixels.add(pixels)

    records = {version: [] for version in READERS}
    skipped = []
    with ExitStack() as stack:
        engines = {version: stack.enter_context(tesserocr.PyTessBaseAPI(path=str(directory.resolve()), lang="eng"))
                   for version in READERS}
        for spec in sources:
            for idx, row in enumerate(rows(directory, spec, labels=True)):
                cid = f"cord-{spec[0][6:11]}-{idx:03d}"
                raw = row["image"]["bytes"]
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
                    group, pixels = identities(raw, image)
                    duplicate = group in seen_raw or pixels in seen_pixels
                    seen_raw.add(group)
                    seen_pixels.add(pixels)
                    if duplicate:
                        skipped.append({"id": cid, "reason": "duplicate document"})
                        continue
                    common = {"id": cid, "group": group, "split": "calibration" if split == "calibration" else "holdout",
                              "stratum": "tall" if image.height >= 2 * image.width else "normal",
                              "budget_units": 4, "deadline_ms": 1200 if int(group[:2], 16) % 2 == 0 else 5000}
                    choices = {version: {} for version in READERS}
                    for n, (action, price) in enumerate(PRICES.items()):
                        order = ("old", "new") if (int(group[2:4], 16) + n) % 2 == 0 else ("new", "old")
                        for version in order:
                            tick = time.perf_counter_ns()
                            answer = READERS[version](image, action, engines[version])
                            ms = max(1, (time.perf_counter_ns() - tick + 999_999) // 1_000_000)
                            choices[version][action] = {"answer": answer, "cost_units": price, "elapsed_ms": ms}
                gold = reference(row["ground_truth"])
                if gold is None:
                    skipped.append({"id": cid, "reason": "no unambiguous single annotated total"})
                else:
                    for version in READERS:
                        records[version].append({**common, "truth": gold, "actions": choices[version]})
                if (idx + 1) % 25 == 0:
                    print(f"{spec[0][:11]}: {idx + 1} documents", flush=True)

    output.mkdir(parents=True, exist_ok=True)
    for version in READERS:
        metadata = {"reader": version, "revision": REVISION, "source_sha256": dict(sources), "excluded": skipped,
                    "ocr": tesserocr.tesseract_version().splitlines()[0], "eng_sha256": ENG_SHA256}
        document = {"metadata": metadata, "records": records[version]}
        path = output / f"{version}-{split}.json"
        path.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{split}: {len(records['new'])} paired documents, {len(skipped)} exclusions", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("split", choices=("calibration", "holdout", "independent"))
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    measure(args.split, args.source_dir, args.output_dir)
