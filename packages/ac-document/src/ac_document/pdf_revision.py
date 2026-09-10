"""Publish an explicitly reviewed PDF-source text revision without losing assets."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from ac_jobs import atomic_write_bytes, file_lease
from bs4 import BeautifulSoup

from .pdf_source import (
    PDFSourceBundleError,
    _review_mode,
    file_record,
    json_bytes,
    verify_pdf_source_bundle,
)


def _structure(payload):
    soup = BeautifulSoup(payload, "html.parser")
    tags = list(soup.find_all(True))
    positions = {id(tag): index for index, tag in enumerate(tags)}
    return [
        (
            tag.name,
            positions.get(id(tag.parent)),
            {
                k: v
                for k, v in tag.attrs.items()
                if not (tag.name == "math" and k == "alttext")
                and not (tag.name == "img" and k == "alt")
            },
        )
        for tag in tags
    ]


def publish_reviewed_pdf_source(
    manifest, *, reviewed_source: bytes, review: dict, output_dir
):
    original = verify_pdf_source_bundle(manifest)
    if original.get("proofreading"):
        raise PDFSourceBundleError(
            "pdf_review_invalid", "This source already has a proofreading revision."
        )
    root = Path(manifest).resolve().parent
    before = (root / original["source"]["path"]).read_bytes()
    source_digest = hashlib.sha256(reviewed_source).hexdigest()
    required = {
        "schema_version",
        "source_bundle_digest",
        "source_sha256",
        "reviewed_source_sha256",
        "candidate_digest",
        "reviewer",
        "approved",
        "uncertainty_count",
        "page_count",
    }
    if (
        (
            not required.issubset(review)
            or set(review)
            - required
            - (
                {"manual_edits", "manual_resolutions"}
                if review.get("schema_version") == "ac.document.pdf_review.v2"
                else set()
            )
        )
        or _review_mode(review) is None
        or review["source_bundle_digest"] != original["bundle_digest"]
        or review["source_sha256"] != original["source"]["sha256"]
        or review["reviewed_source_sha256"] != source_digest
        or type(review["page_count"]) is not int
        or review["page_count"] != len(original["pages"])
        or not isinstance(review["candidate_digest"], str)
        or len(review["candidate_digest"]) != 64
    ):
        raise PDFSourceBundleError(
            "pdf_review_invalid",
            "A complete, explicitly approved source-bound review is required.",
        )
    if _structure(before) != _structure(reviewed_source):
        raise PDFSourceBundleError(
            "pdf_review_structure_changed",
            "Review changed source structure or resource bindings.",
        )
    review_payload = json_bytes(review)
    review_digest = hashlib.sha256(review_payload).hexdigest()
    output = Path(output_dir).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with file_lease(output.parent / ("." + output.name + ".lock")):
        if output.exists():
            existing = verify_pdf_source_bundle(output / "manifest.json")
            if (
                existing.get("proofreading", {}).get("candidate_digest")
                != review["candidate_digest"]
                or existing["source"]["sha256"] != source_digest
                or existing["proofreading"].get("source_bundle_digest")
                != original["bundle_digest"]
                or existing["proofreading"].get("review_sha256") != review_digest
            ):
                raise PDFSourceBundleError(
                    "pdf_review_output_exists", "Output belongs to another review."
                )
            return {
                "manifest": str(output / "manifest.json"),
                "source": str(output / original["source"]["path"]),
            }
        stage = Path(tempfile.mkdtemp(prefix=".pdf-review-", dir=output.parent))
        try:
            for record in [
                original["original"],
                original["source"],
                *original["evidence"],
                *original["resources"],
            ]:
                destination = stage / record["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / record["path"], destination)
            evidence = {
                "review/approval.json": review_payload,
                "review/original-manifest.json": json_bytes(original),
                "review/original-source.html": before,
            }
            if any((stage / path).exists() for path in evidence):
                raise PDFSourceBundleError(
                    "pdf_review_invalid", "Reserved review paths already exist."
                )
            for path, payload in evidence.items():
                atomic_write_bytes(stage / path, payload)
            atomic_write_bytes(stage / original["source"]["path"], reviewed_source)
            value = {
                **original,
                "source": file_record(original["source"]["path"], reviewed_source),
                "evidence": [
                    *original["evidence"],
                    *[file_record(path, payload) for path, payload in evidence.items()],
                ],
                "proofreading": {
                    "source_bundle_digest": original["bundle_digest"],
                    "candidate_digest": review["candidate_digest"],
                    "review_sha256": review_digest,
                    "source_sha256": source_digest,
                    **(
                        {
                            "review_mode": _review_mode(review),
                            "uncertainty_count": review["uncertainty_count"],
                        }
                        if review["schema_version"] == "ac.document.pdf_review.v2"
                        else {}
                    ),
                },
                "warnings": [
                    w
                    for w in original["warnings"]
                    if w
                    != "OCR extraction has not been proofread against the original PDF."
                ],
            }
            if _review_mode(review) == "model":
                value["warnings"].append(
                    "OCR was checked by a model and has not been reviewed by a person."
                )
                if review["uncertainty_count"]:
                    value["warnings"].append(
                        f"Model OCR review retained {review['uncertainty_count']} unresolved uncertainties."
                    )
            value["bundle_digest"] = hashlib.sha256(
                json_bytes({k: v for k, v in value.items() if k != "bundle_digest"})
            ).hexdigest()
            atomic_write_bytes(stage / "manifest.json", json_bytes(value))
            verify_pdf_source_bundle(stage / "manifest.json")
            stage.rename(output)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {
        "manifest": str(output / "manifest.json"),
        "source": str(output / original["source"]["path"]),
    }
