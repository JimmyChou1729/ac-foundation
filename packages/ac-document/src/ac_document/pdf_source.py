"""Portable PDF derivatives with explicit, unreviewed extraction provenance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

PDF_SOURCE_BUNDLE_SCHEMA = "ac.document.pdf_source_bundle.v1"
PDF_SOURCE_PROVENANCE_SCHEMA = "ac.document.pdf_source_provenance.v1"
MAX_PDF_BYTES = None
MAX_JSON_BYTES = 50 * 1024 * 1024
MAX_RESOURCE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_RESOURCE_BYTES = 200 * 1024 * 1024
MAX_PAGES = 10000
MAX_ENTRIES = 100000


class PDFSourceBundleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _object(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties if required is None else required),
        "additionalProperties": False,
    }


_HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_STRING = {"type": "string", "minLength": 1}
_PAGE_NUMBER = {"type": "integer", "minimum": 1, "maximum": MAX_PAGES}
_BBOX = {
    "anyOf": [
        {"type": "null"},
        {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": {"type": "number", "minimum": 0, "maximum": 1000},
        },
    ]
}
_FILE_FIELDS = {
    "path": _STRING,
    "sha256": _HASH,
    "size": {"type": "integer", "minimum": 0},
}
_FILE = _object(_FILE_FIELDS)
_RESOURCE = _object(
    {
        **_FILE_FIELDS,
        "media_type": {"enum": ["image/png", "image/jpeg", "image/webp"]},
        "rendered": {"type": "boolean"},
        "original_paths": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": _STRING,
        },
    }
)
_PAGE = _object(
    {
        "page_number": _PAGE_NUMBER,
        "size": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "items": {"type": "number", "exclusiveMinimum": 0},
                },
            ]
        },
        "status": {"enum": ["parsed", "partial", "empty", "unavailable"]},
        "expected_entries": {
            "type": ["integer", "null"],
            "minimum": 0,
            "maximum": MAX_ENTRIES,
        },
    }
)
_ENTRY = _object(
    {
        "ordinal": {"type": "integer", "minimum": 0},
        "page_number": _PAGE_NUMBER,
        "kind": _STRING,
        "bbox": _BBOX,
        "status": {"enum": ["included", "excluded", "unavailable", "plain_fallback"]},
        "source_ids": {"type": "array", "uniqueItems": True, "items": _STRING},
    }
)
_PAGES = {"type": "array", "minItems": 1, "maxItems": MAX_PAGES, "items": _PAGE}
_MANIFEST = _object(
    {
        "schema_version": {"const": PDF_SOURCE_BUNDLE_SCHEMA},
        "provider": _object({"name": _STRING, "version": _STRING, "backend": _STRING}),
        "original": _FILE,
        "source": _FILE,
        "evidence": {"type": "array", "minItems": 1, "maxItems": 10, "items": _FILE},
        "resources": {"type": "array", "maxItems": MAX_ENTRIES, "items": _RESOURCE},
        "pages": _PAGES,
        "entries": {"type": "array", "maxItems": MAX_ENTRIES, "items": _ENTRY},
        "warnings": {"type": "array", "items": _STRING},
        "bundle_digest": _HASH,
    }
)
_PROVENANCE = _object(
    {
        "schema_version": {"const": PDF_SOURCE_PROVENANCE_SCHEMA},
        "bundle_digest": _HASH,
        "original_sha256": _HASH,
        "source_sha256": _HASH,
        "proofread": {"const": False},
        "bbox_units": {"const": "page_1000"},
        "pages": _PAGES,
        "blocks": {
            "type": "array",
            "items": _object(
                {
                    "block_id": _STRING,
                    "page_number": _PAGE_NUMBER,
                    "entry_ordinal": {"type": "integer", "minimum": 0},
                    "bbox": _BBOX,
                }
            ),
        },
    }
)


_PROOFREADING = _object(
    {
        "source_bundle_digest": _HASH,
        "candidate_digest": _HASH,
        "review_sha256": _HASH,
        "source_sha256": _HASH,
    }
)
_PROOFREADING["properties"]["review_mode"] = {"enum": ["model", "human"]}
_PROOFREADING["properties"]["uncertainty_count"] = {"type": "integer", "minimum": 0}


def _review_mode(review):
    if not isinstance(review, dict):
        return None
    if any(
        not isinstance(review.get(key, {}), dict)
        or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in review.get(key, {}).items()
        )
        for key in ("manual_edits", "manual_resolutions")
    ):
        return None
    count = review.get("uncertainty_count")
    if type(count) is not int or count < 0:
        return None
    if review.get("schema_version") not in {
        "ac.document.pdf_review.v1",
        "ac.document.pdf_review.v2",
    }:
        return None
    if (
        review.get("reviewer") == "user"
        and review.get("approved") is True
        and count == 0
    ):
        return "human"
    if (
        review.get("schema_version") == "ac.document.pdf_review.v2"
        and review.get("reviewer") == "model"
        and review.get("approved") is False
    ):
        return "model"
    return None


_MANIFEST["properties"]["proofreading"] = _PROOFREADING
_PROVENANCE["properties"]["proofreading"] = _PROOFREADING
_PROVENANCE["properties"]["proofread"] = {"type": "boolean"}


def _plain(value):
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def file_record(path: str, payload: bytes) -> dict:
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def read_bounded(path: Path, limit: int | None) -> bytes:
    with path.open("rb") as stream:
        payload = stream.read() if limit is None else stream.read(limit + 1)
    if limit is not None and len(payload) > limit:
        raise PDFSourceBundleError(
            "pdf_source_too_large", "PDF source input exceeds its byte limit."
        )
    return payload


def safe_relative(value: str) -> str:
    from urllib.parse import unquote, urlsplit

    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or unquote(value) != value
        or urlsplit(value).scheme
        or urlsplit(value).netloc
        or "?" in value
        or "#" in value
    ):
        raise PDFSourceBundleError(
            "pdf_source_unsafe_path", "PDF resource path must be a plain relative path."
        )
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", "..", ""} for part in value.split("/")):
        raise PDFSourceBundleError(
            "pdf_source_unsafe_path", "PDF resource path escapes its bundle."
        )
    return path.as_posix()


def read_json(payload: bytes):
    try:
        return json.loads(
            payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PDFSourceBundleError(
            "pdf_source_invalid_json", "PDF source JSON is invalid."
        ) from exc


def _validate(schema, value):
    try:
        valid = Draft202012Validator(schema).is_valid(value)
        json_bytes(value)
    except (ValueError, TypeError, RecursionError):
        valid = False
    if not valid:
        raise PDFSourceBundleError(
            "pdf_source_invalid", "PDF source contract is invalid."
        )


def _validate_pages(pages):
    if [p["page_number"] for p in pages] != list(range(1, len(pages) + 1)):
        raise PDFSourceBundleError(
            "pdf_source_invalid", "PDF page coverage must be contiguous."
        )
    for page in pages:
        if page["status"] == "empty" and (
            page["size"] is None or page["expected_entries"] != 0
        ):
            raise PDFSourceBundleError(
                "pdf_source_invalid",
                "Empty PDF page conflicts with its provider inventory.",
            )
        if page["status"] == "parsed" and (
            page["size"] is None or page["expected_entries"] is None
        ):
            raise PDFSourceBundleError(
                "pdf_source_invalid", "Parsed PDF page needs its provider inventory."
            )


def page_coverage_status(size, expected_entries, entries):
    visible = any(e["source_ids"] for e in entries)
    reported = sum(e["status"] != "excluded" for e in entries)
    missing = (
        size is None
        or expected_entries is None
        or reported < expected_entries
        or any(e["status"] == "unavailable" for e in entries)
    )
    return (
        ("partial" if visible else "unavailable")
        if missing
        else ("parsed" if visible else "empty")
    )


def _validate_bbox(box):
    if box is not None and (box[0] > box[2] or box[1] > box[3]):
        raise PDFSourceBundleError(
            "pdf_source_invalid", "PDF bounding box is inverted."
        )


def verify_pdf_source_bundle(manifest: str | Path) -> dict:
    """Verify a local manifest and every bound file without invoking OCR or network."""
    path = Path(manifest).expanduser().resolve()
    root = path.parent
    value = read_json(read_bounded(path, MAX_JSON_BYTES))
    _validate(_MANIFEST, value)
    expected = value["bundle_digest"]
    body = {k: v for k, v in value.items() if k != "bundle_digest"}
    if hashlib.sha256(json_bytes(body)).hexdigest() != expected:
        raise PDFSourceBundleError(
            "pdf_source_digest_mismatch",
            "PDF source manifest bytes differ from their identity.",
        )
    _validate_pages(value["pages"])
    if [e["ordinal"] for e in value["entries"]] != list(range(len(value["entries"]))):
        raise PDFSourceBundleError(
            "pdf_source_invalid", "PDF source entries must be contiguous."
        )
    source_ids = set()
    for entry in value["entries"]:
        if entry["page_number"] > len(value["pages"]):
            raise PDFSourceBundleError(
                "pdf_source_invalid",
                "PDF source entry is outside the original page range.",
            )
        _validate_bbox(entry["bbox"])
        if source_ids.intersection(entry["source_ids"]):
            raise PDFSourceBundleError(
                "pdf_source_invalid", "PDF source anchors are duplicated."
            )
        source_ids.update(entry["source_ids"])
        if entry["status"] == "excluded" and entry["source_ids"]:
            raise PDFSourceBundleError(
                "pdf_source_invalid", "Excluded PDF content cannot own visible anchors."
            )
        if (
            entry["status"] in {"included", "plain_fallback"}
            and not entry["source_ids"]
        ):
            raise PDFSourceBundleError(
                "pdf_source_invalid", "Included PDF content needs visible anchors."
            )
    by_page = {p["page_number"]: [] for p in value["pages"]}
    for entry in value["entries"]:
        by_page[entry["page_number"]].append(entry)
    for page in value["pages"]:
        owned = by_page[page["page_number"]]
        status = page_coverage_status(page["size"], page["expected_entries"], owned)
        if page["status"] != status:
            raise PDFSourceBundleError(
                "pdf_source_invalid", "PDF page status disagrees with content coverage."
            )
    records = [
        value["original"],
        value["source"],
        *value["evidence"],
        *value["resources"],
    ]
    paths = set()
    source_payload = b""
    review_payloads = {}
    review_paths = {
        "review/approval.json",
        "review/original-manifest.json",
        "review/original-source.html",
    }
    if sum(r["size"] for r in value["resources"]) > MAX_TOTAL_RESOURCE_BYTES:
        raise PDFSourceBundleError(
            "pdf_source_too_large", "PDF resources exceed the total byte limit."
        )
    for record in records:
        relative = safe_relative(record["path"])
        if relative in paths or relative == path.name:
            raise PDFSourceBundleError(
                "pdf_source_invalid", "PDF bundle file roles overlap."
            )
        paths.add(relative)
        target = root / relative
        if not target.resolve().is_relative_to(root):
            raise PDFSourceBundleError(
                "pdf_source_unsafe_path", "PDF bundle resource escapes its directory."
            )
        component = root
        for part in PurePosixPath(relative).parts:
            component = component / part
            if component.is_symlink():
                raise PDFSourceBundleError(
                    "pdf_source_unsafe_path", "PDF bundle resource cannot be a symlink."
                )
        limit = MAX_PDF_BYTES if record is value["original"] else MAX_JSON_BYTES
        if record in value["resources"]:
            limit = MAX_RESOURCE_BYTES
            for original in record["original_paths"]:
                safe_relative(original)
        try:
            payload = read_bounded(target, limit)
        except OSError as exc:
            raise PDFSourceBundleError(
                "pdf_source_file_missing", "PDF bundle file is missing or unreadable."
            ) from exc
        if (
            len(payload) != record["size"]
            or hashlib.sha256(payload).hexdigest() != record["sha256"]
        ):
            raise PDFSourceBundleError(
                "pdf_source_digest_mismatch",
                "PDF bundle file bytes differ from the manifest.",
            )
        if record is value["source"]:
            source_payload = payload
        if value.get("proofreading") and relative in review_paths:
            review_payloads[relative] = payload
    soup = BeautifulSoup(source_payload, "html.parser")
    ids = [str(n["id"]) for n in soup.find_all(id=True)]
    if len(ids) != len(set(ids)) or not source_ids.issubset(set(ids)):
        raise PDFSourceBundleError(
            "pdf_source_invalid", "PDF source anchors do not match the HTML."
        )
    resources = {r["path"] for r in value["resources"] if r["rendered"]}
    if {img.get("src") for img in soup.find_all("img")} != resources:
        raise PDFSourceBundleError(
            "pdf_source_invalid",
            "PDF source images disagree with the resource inventory.",
        )
    for img in soup.find_all("img"):
        if img.get("src") not in resources:
            raise PDFSourceBundleError(
                "pdf_source_invalid", "PDF source image is not bound by the manifest."
            )
    if value.get("proofreading"):
        from .pdf_revision import _structure

        receipt = value["proofreading"]
        review_path = "review/approval.json"
        original_path = "review/original-manifest.json"
        original_source_path = "review/original-source.html"
        by_path = {e["path"]: e for e in value["evidence"]}
        if (
            not review_paths.issubset(by_path)
            or by_path[review_path]["sha256"] != receipt["review_sha256"]
        ):
            raise PDFSourceBundleError(
                "pdf_review_invalid", "Review evidence is missing or mismatched."
            )
        review = read_json(review_payloads[review_path])
        previous = read_json(review_payloads[original_path])
        _validate(_MANIFEST, previous)
        previous_digest = hashlib.sha256(
            json_bytes({k: v for k, v in previous.items() if k != "bundle_digest"})
        ).hexdigest()
        if (
            previous_digest != receipt["source_bundle_digest"]
            or previous["bundle_digest"] != previous_digest
            or previous.get("proofreading")
            or not isinstance(review, dict)
            or _review_mode(review) is None
            or receipt.get("review_mode", "human") != _review_mode(review)
            or (
                review.get("schema_version") == "ac.document.pdf_review.v2"
                and receipt.get("uncertainty_count") != review.get("uncertainty_count")
            )
            or review.get("source_bundle_digest") != previous_digest
            or review.get("source_sha256") != previous["source"]["sha256"]
            or review.get("reviewed_source_sha256") != value["source"]["sha256"]
            or receipt["source_sha256"] != value["source"]["sha256"]
            or review.get("candidate_digest") != receipt["candidate_digest"]
            or type(review.get("page_count")) is not int
            or review["page_count"] != len(value["pages"])
        ):
            raise PDFSourceBundleError(
                "pdf_review_invalid",
                "Review does not bind this approved source revision.",
            )
        if (
            any(
                value[field] != previous[field]
                for field in ("original", "pages", "entries", "resources", "provider")
            )
            or value["source"]["path"] != previous["source"]["path"]
            or any(
                record["path"] in review_paths or by_path.get(record["path"]) != record
                for record in previous["evidence"]
            )
            or any(
                by_path[original_source_path][field] != previous["source"][field]
                for field in ("size", "sha256")
            )
        ):
            raise PDFSourceBundleError(
                "pdf_review_invalid", "Review changed the original source bindings."
            )
        if _structure(review_payloads[original_source_path]) != _structure(
            source_payload
        ):
            raise PDFSourceBundleError(
                "pdf_review_structure_changed",
                "Review changed source structure or resource bindings.",
            )
    return value


def bind_pdf_source(document, manifest: dict):
    """Bind exact normalized HTML anchors, never infer pages from OCR wording."""
    from .rich_document.models import RichPageMapEntry

    if document.source.artifact_digest != manifest["source"]["sha256"]:
        raise PDFSourceBundleError(
            "pdf_source_mismatch", "PDF manifest belongs to another normalized source."
        )
    expected_assets = {r["path"]: r for r in manifest["resources"] if r["rendered"]}
    if {a.logical_name for a in document.assets} != set(expected_assets):
        raise PDFSourceBundleError(
            "pdf_source_asset_mismatch", "Normalized PDF images were not all imported."
        )
    for asset in document.assets:
        record = expected_assets[asset.logical_name]
        if (asset.artifact_digest, asset.size, asset.media_type) != (
            record["sha256"],
            record["size"],
            record["media_type"],
        ):
            raise PDFSourceBundleError(
                "pdf_source_asset_mismatch",
                "Normalized PDF image bytes changed during import.",
            )
    owners = {sid: e for e in manifest["entries"] for sid in e["source_ids"]}
    by_block = {}
    for block in document.blocks:
        if block.locator.source_id in owners:
            by_block[block.block_id] = owners[block.locator.source_id]
    for target in document.metadata.get("source_target_manifest", {}).get(
        "targets", ()
    ):
        owner = owners.get(target["alias"])
        if owner is None:
            continue
        for block in document.blocks[target["block_start"] : target["block_end"]]:
            previous = by_block.setdefault(block.block_id, owner)
            if previous["page_number"] != owner["page_number"]:
                raise PDFSourceBundleError(
                    "pdf_source_mapping_conflict",
                    "PDF page anchors overlap inconsistently.",
                )
    if len(by_block) != len(document.blocks):
        raise PDFSourceBundleError(
            "pdf_source_mapping_missing",
            "Some normalized content has no exact PDF page anchor.",
        )
    provenance = {
        "schema_version": PDF_SOURCE_PROVENANCE_SCHEMA,
        "bundle_digest": manifest["bundle_digest"],
        "original_sha256": manifest["original"]["sha256"],
        "source_sha256": manifest["source"]["sha256"],
        "proofread": bool(manifest.get("proofreading"))
        and manifest["proofreading"].get("review_mode", "human") == "human",
        **(
            {"proofreading": manifest["proofreading"]}
            if manifest.get("proofreading")
            else {}
        ),
        "bbox_units": "page_1000",
        "pages": manifest["pages"],
        "blocks": [
            {
                "block_id": b.block_id,
                "page_number": by_block[b.block_id]["page_number"],
                "entry_ordinal": by_block[b.block_id]["ordinal"],
                "bbox": by_block[b.block_id]["bbox"],
            }
            for b in document.blocks
        ],
    }
    return replace(
        document,
        page_map=tuple(
            RichPageMapEntry(b.block_id, by_block[b.block_id]["page_number"])
            for b in document.blocks
        ),
        metadata={**document.metadata, "pdf_source": provenance},
    )


def validate_pdf_source_metadata(value, blocks, page_map, source):
    if value.get("proofread") is not (
        bool(value.get("proofreading"))
        and value["proofreading"].get("review_mode", "human") == "human"
    ):
        raise PDFSourceBundleError(
            "pdf_review_invalid", "Proofreading flag requires its review identity."
        )
    if value.get("proofreading") and value["proofreading"].get(
        "source_sha256"
    ) != value.get("source_sha256"):
        raise PDFSourceBundleError(
            "pdf_review_invalid", "Proofreading identity belongs to another source."
        )

    value = _plain(value)
    _validate(_PROVENANCE, value)
    _validate_pages(value["pages"])
    if value["source_sha256"] != source.artifact_digest:
        raise ValueError("PDF provenance belongs to another normalized source")
    ids = [b.block_id for b in blocks]
    if [b["block_id"] for b in value["blocks"]] != ids:
        raise ValueError("PDF provenance must cover each rich block in source order")
    mapped = {p.block_id: p.page_number for p in page_map}
    if len(mapped) != len(ids):
        raise ValueError("PDF provenance needs a complete rich page map")
    for block in value["blocks"]:
        _validate_bbox(block["bbox"])
        if (
            block["page_number"] > len(value["pages"])
            or mapped.get(block["block_id"]) != block["page_number"]
        ):
            raise ValueError("PDF provenance disagrees with the rich page map")


__all__ = [
    "PDF_SOURCE_BUNDLE_SCHEMA",
    "PDF_SOURCE_PROVENANCE_SCHEMA",
    "PDFSourceBundleError",
    "verify_pdf_source_bundle",
]
