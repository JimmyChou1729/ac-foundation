from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from ac_document import (
    AcDocumentService,
    PDFSourceBundleError,
    import_mineru_bundle,
    restore_mineru_page_items,
    verify_pdf_source_bundle,
)
from ac_document.parse import PDFTextLayer


def line(*spans, y=100):
    return {
        "bbox": [10, y, 90, y + 10],
        "spans": [
            {
                "type": kind,
                "content": text,
                "bbox": [10 + i * 10, y, 20 + i * 10, y + 10],
            }
            for i, (kind, text) in enumerate(spans)
        ],
    }


def text_line(text, **kwargs):
    return line(("text", text), **kwargs)


def paragraph(lines, *, index=1, bbox=None):
    return {
        "type": "text",
        "bbox": bbox or [10, 100, 90, 150],
        "index": index,
        "lines": lines,
    }


def page(index, blocks):
    return {
        "page_idx": index,
        "page_size": [100, 200],
        "para_blocks": copy.deepcopy(blocks),
        "preproc_blocks": copy.deepcopy(blocks),
        "discarded_blocks": [],
    }


def item(index, block, text):
    return {
        "type": "text",
        "page_idx": index,
        "text": text,
        "bbox": [
            int(v * 1000 / [100, 200][i % 2]) for i, v in enumerate(block["bbox"])
        ],
    }


def merge(pages, positions):
    anchor_page, anchor_index = positions[0]
    target = pages[anchor_page]["para_blocks"][anchor_index]
    for page_index, block_index in positions[1:]:
        source = pages[page_index]["para_blocks"][block_index]
        moved = copy.deepcopy(source["lines"])
        if page_index != anchor_page:
            for value in moved:
                for span in value["spans"]:
                    span["cross_page"] = True
        target["lines"].extend(moved)
        source["lines"] = []
        source["lines_deleted"] = True


def sample():
    blocks = [
        paragraph([text_line("A dynamical theory govern-")]),
        paragraph(
            [
                line(("text", "ing matter with"), ("inline_equation", "x^2")),
                text_line("continues here.", y=120),
            ]
        ),
        paragraph([text_line("The final part.")]),
    ]
    pages = [page(i, [block]) for i, block in enumerate(blocks)]
    items = [item(i, block, "") for i, block in enumerate(blocks)]
    items[0]["text"] = (
        "A dynamical theory governing matter with $x^2$ continues here. The final part."
    )
    merge(pages, [(0, 0), (1, 0), (2, 0)])
    return items, {"_backend": "pipeline", "_version_name": "3.4.5", "pdf_info": pages}


def unavailable(items, middle):
    before = copy.deepcopy((items, middle))
    with pytest.raises(PDFSourceBundleError) as caught:
        restore_mineru_page_items(items, middle)
    assert caught.value.code == "mineru_page_restore_unavailable"
    assert (items, middle) == before


def test_unmerged_items_are_unchanged_without_preproc_and_deep_copied():
    items = [{"type": "table", "page_idx": 0, "table_caption": ["Retained"]}]
    middle = {
        "_backend": "pipeline",
        "_version_name": "3.4.5",
        "pdf_info": [{"page_idx": 0}],
    }
    restored = restore_mineru_page_items(items, middle)
    assert restored == items and restored is not items
    restored[0]["table_caption"].append("New")
    assert items[0]["table_caption"] == ["Retained"]


def test_restores_three_pages_with_identical_boxes_and_inline_math_once():
    items, middle = sample()
    before = copy.deepcopy((items, middle))
    restored = restore_mineru_page_items(items, middle)
    assert [entry["text"] for entry in restored] == [
        "A dynamical theory govern-",
        "ing matter with $x^2$ continues here.",
        "The final part.",
    ]
    assert [entry["page_idx"] for entry in restored] == [0, 1, 2]
    assert sum(entry["text"].count("$x^2$") for entry in restored) == 1
    assert restore_mineru_page_items(restored, middle) == restored
    assert (items, middle) == before


def test_repeated_identical_lines_on_different_pages_follow_original_order():
    blocks = [paragraph([text_line("Repeated sentence.")]) for _ in range(3)]
    pages = [page(i, [block]) for i, block in enumerate(blocks)]
    items = [item(i, block, "") for i, block in enumerate(blocks)]
    items[0]["text"] = " ".join(["Repeated sentence."] * 3)
    merge(pages, [(0, 0), (1, 0), (2, 0)])
    middle = {"_backend": "pipeline", "_version_name": "3.4.5", "pdf_info": pages}
    restored = restore_mineru_page_items(items, middle)
    assert [entry["text"] for entry in restored] == ["Repeated sentence."] * 3


def test_multiple_chains_and_same_page_segments_preserve_nontext_item_order():
    first = paragraph([text_line("First part")])
    second = paragraph([text_line("second part.")])
    third = paragraph([text_line("A separate")], index=2, bbox=[10, 155, 90, 180])
    fourth = paragraph([text_line("paragraph.")])
    fifth = paragraph(
        [text_line("Following material.")], index=2, bbox=[10, 155, 90, 180]
    )
    pages = [page(0, [first]), page(1, [second, third]), page(2, [fourth, fifth])]
    merge(pages, [(0, 0), (1, 0)])
    merge(pages, [(1, 1), (2, 0), (2, 1)])
    preserved = [
        {
            "type": "image",
            "img_path": "images/curve.png",
            "image_caption": ["A curve"],
            "page_idx": 1,
        },
        {"type": "equation", "text": "$$y=x^2$$", "page_idx": 1},
        {
            "type": "table",
            "table_body": "<table><tr><td>1</td></tr></table>",
            "page_idx": 2,
        },
    ]
    items = [
        item(0, first, "First part second part."),
        preserved[0],
        item(1, second, ""),
        preserved[1],
        item(1, third, "A separate paragraph. Following material."),
        preserved[2],
        item(2, fourth, ""),
        item(2, fifth, ""),
    ]
    middle = {"_backend": "pipeline", "_version_name": "3.4.5", "pdf_info": pages}
    restored = restore_mineru_page_items(items, middle)
    assert [restored[i] for i in (1, 3, 5)] == preserved
    assert [entry["text"] for entry in restored if entry["type"] == "text"] == [
        "First part",
        "second part.",
        "A separate",
        "paragraph.",
        "Following material.",
    ]
    assert [entry["type"] for entry in restored] == [entry["type"] for entry in items]


@pytest.mark.parametrize("cjk", [False, True])
def test_checks_provider_rendering_without_language_inference(cjk):
    blocks = [paragraph([text_line("中文原文")]), paragraph([text_line("另一頁原文")])]
    pages = [page(i, [block]) for i, block in enumerate(blocks)]
    items = [
        item(0, blocks[0], "中文原文" + ("" if cjk else " ") + "另一頁原文"),
        item(1, blocks[1], ""),
    ]
    merge(pages, [(0, 0), (1, 0)])
    middle = {"_backend": "pipeline", "_version_name": "3.4.5", "pdf_info": pages}
    restored = restore_mineru_page_items(items, middle)
    assert [entry["text"] for entry in restored] == ["中文原文", "另一頁原文"]


@pytest.mark.parametrize(
    "damage",
    [
        "missing_preproc",
        "missing_original",
        "ambiguous_original",
        "ambiguous_item",
        "missing_item",
        "wrong_text",
        "wrong_span",
        "wrong_geometry",
        "wrong_flag",
        "missing_deleted",
        "nonempty_deleted",
        "unsupported_span",
        "duplicate_page",
        "wrong_order",
        "merged_preproc",
    ],
)
def test_incomplete_or_conflicting_evidence_never_guesses_pages(damage):
    items, middle = sample()
    pages = middle["pdf_info"]
    if damage == "missing_preproc":
        pages[1].pop("preproc_blocks")
    elif damage == "missing_original":
        pages[1]["preproc_blocks"] = []
    elif damage == "ambiguous_original":
        pages[1]["preproc_blocks"] *= 2
    elif damage == "ambiguous_item":
        items.append(copy.deepcopy(items[1]))
    elif damage == "missing_item":
        items.pop(1)
    elif damage == "wrong_text":
        items[0]["text"] += " Unproven content."
    elif damage == "wrong_span":
        pages[1]["preproc_blocks"][0]["lines"][0]["spans"][0]["content"] = "Unproven"
    elif damage == "wrong_geometry":
        pages[1]["preproc_blocks"][0]["lines"][0]["spans"][0]["bbox"][0] += 1
    elif damage == "wrong_flag":
        pages[0]["para_blocks"][0]["lines"][1]["spans"][0].pop("cross_page")
    elif damage == "missing_deleted":
        pages[1]["para_blocks"] = []
    elif damage == "nonempty_deleted":
        pages[1]["para_blocks"][0]["lines"] = pages[1]["preproc_blocks"][0]["lines"]
    elif damage == "unsupported_span":
        pages[1]["preproc_blocks"][0]["lines"][0]["spans"][0]["type"] = "image"
    elif damage == "duplicate_page":
        pages[2]["page_idx"] = 1
    elif damage == "wrong_order":
        lines = pages[0]["para_blocks"][0]["lines"]
        lines[1:3] = reversed(lines[1:3])
    elif damage == "merged_preproc":
        pages[1]["preproc_blocks"][0]["lines"][0]["spans"][0]["cross_page"] = True
    unavailable(items, middle)


def test_unrelated_page_without_preproc_remains_unchanged():
    items, middle = sample()
    extra = {"type": "text", "page_idx": 3, "text": "Independent text."}
    items.append(extra)
    middle["pdf_info"].append(
        {
            "page_idx": 3,
            "para_blocks": [
                {"type": "text", "lines": [text_line("Independent text.")]}
            ],
        }
    )
    assert restore_mineru_page_items(items, middle)[-1] == extra


def test_marked_cross_page_table_fails_even_without_text_merges():
    middle = {
        "_backend": "pipeline",
        "_version_name": "3.4.5",
        "pdf_info": [
            page(
                0,
                [
                    {
                        "type": "table",
                        "blocks": [
                            {"type": "table_body", "lines": [], "lines_deleted": True}
                        ],
                    }
                ],
            )
        ],
    }
    unavailable(
        [{"type": "table", "page_idx": 0, "table_body": "<table></table>"}], middle
    )


def test_import_and_rich_export_bind_restored_pages_and_preserve_raw_evidence(tmp_path):
    items, middle = sample()
    content_path, middle_path, pdf = [
        tmp_path / name for name in ("content.json", "middle.json", "input.pdf")
    ]
    content_path.write_text(json.dumps(items))
    middle_path.write_text(json.dumps(middle))
    pdf.write_bytes(b"%PDF-1.4\nSynthetic offline fixture\n")

    class Pages:
        def extract(self, payload):
            return PDFTextLayer(("", "", ""))

    result = import_mineru_bundle(
        pdf,
        content_list=content_path,
        middle_json=middle_path,
        output_dir=tmp_path / "bundle",
        pdf_text_extractor=Pages(),
    )
    manifest = verify_pdf_source_bundle(result["manifest"])
    assert [entry["status"] for entry in manifest["pages"]] == ["parsed"] * 3
    bundle = Path(result["manifest"]).parent
    assert (
        bundle / "evidence/content-list.json"
    ).read_bytes() == content_path.read_bytes()
    assert (bundle / "evidence/middle.json").read_bytes() == middle_path.read_bytes()
    derived = json.loads((bundle / "evidence/page-content-list.json").read_text())
    assert derived == restore_mineru_page_items(items, middle)
    assert len(manifest["evidence"]) == 3
    exported = AcDocumentService(cache_root=tmp_path / "cache").export_rich_document(
        result["source"],
        output_dir=tmp_path / "publication",
        pdf_source_manifest=result["manifest"],
    )
    document = json.loads(Path(exported["source"]).read_text())
    pages = {entry["block_id"]: entry["page_number"] for entry in document["page_map"]}
    text_pages = {
        block["payload"]["text"]: pages[block["block_id"]]
        for block in document["blocks"]
        if block["kind"] == "paragraph"
    }
    assert text_pages["A dynamical theory govern-"] == 1
    assert text_pages["ing matter with x^2 continues here."] == 2
    math = [
        span
        for block in document["blocks"]
        for span in block["payload"].get("inline_spans", [])
        if span["kind"] == "math"
    ]
    assert len(math) == 1 and math[0]["tex"] == "x^2"
    assert text_pages["The final part."] == 3
    (bundle / "evidence/page-content-list.json").write_text("[]")
    with pytest.raises(PDFSourceBundleError, match="bytes"):
        verify_pdf_source_bundle(result["manifest"])


def test_failed_restore_never_publishes_bundle(tmp_path):
    items, middle = sample()
    middle["pdf_info"][1].pop("preproc_blocks")
    for name, content in (("content.json", items), ("middle.json", middle)):
        (tmp_path / name).write_text(json.dumps(content))
    (tmp_path / "input.pdf").write_bytes(b"%PDF-1.4\n")

    class Pages:
        def extract(self, payload):
            return PDFTextLayer(("", "", ""))

    with pytest.raises(PDFSourceBundleError) as caught:
        import_mineru_bundle(
            tmp_path / "input.pdf",
            content_list=tmp_path / "content.json",
            middle_json=tmp_path / "middle.json",
            output_dir=tmp_path / "bundle",
            pdf_text_extractor=Pages(),
        )
    assert caught.value.code == "mineru_page_restore_unavailable"
    assert not (tmp_path / "bundle").exists()


def test_index_merge_restores_page_local_text_without_changing_types():
    items, middle = sample()
    for page_info in middle['pdf_info']:
        for collection in ('para_blocks', 'preproc_blocks'):
            for block in page_info[collection]:
                block['type'] = 'index'
    before = copy.deepcopy((items, middle))
    result = restore_mineru_page_items(items, middle)
    assert result[0]['text'] == 'A dynamical theory govern-'
    assert result[1]['text'] == 'ing matter with $x^2$ continues here.'
    assert result[2]['text'] == 'The final part.'
    assert all(entry['type'] == 'text' for entry in result)
    assert (items, middle) == before


def test_mixed_index_text_merge_is_rejected():
    items, middle = sample()
    middle['pdf_info'][1]['para_blocks'][0]['type'] = 'index'
    with pytest.raises(PDFSourceBundleError):
        restore_mineru_page_items(items, middle)
