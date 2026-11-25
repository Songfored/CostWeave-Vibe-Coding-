"""Read an Indonesian receipt total from OCR word boxes, in hundredths of a rupiah.

A view is a list of visual rows. Each word has text and normalized box edges.
Multiple OCR passes on the same scan can corroborate a reading, but provide
no independent evidence about whether the printed receipt itself is correct.
"""

import re
from collections import Counter, defaultdict
from statistics import median


# Consume the whole token before validating its group sizes. Taking just the
# first valid substring of "22,0009" would silently change its magnitude.
_MONEY = re.compile(r"(?<![a-z\d.,:/])(?:rp\.?\s*)?([0-9o]+(?:[.,][0-9o]+)*)(?![a-z\d.:/%]|,\d)", re.I)
_TOTAL = re.compile(r"\b(?:[tf][o0][tli][a4][li1]|jumlah(?:\s+bayar)?|tagihan|amount\s+(?:due|payable)|grand\s+tot\w*)\b")
_SUB = re.compile(r"\bsub\s*[-.:]?\s*(?:t[o0]t[a4][li1]|ttl)\b")
_CASH = re.compile(r"\b(?:cash|tunai|bayar)\b")
_CHANGE = re.compile(r"\b(?:change[ds]?|[cg]hange|kembal[il1]?|kembalian)\b")
_CARD = re.compile(r"\b(?:kartu|debit|credit|card|edc|visa|mastercard|go[\s-]*pay|ovo|qris|non[\s-]*tunai)\b")
_ADJUST = re.compile(r"\b(?:tax|pajak|ppn|pb[1il]|service|s[uv]c\s*(?:chg|charge)|diskon|disc(?:ount)?)\b")
_DISCOUNT = re.compile(r"\b(?:diskon|disc(?:ount)?)\b")
_NON_AMOUNT = re.compile(r"\b(?:qty|quantity|total\s*(?:item|poin)|item\s*count|points?|telp?|phone|npwp|invoice\s*(?:id|no)|ref\s*no)\b")
_FOOTER = re.compile(r"\b(?:terima\s*kasih|thank\s+you|www|instagram|facebook|nomor|member\s*id)\b")


def _amounts(text):
    text = re.sub(r"(?<=[0-9])\s*([.,])\s*(?=[0-9o])", r"\1", text, flags=re.I)
    # OCR sometimes emits the punctuation as a blank in '60 000'. Limit
    # this repair to groups ending in zeros, rather than joining quantities.
    text = re.sub(r"(?<![\d.,])(\d{1,3})\s+(000)(?![\d.,])", r"\1,\2", text)
    values = []
    for match in _MONEY.finditer(text):
        token = match[1].lower()
        if not any(c.isdigit() for c in token):
            continue
        pieces = re.split(r"[.,]", token.replace("o", "0"))
        decimals = ""
        if len(pieces) > 1 and len(pieces[-1]) in (1, 2):
            decimals = pieces.pop()
        if any(len(piece) != 3 for piece in pieces[1:]) or len("".join(pieces)) > 10:
            continue
        cents = int("".join(pieces)) * 100 + int(decimals.ljust(2, "0"))
        values.append(cents)
    return values


def _row_amount(text):
    values = _amounts(text)
    plausible = [v for v in values if v >= 10000]
    return (plausible or values or [None])[-1]


def _role(text):
    if _NON_AMOUNT.search(text) or _FOOTER.search(text):
        return "ignore"
    if _SUB.search(text):
        return "subtotal"
    if _CHANGE.search(text):
        return "change"
    if _ADJUST.search(text) and not re.search(r"\bgrand\s+total", text):
        return "discount" if _DISCOUNT.search(text) else "tax"
    if _TOTAL.search(text):
        return "total"
    if _CARD.search(text):
        return "card"
    if _CASH.search(text):
        return "cash"
    return "unlabelled"


def _observations(view):
    result = []
    for words in view:
        if not words:
            continue
        ordered = sorted(words, key=lambda w: w["left"])
        text = " ".join(w["text"] for w in ordered).lower()
        right_edges = [w["right"] for w in ordered if any(c.isdigit() for c in w["text"])]
        edge = max(right_edges, default=0)
        center = median([(w["top"] + w["bottom"]) / 2 for w in ordered])
        # A single printed row may say "Pay cash 50,000 Change 8,000".
        change = _CHANGE.search(text)
        if change and _CASH.search(text[:change.start()]):
            result.append(("cash", _row_amount(text[:change.start()]), edge, center))
            text = text[change.start():]
        result.append((_role(text), _row_amount(text), edge, center))
    # Associate a label on its own line with the next unlabelled amount when adjacent.
    for i in range(1, len(result)):
        prior, empty, _, prior_y = result[i - 1]
        kind, value, edge, y = result[i]
        if (empty is None and prior in {"total", "subtotal", "cash", "card", "change", "tax", "discount"}
                and kind == "unlabelled" and value is not None and 0 <= y - prior_y <= 0.07):
            result[i] = prior, value, edge, y
    return result


def _view_scores(view):
    rows = _observations(view)
    support = defaultdict(dict)

    def add(value, source, strength):
        if value is not None and value > 0:
            evidence = support[value]
            evidence[source] = max(evidence.get(source, 0), strength)

    monetary = [(kind, value, edge) for kind, value, edge, _ in rows
                if kind != "ignore" and value is not None and value >= 10000 and edge >= 0.42]
    counts = Counter(value for _, value, _ in monetary)
    items = []
    subtotal = cash = change = None
    adjustments = []
    saw_summary = False
    last_total = next((value for kind, value, _, _ in reversed(rows) if kind == "total" and value is not None), None)
    has_discount = any(kind == "discount" for kind, _, _, _ in rows)
    for kind, value, edge, _ in rows:
        if kind in ("subtotal", "total", "cash", "card", "change"):
            saw_summary = True
        if value is None or kind == "ignore":
            continue
        if kind == "total":
            if value >= 10000:
                strength = 9 if value == last_total else 6
                # A supposed total losing most of the printed subtotal is often a clipped
                # leading digit. Keep it as a weak candidate when no discount explains it.
                if subtotal is not None and value < subtotal / 2 and not has_discount:
                    strength = 3
                add(value, "label", strength)
        elif kind == "card" and value >= 10000:
            add(value, "card", 7)
        elif kind == "cash":
            cash = value
        elif kind == "change":
            change = value
        elif kind == "subtotal" and value >= 10000:
            subtotal = value
            adjustments = []
            add(value, "subtotal", 4)
        elif kind in ("tax", "discount"):
            if subtotal is not None:
                adjustments.append(value if kind == "tax" else -value)
        elif not saw_summary and edge >= 0.42 and value >= 10000:
            items.append(value)

    if subtotal is not None and adjustments:
        add(subtotal + sum(adjustments), "adjusted_subtotal", 8.5)
    if subtotal is not None:
        # The final total and the payment often repeat in consecutive right-aligned rows,
        # even when OCR drops both labels. Do not use repeats from the item list.
        after_subtotal = False
        previous = None
        for kind, value, edge, _ in rows:
            if kind == "subtotal":
                after_subtotal = True
                previous = None
            elif after_subtotal and value is not None:
                if kind in ("unlabelled", "total", "card", "cash") and value >= 10000 and edge >= 0.55:
                    if previous is not None and previous[0] == value and abs(previous[1] - edge) <= 0.12:
                        add(value, "adjacent_payment", 7)
                    previous = (value, edge)
                else:
                    previous = None
    if cash is not None and change is not None and cash >= change:
        add(cash - change, "cash_less_change", 8)
    elif cash is not None and counts[cash] >= 2:
        add(cash, "repeated_tender", 5)
    item_sum = sum(items)
    if item_sum and (len(items) > 1 or counts[item_sum] >= 2):
        add(item_sum, "items", 4 if counts[item_sum] or item_sum in support else 1.5)
    # Unlabelled originals can still exhibit an exact item sum and cash-change identity.
    vals = [v for _, v, _ in monetary]
    if len(vals) >= 3 and vals[-2] - vals[-1] > 0:
        difference = vals[-2] - vals[-1]
        if difference in vals[:-2] or difference == sum(vals[:-2]):
            add(difference, "printed_difference", 7)
    for i, (kind, value, edge) in enumerate(monetary):
        if kind == "unlabelled":
            add(value, "layout", 1 + i / len(monetary) + min(edge, 1))
        if counts[value] > 1 and kind not in ("change", "tax", "discount"):
            add(value, "repeated", 3 if counts[value] == 2 else 4)
    # The strongest financial relation dominates. Independently expressed relations
    # or repeated readings on the same paper give bounded additional support.
    scores = {}
    for value, sources in support.items():
        ranked = sorted(sources.values(), reverse=True)
        scores[value] = ranked[0] + sum(min(s, 5) * 0.36 for s in ranked[1:3])
    return scores


def select_total(views, route):
    """Choose one total, or abstain. All routes return hundredths of a rupiah."""
    if route not in {"direct", "check", "original"}:
        raise ValueError(f"unknown receipt route: {route}")
    readings = [_view_scores(view) for view in views]
    values = set().union(*(scores.keys() for scores in readings))
    if not values:
        return None
    combined = []
    for value in values:
        strengths = sorted((scores.get(value, 0) for scores in readings), reverse=True)
        count = sum(score >= 4 for score in strengths)
        corroboration = min(3, strengths[1] * 0.40) if len(strengths) > 1 else 0
        combined.append((strengths[0] + corroboration, count, value))
    combined.sort(reverse=True)
    score, count, value = combined[0]
    margin = score - combined[1][0] if len(combined) > 1 else score
    if route == "direct":
        return value if score >= 2 and margin > 0 else None
    if route == "check":
        return value if score >= 5 and margin >= 0.35 else None
    return value if (count >= 2 or score >= 10.5) and score >= 7 and margin >= 0.8 else None
