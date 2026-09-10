"""Offline adapter for MinerU pipeline results; no model or server dependency."""

from __future__ import annotations

import hashlib
import html
import math
import re
import shutil
import tempfile
from pathlib import Path

from ac_jobs import atomic_write_bytes
from bs4 import BeautifulSoup, NavigableString

from ._file_lock import exclusive_file_lock
from .parse.parser import PdftotextExtractor
from .mineru_pages import restore_mineru_page_items
from .pdf_source import (
    MAX_ENTRIES,
    MAX_JSON_BYTES,
    MAX_PAGES,
    MAX_PDF_BYTES,
    MAX_RESOURCE_BYTES,
    MAX_TOTAL_RESOURCE_BYTES,
    PDF_SOURCE_BUNDLE_SCHEMA,
    PDFSourceBundleError,
    file_record,
    page_coverage_status,
    json_bytes,
    read_bounded,
    read_json,
    safe_relative,
    verify_pdf_source_bundle,
)


_PAGE_FURNITURE = {"page_number", "header", "footer"}
_TEXT_KINDS = {"text", "page_footnote", "aside_text", "ref_text"}


def _text(value):
    if not isinstance(value, str) or any(
        ord(c) < 32 and c not in "\n\r\t" for c in value
    ):
        raise PDFSourceBundleError(
            "mineru_invalid_text", "MinerU text must be valid printable text."
        )
    return value


def _rich(value):
    text = _text(value)
    chunks = re.split(r"(?<!\\)(\$(?!\$)(?:\\.|[^$])+?(?<!\\)\$)", text)
    return "".join(
        '<math alttext="' + html.escape(c[1:-1], quote=True) + '"></math>'
        if c.startswith("$") and c.endswith("$") and len(c) > 2
        else html.escape(_decode_text_escapes(c))
        for c in chunks
    )


def _decode_text_escapes(text):
    # MinerU escapes these Markdown markers in text spans, not in math or code.
    text = re.sub(r"\\([*_`~$])", r"\1", text)
    return re.sub(r"^([ \t]{0,3})\\(#{1,6}|[+-])(?=[ \t])", r"\1\2", text)


def _table_sibling_html(node):
    # Called only after table sanitization and inline-math conversion.
    if getattr(node, "name", None) == "math":
        return str(node)
    if isinstance(node, NavigableString):
        return html.escape(str(node))
    return " ".join(_table_sibling_html(child) for child in node.children).strip()


def _strings(item, key):
    values = item.get(key, [])
    if not isinstance(values, list):
        raise PDFSourceBundleError(
            "mineru_invalid_text", "MinerU captions and notes must be text arrays."
        )
    return [_text(v) for v in values]


def _page_index(value, count):
    if type(value) is not int or not 0 <= value < count:
        raise PDFSourceBundleError(
            "mineru_invalid_page", "MinerU page index is outside the original PDF."
        )
    return value


def _expected_entries(page):
    body, discarded = page.get("para_blocks"), page.get("discarded_blocks")
    if not isinstance(body, list) or not isinstance(discarded, list):
        return None
    count = 0
    reference_counted = False
    for block in [*body, *discarded]:
        if not isinstance(block, dict):
            return None
        kind = block.get("type")
        if kind != "ref_text":
            reference_counted = False
        if block.get("type") in _PAGE_FURNITURE:
            continue
        readable = block.get("type") in {"image", "chart", "table", "code"}
        stack = [block]
        while stack and not readable:
            value = stack.pop()
            if isinstance(value, dict):
                readable = any(
                    isinstance(value.get(k), str) and value[k].strip()
                    for k in ("content", "text", "html", "image_path")
                )
                stack.extend(
                    value[k]
                    for k in ("lines", "spans", "blocks", "list_items")
                    if k in value
                )
            elif isinstance(value, list):
                stack.extend(value)
        # MinerU coalesces adjacent reference blocks into one content-list item.
        if readable:
            if kind != "ref_text" or not reference_counted:
                count += 1
            if kind == "ref_text":
                reference_counted = True
    return count


class _Converter:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.resources = {}
        self.payloads = {}
        self.by_reference = {}
        self.resource_bytes = 0
        self.warnings = [
            "OCR extraction has not been proofread against the original PDF."
        ]

    def image(self, item, entry, *, rendered=True):
        raw = item.get("img_path")
        if raw is None or raw == "":
            return ""
        relative = safe_relative(raw)
        if relative in self.by_reference:
            target = self.by_reference[relative]
            if target is None:
                self.warn(entry, "image is unavailable")
                return ""
            self.resources[target]["rendered"] |= rendered
            return f'<img src="{target}" alt="">'
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise PDFSourceBundleError(
                "pdf_source_unsafe_path", "MinerU image escapes the result directory."
            )
        try:
            payload = read_bounded(path, MAX_RESOURCE_BYTES)
        except FileNotFoundError:
            self.warn(entry, "image is missing")
            self.by_reference[relative] = None
            return ""
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix, media = "png", "image/png"
        elif payload.startswith(b"\xff\xd8\xff"):
            suffix, media = "jpg", "image/jpeg"
        elif payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            suffix, media = "webp", "image/webp"
        else:
            self.warn(entry, "image format is unsupported")
            self.by_reference[relative] = None
            return ""
        target = f"assets/{hashlib.sha256(payload).hexdigest()}.{suffix}"
        if target not in self.resources:
            if self.resource_bytes + len(payload) > MAX_TOTAL_RESOURCE_BYTES:
                raise PDFSourceBundleError(
                    "pdf_source_too_large",
                    "MinerU resources exceed the total byte limit.",
                )
            self.payloads[target] = payload
            self.resource_bytes += len(payload)
            self.resources[target] = {
                **file_record(target, payload),
                "media_type": media,
                "rendered": rendered,
                "original_paths": [],
            }
        if relative not in self.resources[target]["original_paths"]:
            self.resources[target]["original_paths"].append(relative)
        self.resources[target]["rendered"] |= rendered
        self.by_reference[relative] = target
        return f'<img src="{target}" alt="">'

    def warn(self, entry, detail):
        entry["status"] = "unavailable"
        self.warnings.append(
            f"PDF page {entry['page_number']}, item {entry['ordinal']}: {detail}."
        )

    @staticmethod
    def node(entry, tag, body):
        sid = f"pdf-p{entry['page_number']}-b{entry['ordinal']}-n{len(entry['source_ids'])}"
        entry["source_ids"].append(sid)
        return f'<{tag} id="{sid}">{body}</{tag}>'

    def table(self, item, entry):
        body = _text(item.get("table_body", ""))
        soup = BeautifulSoup(body, "html.parser")
        table = soup.find("table")
        captions = _strings(item, "table_caption")
        if table is None or not table.find("tr"):
            return self.table_fallback(item, entry, soup, captions)
        allowed = {
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "td",
            "th",
            "caption",
            "p",
            "br",
            "b",
            "strong",
            "i",
            "em",
            "sub",
            "sup",
            "span",
            "code",
        }
        sanitized = False
        for tag in list(soup.find_all(True)):
            if tag.parent is None:
                continue
            if tag.name in {
                "script",
                "style",
                "iframe",
                "object",
                "embed",
                "img",
                "svg",
                "math",
            }:
                tag.decompose()
                sanitized = True
                continue
            if tag.name not in allowed:
                tag.unwrap()
                sanitized = True
                continue
            attrs = {}
            for key in ("rowspan", "colspan"):
                if tag.name in {"td", "th"} and key in tag.attrs:
                    value = str(tag[key])
                    if value.isdecimal() and 1 <= int(value) <= 1000:
                        attrs[key] = value
                    else:
                        sanitized = True
            if set(tag.attrs) - {"rowspan", "colspan"}:
                sanitized = True
            tag.attrs = attrs
        for string in list(soup.find_all(string=True)):
            if isinstance(string, NavigableString) and "$" in string:
                fragment = BeautifulSoup(_rich(str(string)), "html.parser")
                string.replace_with(*list(fragment.contents))
        if sanitized:
            self.warn(entry, "unsupported table markup was removed")
        empty_grids = []
        for grid in soup.find_all("table"):
            if not any(
                cell.get_text(strip=True)
                or any(m.get("alttext", "").strip() for m in cell.find_all("math"))
                for cell in grid.find_all(["td", "th"])
            ):
                empty_grids.append(grid)
        if empty_grids:
            if len(empty_grids) == len(soup.find_all("table")):
                return self.table_fallback(item, entry, soup, captions)
            self.warn(entry, "empty table grids were unavailable")
            for grid in empty_grids:
                grid.decompose()
        # Keep every top-level table and any sibling text, not just the first grid.
        parts = []
        for child in list(soup.contents):
            if getattr(child, "name", None) == "table":
                sid = f"pdf-p{entry['page_number']}-b{entry['ordinal']}-n{len(entry['source_ids'])}"
                entry["source_ids"].append(sid)
                child["id"] = sid
                if captions:
                    cap = soup.new_tag("caption")
                    cap.append(
                        BeautifulSoup(
                            " ".join(_rich(c) for c in captions), "html.parser"
                        )
                    )
                    child.insert(0, cap)
                    captions = []
                parts.append(str(child))
            elif str(child).strip():
                parts.append(self.node(entry, "p", _table_sibling_html(child)))
        return "\n".join(parts)

    def table_fallback(self, item, entry, soup, captions):
        for math in soup.find_all("math"):
            math.replace_with(f"${math.get('alttext', '')}$")
        image = self.image(item, entry)
        self.warn(
            entry, "table structure is unavailable; retained available source content"
        )
        parts = [self.node(entry, "figure", image)] if image else []
        if soup.get_text(strip=True):
            parts.append(self.node(entry, "p", _rich(soup.get_text(" ", strip=True))))
        parts.extend(self.node(entry, "p", _rich(c)) for c in captions if c.strip())
        return "\n".join(parts)

    def convert(self, item, entry):
        kind = entry["kind"]
        if kind in _PAGE_FURNITURE:
            entry["status"] = "excluded"
            return ""
        if kind in _TEXT_KINDS:
            text = _text(item.get("text", ""))
            level = item.get("text_level")
            tag = f"h{level}" if type(level) is int and 1 <= level <= 6 else "p"
            return self.node(entry, tag, _rich(text)) if text.strip() else ""
        if kind == "equation":
            text = _text(item.get("text", "")).strip()
            if text.startswith("$$") and text.endswith("$$"):
                text = text[2:-2].strip()
            if text:
                sid = f"pdf-p{entry['page_number']}-b{entry['ordinal']}-n0"
                entry["source_ids"].append(sid)
                return f'<math id="{sid}" display="block" alttext="{html.escape(text, quote=True)}"></math>'
            self.warn(entry, "equation text is unavailable")
            image = self.image(item, entry)
            return self.node(entry, "figure", image) if image else ""
        if kind == "table":
            result = self.table(item, entry)
            return (
                result
                + "\n"
                + "\n".join(
                    self.node(entry, "p", _rich(x))
                    for x in _strings(item, "table_footnote")
                    if x.strip()
                )
            )
        if kind in {"image", "chart"}:
            image = self.image(item, entry)
            captions = _strings(item, kind + "_caption")
            parts = []
            if image:
                caption = (
                    "<figcaption>"
                    + " ".join(_rich(x) for x in captions)
                    + "</figcaption>"
                    if captions
                    else ""
                )
                parts.append(self.node(entry, "figure", image + caption))
            else:
                self.warn(entry, "figure image is unavailable")
                parts.extend(
                    self.node(entry, "p", _rich(x)) for x in captions if x.strip()
                )
            if item.get("content"):
                parts.append(self.node(entry, "p", _rich(item["content"])))
            parts.extend(
                self.node(entry, "p", _rich(x))
                for x in _strings(item, kind + "_footnote")
                if x.strip()
            )
            return "\n".join(parts)
        if kind == "list":
            items = _strings(item, "list_items")
            return (
                "<ul>"
                + "".join(self.node(entry, "li", _rich(x)) for x in items if x.strip())
                + "</ul>"
            )
        if kind == "code":
            parts = [
                self.node(entry, "p", _rich(x))
                for x in _strings(item, "code_caption")
                if x.strip()
            ]
            body = _text(item.get("code_body", ""))
            if body.strip():
                if item.get("sub_type") == "algorithm":
                    parts.append(self.node(entry, "p", _rich(body)))
                else:
                    fence = re.fullmatch(
                        r"```([A-Za-z0-9_.+-]*)\r?\n([\s\S]*)\r?\n```", body
                    )
                    language = fence[1] if fence else ""
                    code = fence[2] if fence else body
                    parts.append(
                        self.node(
                            entry,
                            "pre",
                            f'<code class="language-{language}">{html.escape(code)}</code>',
                        )
                    )
            else:
                self.warn(entry, "code body is unavailable")
            parts.extend(
                self.node(entry, "p", _rich(x))
                for x in _strings(item, "code_footnote")
                if x.strip()
            )
            return "\n".join(parts)
        text = _text(item.get("text", ""))
        entry["status"] = "plain_fallback" if text.strip() else "unavailable"
        self.warnings.append(
            f"PDF page {entry['page_number']}, item {entry['ordinal']}: unsupported content type retained where readable."
        )
        return self.node(entry, "p", _rich(text)) if text.strip() else ""


def import_mineru_bundle(
    pdf: str | Path,
    *,
    content_list: str | Path,
    middle_json: str | Path,
    output_dir: str | Path,
    pdf_text_extractor=None,
) -> dict:
    """Normalize existing MinerU pipeline output; never install, run, or contact MinerU."""
    pdf_path, content_path, middle_path = (
        Path(p).expanduser().resolve() for p in (pdf, content_list, middle_json)
    )
    output = Path(output_dir).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise PDFSourceBundleError(
            "pdf_source_output_exists",
            "PDF source output directory must not already exist.",
        )
    pdf_bytes = read_bounded(pdf_path, MAX_PDF_BYTES)
    if not pdf_bytes.startswith(b"%PDF-"):
        raise PDFSourceBundleError(
            "pdf_source_invalid_pdf", "Original source is not a PDF."
        )
    page_count = len(
        (pdf_text_extractor or PdftotextExtractor()).extract(pdf_bytes).pages
    )
    if not 1 <= page_count <= MAX_PAGES:
        raise PDFSourceBundleError(
            "pdf_source_invalid_pdf",
            "Original PDF page count is unavailable or exceeds the limit.",
        )
    content_bytes = read_bounded(content_path, MAX_JSON_BYTES)
    middle_bytes = read_bounded(middle_path, MAX_JSON_BYTES)
    items, middle = read_json(content_bytes), read_json(middle_bytes)
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
    info = middle.get("pdf_info")
    if not isinstance(info, list) or len(info) > page_count:
        raise PDFSourceBundleError(
            "mineru_invalid_page", "MinerU page inventory is invalid."
        )
    sizes, expected_entries = {}, {}
    for page in info:
        if not isinstance(page, dict):
            raise PDFSourceBundleError(
                "mineru_invalid_page", "MinerU page inventory is invalid."
            )
        index = _page_index(page.get("page_idx"), page_count)
        size = page.get("page_size")
        if (
            index in sizes
            or not isinstance(size, list)
            or len(size) != 2
            or any(
                type(v) not in {int, float} or not math.isfinite(v) or v <= 0
                for v in size
            )
        ):
            raise PDFSourceBundleError(
                "mineru_invalid_page", "MinerU page dimensions or identity are invalid."
            )
        sizes[index] = size
        expected_entries[index] = _expected_entries(page)
    restored_items = restore_mineru_page_items(items, middle)
    from .mineru_headings import restore_title_labels

    labeled_items = restore_title_labels(restored_items, middle)
    for index in sizes:
        added = sum(isinstance(v, dict) and v.get("page_idx") == index for v in labeled_items) - sum(
            isinstance(v, dict) and v.get("page_idx") == index for v in restored_items
        )
        if expected_entries[index] is not None:
            expected_entries[index] += added
    from .mineru_headers import classify_running_headers

    restored_items = classify_running_headers(labeled_items)
    page_items_bytes = json_bytes(restored_items) if restored_items != items else None
    items = restored_items
    converter = _Converter(content_path.parent)
    entries, parts = [], []
    # MinerU appends page furniture after body blocks. Group by page while keeping
    # provider order within each page, so footnotes do not move past later pages.
    for ordinal, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("type"), str)
            or not item["type"]
        ):
            raise PDFSourceBundleError(
                "mineru_invalid_result", "MinerU item is invalid."
            )
        index = _page_index(item.get("page_idx"), page_count)
        box = item.get("bbox")
        if box is not None and (
            not isinstance(box, list)
            or len(box) != 4
            or any(
                type(v) not in {int, float}
                or not math.isfinite(v)
                or not 0 <= v <= 1000
                for v in box
            )
            or box[0] > box[2]
            or box[1] > box[3]
        ):
            raise PDFSourceBundleError(
                "mineru_invalid_bbox", "MinerU normalized bounding box is invalid."
            )
        entry = {
            "ordinal": ordinal,
            "page_number": index + 1,
            "kind": item["type"],
            "bbox": box,
            "status": "included",
            "source_ids": [],
        }
        if item.get("img_path"):
            converter.image(item, entry, rendered=False)
        rendered = converter.convert(item, entry)
        if not entry["source_ids"] and entry["status"] == "included":
            entry["status"] = "unavailable"
            converter.warnings.append(
                f"PDF page {index + 1}, item {ordinal}: no readable content was emitted."
            )
        entries.append(entry)
        parts.append((index, ordinal, rendered))
    pages = []
    by_page = {index: [] for index in range(page_count)}
    for entry in entries:
        by_page[entry["page_number"] - 1].append(entry)
    for index in range(page_count):
        owned = by_page[index]
        status = page_coverage_status(
            sizes.get(index), expected_entries.get(index), owned
        )
        pages.append(
            {
                "page_number": index + 1,
                "size": sizes.get(index),
                "status": status,
                "expected_entries": expected_entries.get(index),
            }
        )
        if status in {"partial", "unavailable"}:
            converter.warnings.append(
                f"PDF page {index + 1}: extraction coverage is {status}."
            )
    if not any(e["source_ids"] for e in entries):
        raise PDFSourceBundleError(
            "pdf_source_empty", "MinerU produced no usable PDF content."
        )
    source = (
        '<!doctype html><html><head><meta charset="utf-8"><title>PDF source</title></head>'
        "<body><article>\n"
        + "\n".join(p[2] for p in sorted(parts) if p[2])
        + "\n</article></body></html>\n"
    ).encode()
    files = {
        "original.pdf": pdf_bytes,
        "source.html": source,
        "evidence/content-list.json": content_bytes,
        "evidence/middle.json": middle_bytes,
        **converter.payloads,
    }
    evidence_paths = ["evidence/content-list.json", "evidence/middle.json"]
    if page_items_bytes is not None:
        files["evidence/page-content-list.json"] = page_items_bytes
        evidence_paths.append("evidence/page-content-list.json")
    manifest = {
        "schema_version": PDF_SOURCE_BUNDLE_SCHEMA,
        "provider": {"name": "mineru", "version": "3.4.5", "backend": "pipeline"},
        "original": file_record("original.pdf", pdf_bytes),
        "source": file_record("source.html", source),
        "evidence": [
            file_record(p, files[p])
            for p in evidence_paths
        ],
        "resources": list(converter.resources.values()),
        "pages": pages,
        "entries": entries,
        "warnings": list(dict.fromkeys(converter.warnings)),
    }
    manifest["bundle_digest"] = hashlib.sha256(json_bytes(manifest)).hexdigest()
    files["manifest.json"] = json_bytes(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(output.parent / f".{output.name}.pdf-source.lock"):
        if output.exists() or output.is_symlink():
            raise PDFSourceBundleError(
                "pdf_source_output_exists",
                "PDF source output directory already exists.",
            )
        staging = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
        )
        try:
            for path, payload in files.items():
                target = staging / path
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(target, payload)
            verify_pdf_source_bundle(staging / "manifest.json")
            staging.rename(output)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {
        "source": str(output / "source.html"),
        "manifest": str(output / "manifest.json"),
        "original": str(output / "original.pdf"),
        "bundle_digest": manifest["bundle_digest"],
        "pages": pages,
        "warnings": manifest["warnings"],
    }


__all__ = ["import_mineru_bundle"]
