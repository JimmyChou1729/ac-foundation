"""Identify running section headers using both body and repeated margin evidence."""

import copy
import math
import re
import unicodedata


def _candidate(item):
    if not isinstance(item, dict) or item.get("type") != "text":
        return None
    text, box, page = item.get("text"), item.get("bbox"), item.get("page_idx")
    if not isinstance(text, str) or len(text) > 160 or type(page) is not int or page < 0:
        return None
    if (not isinstance(box, list) or len(box) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
        or not 0 <= box[0] < box[2] <= 1000
        or not 0 <= box[1] < box[3] <= 1000):
        return None
    text = unicodedata.normalize("NFKC", text).strip()
    match = re.match(r"^(\d+(?:\.\d+)+)\s*(.*)$", text)
    if not match or not any(c.isalpha() for c in match[2]):
        return None
    title = "".join(c for c in match[2].casefold() if c.isalnum())
    return (match[1], title), page, box


def classify_running_headers(items):
    """Retype high-confidence headers; never remove provider evidence or reorder items.

    A title must also exist below the margin. The margin occurrence must share
    position with repeated headers on multiple pages. Single-occurrence headers
    additionally require the real heading on their own page and a margin pattern
    established by at least two other section titles.
    """
    candidates = [(i, item, _candidate(item)) for i, item in enumerate(items)]
    candidates = [(i, item, c) for i, item, c in candidates if c is not None]
    body = {}
    for _, item, (key, page, box) in candidates:
        if box[1] >= 120 and type(item.get("text_level")) is int and 1 <= item["text_level"] <= 6:
            body.setdefault(key, set()).add(page)
    margin = [(i, c) for i, _, c in candidates
              if c[0] in body and c[2][3] <= 120 and c[2][3] - c[2][1] <= 25]
    repeated = []
    for i, (key, page, box) in margin:
        peers = [c for _, c in margin if c[0] == key
                 and abs(c[2][1] - box[1]) <= 10 and abs(c[2][3] - box[3]) <= 10]
        if len({c[1] for c in peers}) >= 2:
            repeated.append((i, (key, page, box)))
    selected = {i for i, _ in repeated}
    for i, (key, page, box) in margin:
        witnesses = [c for _, c in repeated if c[0] != key
                     and abs(c[2][1] - box[1]) <= 10 and abs(c[2][3] - box[3]) <= 10]
        if page in body[key] and len({c[0] for c in witnesses}) >= 2 and len({c[1] for c in witnesses}) >= 3:
            selected.add(i)
    result = copy.deepcopy(items)
    for i in selected:
        result[i]["type"] = "header"
        result[i]["page_furniture_reason"] = "repeated_top_margin_with_body_heading"
        result[i]["original_type"] = items[i]["type"]
    return result
