"""Recover narrowly identified title furniture without inventing OCR text."""

from collections import Counter
import copy
import math


def _box(value):
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(type(n) in (int, float) and math.isfinite(n) for n in value)
        and 0 <= value[0] < value[2]
        and 0 <= value[1] < value[3]
    )


def _text(block):
    lines = block.get("lines", [])
    if not isinstance(lines, list):
        return ""
    result = []
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("spans"), list):
            return ""
        for span in line["spans"]:
            if (
                not isinstance(span, dict)
                or span.get("type") != "text"
                or not isinstance(span.get("content"), str)
            ):
                return ""
            result.append(span["content"])
    return " ".join(result).strip()


def restore_title_labels(items, middle):
    """Keep exact, unique label text above a small title-side image.

    This does not recognize the image, combine headings, or restore general
    running headers. Normalized content-list output retains original evidence.
    """
    pages = middle.get("pdf_info", [])
    headers = [
        (p, b, _text(b))
        for p in pages
        if isinstance(p, dict)
        for b in (
            p.get("discarded_blocks")
            if isinstance(p.get("discarded_blocks"), list)
            else []
        )
        if isinstance(b, dict) and b.get("type") == "header"
    ]
    counts = Counter(text.casefold() for _, _, text in headers)
    result = copy.deepcopy(items)
    for page, block, text in headers:
        size, box, index = (
            page.get("page_size"),
            block.get("bbox"),
            page.get("page_idx"),
        )
        if (
            not text
            or len(text) > 60
            or not any(c.isalpha() for c in text)
            or counts[text.casefold()] != 1
            or not _box(box)
            or not isinstance(size, list)
            or len(size) != 2
            or any(
                type(n) not in (int, float) or not math.isfinite(n) or n <= 0
                for n in size
            )
            or type(index) is not int
        ):
            continue
        width, height = size
        if box[3] > height * 0.25:
            continue
        normalized = [
            box[0] / width * 1000,
            box[1] / height * 1000,
            box[2] / width * 1000,
            box[3] / height * 1000,
        ]
        if any(n > 1000 for n in normalized):
            continue
        same = [v for v in result if isinstance(v, dict) and v.get("page_idx") == index]
        if any(
            v.get("type") == "text"
            and isinstance(v.get("text"), str)
            and v["text"].strip() == text
            for v in same
        ):
            continue
        titles = [
            v
            for v in same
            if v.get("type") == "text"
            and v.get("text_level") == 1
            and _box(v.get("bbox"))
        ]
        images = [v for v in same if v.get("type") == "image" and _box(v.get("bbox"))]
        matches = []
        for image in images:
            b = image["bbox"]
            if not (
                b[2] - b[0] <= 120
                and b[3] - b[1] <= 150
                and 0 <= b[1] - normalized[3] <= 50
                and max(b[0], normalized[0]) < min(b[2], normalized[2])
            ):
                continue
            if any(
                t["bbox"][0] >= b[2]
                and max(t["bbox"][1], b[1]) < min(t["bbox"][3], b[3])
                for t in titles
            ):
                matches.append(image)
        if len(matches) != 1:
            continue
        position = next(i for i, item in enumerate(result) if item is matches[0])
        result.insert(
            position,
            {"type": "text", "text": text, "bbox": normalized, "page_idx": index},
        )
    return result
