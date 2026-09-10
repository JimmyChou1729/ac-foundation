"""Restore page-local text from MinerU 3.4.5 pipeline merge evidence."""

from __future__ import annotations

import copy
import math
import re

from .pdf_source import MAX_ENTRIES, PDFSourceBundleError


def _unavailable(detail):
    raise PDFSourceBundleError(
        "mineru_page_restore_unavailable",
        f"MinerU page restoration unavailable: {detail}.",
    )


def _marked(value):
    if isinstance(value, dict):
        return (
            value.get("cross_page") is True
            or value.get("lines_deleted") is True
            or any(
                _marked(child)
                for child in value.values()
                if isinstance(child, (dict, list))
            )
        )
    return isinstance(value, list) and any(_marked(child) for child in value)


def _box(value):
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(v) not in {int, float} or not math.isfinite(v) for v in value)
        or value[0] > value[2]
        or value[1] > value[3]
    ):
        _unavailable("missing or invalid original bounding box")
    return tuple(value)


def _line_keys(lines):
    if not isinstance(lines, list) or not lines:
        _unavailable("missing original text lines")
    result = []
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("spans"), list):
            _unavailable("invalid original text line")
        spans = []
        for span in line["spans"]:
            if (
                not isinstance(span, dict)
                or span.get("type") not in {"text", "inline_equation"}
                or not isinstance(span.get("content"), str)
            ):
                _unavailable("unsupported text span")
            spans.append((_box(span.get("bbox")), span["type"], span["content"]))
        if not spans:
            _unavailable("empty original text line")
        result.append((_box(line.get("bbox")), tuple(spans)))
    return tuple(result)


def _text_span(content):
    # Pipeline normalizes only full-width letters/digits, then escapes Markdown.
    content = "".join(
        chr(ord(c) - 0xFEE0)
        if any(
            lo <= ord(c) <= hi
            for lo, hi in ((0xFF10, 0xFF19), (0xFF21, 0xFF3A), (0xFF41, 0xFF5A))
        )
        else c
        for c in content
    )
    return re.sub(
        r"(\\*)([*_`~$])",
        lambda m: m[1] + ("\\" if len(m[1]) % 2 == 0 else "") + m[2],
        content,
    )


def _render(lines, *, cjk):
    parts = []
    for li, line in enumerate(lines):
        if li and line.get("is_list_start_line"):
            parts.append("  \n")
        for si, span in enumerate(line["spans"]):
            kind = span["type"]
            content = (
                _text_span(span["content"])
                if kind == "text"
                else f"${span['content']}$"
                if span["content"]
                else ""
            ).strip()
            if not content:
                continue
            last = si == len(line["spans"]) - 1
            suffix = " "
            if cjk and last and kind == "text":
                suffix = ""
            elif (
                not cjk
                and last
                and kind == "text"
                and re.search(r"[A-Za-z]+[-\u00ad\u2010\u2011\u2043]$", content)
            ):
                suffix = ""
                following = lines[li + 1]["spans"][0] if li + 1 < len(lines) else None
                if (
                    following
                    and following["type"] == "text"
                    and _text_span(following["content"])[:1].islower()
                ):
                    content = content[:-1]
            parts.append(content + suffix)
    text = "".join(parts).rstrip()
    return re.sub(r"^([ \t]{0,3})(#{1,6}|[+-])(?=[ \t])", r"\1\\\2", text)


def restore_mineru_page_items(items: list[dict], middle: dict) -> list[dict]:
    """Return a deep-copied, content-list-compatible list with page-local text.

    Only MinerU 3.4.5 ``pipeline`` output is supported. Unmerged items, item
    order, types, boxes, images, tables and display equations remain unchanged.
    Text and index merge chains are split using exact original ``preproc_blocks`` line
    geometry and span content, including inline equations. Deleted placeholders
    are filled once; page-end hyphens are retained on their original page.

    This helper does not run OCR, infer pages from prose, or modify either input.
    Missing, ambiguous or unsupported merge evidence raises
    ``PDFSourceBundleError`` with code ``mineru_page_restore_unavailable``;
    malformed top-level results and unsupported versions use the corresponding
    ``mineru_invalid_result`` and ``mineru_unsupported_version`` codes. A caller
    must not fall back to claiming the original merged text is page-local.
    Cross-page table reconstruction is outside this text restoration contract.
    """
    if (
        not isinstance(items, list)
        or len(items) > MAX_ENTRIES
        or not isinstance(middle, dict)
    ):
        raise PDFSourceBundleError(
            "mineru_invalid_result", "MinerU result has an invalid structure."
        )
    if middle.get("_backend") != "pipeline" or middle.get("_version_name") != "3.4.5":
        raise PDFSourceBundleError(
            "mineru_unsupported_version",
            "This adapter supports MinerU 3.4.5 pipeline output.",
        )
    pages = middle.get("pdf_info")
    if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
        raise PDFSourceBundleError(
            "mineru_invalid_result", "MinerU page inventory is invalid."
        )
    result = copy.deepcopy(items)
    if not any(_marked(page.get("para_blocks", [])) for page in pages):
        return result

    records = []
    seen_pages = set()
    for page in pages:
        index = page.get("page_idx")
        if type(index) is not int or index < 0 or index in seen_pages:
            _unavailable("invalid page identity")
        seen_pages.add(index)
        blocks = page.get("para_blocks")
        if not isinstance(blocks, list) or any(
            not isinstance(block, dict) for block in blocks
        ):
            _unavailable("invalid paragraph inventory")
        for order, block in enumerate(blocks):
            if _marked(block) and block.get("type") not in {"text", "index"}:
                _unavailable("unsupported merged block type")
            records.append({"page": page, "order": (index, order), "block": block})
    records.sort(key=lambda rec: rec["order"])
    chains = []
    current = None
    for rec in records:
        block = rec["block"]
        if block.get("type") not in {"text", "index"}:
            continue
        if block.get("lines_deleted") is True:
            if (block.get("lines") or current is None
                or current[0]["block"]["type"] != block["type"]):
                _unavailable("deleted paragraph has no matching merged owner")
            current.append(rec)
        elif block.get("lines"):
            current = [rec]
            chains.append(current)
    chains = [chain for chain in chains if len(chain) > 1 or _marked(chain[0]["block"])]

    def original(rec):
        if "original" in rec:
            return rec["original"]
        block, page = rec["block"], rec["page"]
        preproc = page.get("preproc_blocks")
        if not isinstance(preproc, list):
            _unavailable("missing preproc_blocks")
        box = _box(block.get("bbox"))
        candidates = [
            candidate
            for candidate in preproc
            if isinstance(candidate, dict)
            and candidate.get("type") == block.get("type")
            and candidate.get("bbox") == list(box)
            and ("index" not in block or candidate.get("index") == block["index"])
        ]
        if len(candidates) != 1:
            _unavailable("original paragraph mapping is missing or ambiguous")
        lines = candidates[0].get("lines")
        if _marked(lines):
            _unavailable("preprocessed lines already contain merge markers")
        rec["keys"] = _line_keys(lines)
        rec["original"] = lines
        return lines

    for chain in chains:
        merged_lines = chain[0]["block"]["lines"]
        keys = _line_keys(merged_lines)
        offset = 0
        for rec in chain:
            original(rec)
            count = len(rec["keys"])
            if keys[offset : offset + count] != rec["keys"]:
                _unavailable("moved lines disagree with original reading order")
            expected_cross = rec["order"][0] != chain[0]["order"][0]
            if any(
                bool(span.get("cross_page")) != expected_cross
                for line in merged_lines[offset : offset + count]
                for span in line["spans"]
            ):
                _unavailable("cross-page markers disagree with original pages")
            offset += count
        if offset != len(keys):
            _unavailable("merged paragraph contains lines with no original owner")

    by_item_key = {}
    for index, item in enumerate(items):
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and type(item.get("page_idx")) is int
            and item.get("bbox") is not None
        ):
            key = (item["page_idx"], _box(item["bbox"]))
            by_item_key.setdefault(key, []).append(index)

    def item_index(rec):
        page, block = rec["page"], rec["block"]
        size = page.get("page_size")
        if (
            not isinstance(size, list)
            or len(size) != 2
            or any(
                type(v) not in {int, float} or not math.isfinite(v) or v <= 0
                for v in size
            )
        ):
            _unavailable("missing or invalid page dimensions")
        box = [
            int(v * 1000 / size[i % 2]) for i, v in enumerate(_box(block.get("bbox")))
        ]
        matches = by_item_key.get((page["page_idx"], tuple(box)), [])
        if len(matches) != 1:
            _unavailable("content-list paragraph mapping is missing or ambiguous")
        return matches[0]

    for chain in chains:
        indices = [item_index(rec) for rec in chain]
        choices = set()
        for cjk in (False, True):
            texts = tuple(_render(original(rec), cjk=cjk) for rec in chain)
            merged = _render(chain[0]["block"]["lines"], cjk=cjk)
            incoming = tuple(items[i].get("text") for i in indices)
            if incoming == texts or (
                incoming[0] == merged and all(text == "" for text in incoming[1:])
            ):
                choices.add(texts)
        if len(choices) != 1:
            _unavailable("content-list text disagrees with original merge evidence")
        for index, text in zip(indices, choices.pop()):
            result[index]["text"] = text
    return result


__all__ = ["restore_mineru_page_items"]
