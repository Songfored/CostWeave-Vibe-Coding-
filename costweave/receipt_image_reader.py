"""Read the total from a photographed receipt using local, optional Pillow and Tesseract."""

import math
from collections import deque
from statistics import median


def _paper_crop(image):
    """Locate a light, low-saturation paper region on a darker or colored background."""
    from PIL import Image

    sample = image.convert("RGB")
    sample.thumbnail((96, 96), Image.Resampling.BOX)
    w, h = sample.size
    pixels = list(sample.getdata())
    light = [sum(rgb) // 3 for rgb in pixels]
    central = sorted(light[y * w + x] for y in range(h // 4, max(h // 4 + 1, 3 * h // 4))
                     for x in range(w // 4, max(w // 4 + 1, 3 * w // 4)))
    floor = max(40, central[len(central) // 4] - 33)
    mask = bytearray(level >= floor and max(rgb) - min(rgb) <= max(30, level * 0.28)
                     for level, rgb in zip(light, pixels))
    best = []
    for start in range(w * h):
        if not mask[start]:
            continue
        queue = deque([start])
        component = []
        mask[start] = 0
        while queue:
            offset = queue.pop()
            component.append(offset)
            x, y = offset % w, offset // w
            for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
                if 0 <= nx < w and 0 <= ny < h and mask[ny * w + nx]:
                    mask[ny * w + nx] = 0
                    queue.append(ny * w + nx)
        if len(component) > len(best):
            best = component
    if len(best) < 0.20 * len(mask):
        return image
    xs, ys = zip(*((p % w, p // w) for p in best))
    left, right = min(xs), max(xs) + 1
    top, bottom = min(ys), max(ys) + 1
    if right - left < w * 0.40 or bottom - top < h * 0.40:
        return image
    xpad, ypad = max(2, round(w * .035)), max(2, round(h * .035))
    return image.crop((max(0, (left - xpad) * image.width // w), max(0, (top - ypad) * image.height // h),
                       min(image.width, (right + xpad) * image.width // w),
                       min(image.height, (bottom + ypad) * image.height // h)))


def _normalize(image, target_width):
    from PIL import Image, ImageChops, ImageFilter, ImageOps

    paper = _paper_crop(ImageOps.exif_transpose(image)).convert("L")
    scale = min(target_width / paper.width, 2300 / paper.height, 2.5)
    size = (max(1, round(paper.width * scale)), max(1, round(paper.height * scale)))
    gray = paper.resize(size, Image.Resampling.LANCZOS)
    # A blurred low-resolution background follows shadows and folds more slowly than character strokes.
    coarse = gray.resize((max(1, size[0] // 10), max(1, size[1] // 10)), Image.Resampling.BOX)
    background = coarse.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2))
    background = background.resize(size, Image.Resampling.BILINEAR)
    flattened = ImageChops.subtract(gray, background, offset=248)
    return ImageOps.autocontrast(flattened, cutoff=(0.6, 1.0))


def _deskew(gray):
    from PIL import Image, ImageOps

    w, h = gray.size
    probe = gray.crop((w // 7, h // 5, max(w // 7 + 1, 6 * w // 7), max(h // 5 + 1, 9 * h // 10)))
    probe.thumbnail((320, 360), Image.Resampling.LANCZOS)
    ink = ImageOps.invert(probe).point(lambda value: 255 if value >= 49 else 0)

    def score(angle):
        rotated = ink.rotate(angle, resample=Image.Resampling.NEAREST, fillcolor=0)
        rows = list(rotated.resize((1, rotated.height), Image.Resampling.BOX).getdata())
        return sum(min(90, abs(a - b))**2 for a, b in zip(rows, rows[1:]))

    zero = score(0)
    angle, best = 0, zero
    for candidate in (-12, -9, -6, -3, 3, 6, 9, 12):
        value = score(candidate)
        if value > best:
            angle, best = candidate, value
    if best < zero * 1.16 or not angle:
        return gray
    angle = max((angle - 1, angle, angle + 1), key=score)
    return gray.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)


def _visual_lines(words):
    groups = []
    for word in sorted(words, key=lambda box: (box["top"] + box["bottom"], box["left"])):
        center = (word["top"] + word["bottom"]) / 2
        height = word["bottom"] - word["top"]
        best = None
        distance = math.inf
        for group in reversed(groups):
            delta = abs(center - group["center"])
            if delta < distance and delta <= .52 * max(height, group["height"]):
                best, distance = group, delta
        if best is None:
            groups.append({"center": center, "height": height, "words": [word]})
        else:
            best["words"].append(word)
            best["center"] = median((w["top"] + w["bottom"]) / 2 for w in best["words"])
            best["height"] = median(w["bottom"] - w["top"] for w in best["words"])
    return [sorted(group["words"], key=lambda w: w["left"]) for group in sorted(groups, key=lambda g: g["center"])]


def _ocr(gray, engine, layout):
    w, h = gray.size
    engine.SetPageSegMode(layout)
    engine.SetImage(gray)
    try:
        tsv = engine.GetTSVText(0)
    finally:
        engine.Clear()
        engine.ClearAdaptiveClassifier()
    words = []
    for line in tsv.splitlines():
        fields = line.split("\t", 11)
        if len(fields) != 12 or fields[0] != "5" or not fields[11].strip():
            continue
        x, y, width, height = map(int, fields[6:10])
        if not width or not height:
            continue
        words.append({"text": fields[11], "left": x / w, "top": y / h,
                      "right": (x + width) / w, "bottom": (y + height) / h})
    return _visual_lines(words)


def read_views(image, route, engine):
    from PIL import Image, ImageOps
    from tesserocr import PSM

    if route not in ("direct", "check", "original"):
        raise ValueError("route must be direct, check or original")
    gray = _deskew(_normalize(image, 840 if route == "direct" else 1120))
    if route == "direct":
        return [_ocr(gray, engine, PSM.SINGLE_BLOCK)]
    # A pass with the original lighting retains faint leading digits that shadow
    # normalization can erase. Treat these reads as views of the same receipt.
    plain = ImageOps.grayscale(ImageOps.exif_transpose(image))
    plain.thumbnail((950, 950), Image.Resampling.LANCZOS)
    layouts = (PSM.SPARSE_TEXT,) if route == "check" else (PSM.SINGLE_BLOCK, PSM.SPARSE_TEXT)
    views = [_ocr(gray, engine, layout) for layout in layouts]
    views.append(_ocr(ImageOps.autocontrast(plain), engine, PSM.SINGLE_BLOCK))
    return views


def read_total(image, route, engine):
    """Return integer hundredths of one rupiah, or None when this route abstains."""
    from .receipt_totals import select_total

    return select_total(read_views(image, route, engine), route)
