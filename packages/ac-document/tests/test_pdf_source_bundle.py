from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

from ac_document import (
    AcDocumentService,
    PDFSourceBundleError,
    import_mineru_bundle,
    verify_pdf_source_bundle,
)
from ac_document.cli import main
from ac_document.parse import PDFTextLayer
from ac_document.rich_document import rich_document_from_document


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)


class Pages:
    def extract(self, payload):
        assert payload.startswith(b"%PDF-")
        return PDFTextLayer(("", "", ""))


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    (root / "original.pdf").write_bytes(b"%PDF-1.4\nsynthetic test input\n")
    (root / "images").mkdir()
    (root / "images" / "figure.png").write_bytes(PNG)
    items = [
        {"type": "text", "text": "Energy", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "For $v=3$, energy is 9 J.", "page_idx": 0},
        {"type": "equation", "text": "$$E=\\frac12 mv^2$$", "page_idx": 0},
        {
            "type": "table",
            "table_body": "<table><tr><th>v</th><th>E</th></tr><tr><td>3</td><td>9</td></tr></table>",
            "table_caption": ["Measurements"],
            "table_footnote": ["Mass is 2 kg."],
            "page_idx": 0,
        },
        {
            "type": "page_footnote",
            "text": "An important source footnote.",
            "page_idx": 0,
        },
        {"type": "page_number", "text": "1", "page_idx": 0},
        {
            "type": "image",
            "img_path": "images/figure.png",
            "image_caption": ["Quadratic curve."],
            "bbox": [10, 20, 900, 950],
            "page_idx": 2,
        },
    ]
    (root / "content_list.json").write_text(json.dumps(items))
    (root / "middle.json").write_text(
        json.dumps(
            {
                "_backend": "pipeline",
                "_version_name": "3.4.5",
                "pdf_info": [
                    {
                        "page_idx": i,
                        "page_size": [595, 842],
                        "para_blocks": [
                            {
                                "type": item["type"],
                                "lines": [
                                    {
                                        "spans": [
                                            {
                                                "content": item.get(
                                                    "text",
                                                    item.get(
                                                        "table_body",
                                                        item.get("img_path", ""),
                                                    ),
                                                )
                                            }
                                        ]
                                    }
                                ],
                            }
                            for item in items
                            if item["page_idx"] == i and item["type"] != "page_number"
                        ],
                        "discarded_blocks": [],
                    }
                    for i in range(3)
                ],
            }
        )
    )
    return root


def ingest(source, output, **kwargs):
    return import_mineru_bundle(
        source / "original.pdf",
        content_list=source / "content_list.json",
        middle_json=source / "middle.json",
        output_dir=output,
        pdf_text_extractor=Pages(),
        **kwargs,
    )


def edit_items(source, edit):
    p = source / "content_list.json"
    items = json.loads(p.read_text())
    edit(items)
    p.write_text(json.dumps(items))


def test_preserves_structure_assets_and_page_provenance(source, tmp_path):
    result = ingest(source, tmp_path / "bundle")
    manifest = verify_pdf_source_bundle(result["manifest"])
    assert [p["status"] for p in manifest["pages"]] == ["parsed", "empty", "parsed"]
    assert "An important source footnote." in Path(result["source"]).read_text()
    export = AcDocumentService(cache_root=tmp_path / "cache").export_rich_document(
        result["source"],
        output_dir=tmp_path / "publication",
        pdf_source_manifest=result["manifest"],
    )
    raw = json.loads(Path(export["source"]).read_text())
    doc = rich_document_from_document(raw)
    assert len([b for b in doc.blocks if b.kind.value == "table"]) == 1
    table = next(b for b in doc.blocks if b.kind.value == "table")
    assert table.payload["rows"] == (("3", "9"),)
    figure = next(b for b in doc.blocks if b.kind.value == "figure")
    assert figure.payload["caption"] == "Quadratic curve."
    assert any("source footnote" in str(b.payload) for b in doc.blocks)
    assert len(doc.page_map) == len(doc.blocks)
    assert (
        next(p.page_number for p in doc.page_map if p.block_id == figure.block_id) == 3
    )
    assert (
        doc.metadata["pdf_source"]["original_sha256"] == manifest["original"]["sha256"]
    )
    assert doc.metadata["pdf_source"]["proofread"] is False
    assert export["warnings"]


def test_bundle_is_portable_and_verifies_every_bound_file(source, tmp_path):
    import shutil

    r = ingest(source, tmp_path / "bundle")
    moved = tmp_path / "moved"
    shutil.move(str(Path(r["manifest"]).parent), moved)
    manifest = verify_pdf_source_bundle(moved / "manifest.json")
    resource = manifest["resources"][0]
    (moved / resource["path"]).write_bytes(b"changed")
    with pytest.raises(PDFSourceBundleError, match="bytes"):
        verify_pdf_source_bundle(moved / "manifest.json")


def test_keeps_equation_and_table_crops_as_evidence_without_duplicate_figures(
    source, tmp_path
):
    (source / "images/crop.png").write_bytes(PNG + b"crop")
    edit_items(source, lambda items: items[2].update(img_path="images/crop.png"))
    r = ingest(source, tmp_path / "bundle")
    m = verify_pdf_source_bundle(r["manifest"])
    assert sorted(x["rendered"] for x in m["resources"]) == [False, True]
    e = AcDocumentService(cache_root=tmp_path / "cache").export_rich_document(
        r["source"],
        output_dir=tmp_path / "out",
        pdf_source_manifest=r["manifest"],
    )
    d = json.loads(Path(e["source"]).read_text())
    assert len(d["assets"]) == 1


def test_empty_recognized_region_is_not_claimed_as_verified_blank(source, tmp_path):
    edit_items(
        source, lambda items: items.append({"type": "text", "text": "", "page_idx": 1})
    )
    r = ingest(source, tmp_path / "bundle")
    m = verify_pdf_source_bundle(r["manifest"])
    assert m["pages"][1]["status"] == "unavailable"
    assert m["warnings"]


@pytest.mark.parametrize(
    "target",
    [
        "../outside.png",
        "/tmp/outside.png",
        "https://example.com/a.png",
        "images/../../outside.png",
        "images\\outside.png",
    ],
)
def test_unsafe_resource_paths_are_rejected_before_publish(source, tmp_path, target):
    edit_items(source, lambda items: items[-1].update(img_path=target))
    with pytest.raises(PDFSourceBundleError):
        ingest(source, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_missing_asset_and_unreported_page_remain_explicit_partial_result(
    source, tmp_path
):
    (source / "images/figure.png").unlink()
    p = source / "middle.json"
    d = json.loads(p.read_text())
    d["pdf_info"].pop(1)
    p.write_text(json.dumps(d))
    result = ingest(source, tmp_path / "bundle")
    m = verify_pdf_source_bundle(result["manifest"])
    assert m["pages"][1]["status"] == "unavailable"
    assert m["pages"][2]["status"] == "partial"
    assert "Quadratic curve." in Path(result["source"]).read_text()
    assert m["warnings"]


@pytest.mark.parametrize("index", [-1, 3, True, 1.5])
def test_invalid_page_indices_reject_without_output(source, tmp_path, index):
    edit_items(source, lambda items: items[0].update(page_idx=index))
    with pytest.raises(PDFSourceBundleError):
        ingest(source, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_unknown_content_preserves_text_with_diagnostic(source, tmp_path):
    edit_items(
        source,
        lambda items: items.insert(
            2, {"type": "future_block", "text": "Keep this evidence", "page_idx": 0}
        ),
    )
    r = ingest(source, tmp_path / "bundle")
    assert "Keep this evidence" in Path(r["source"]).read_text()
    m = verify_pdf_source_bundle(r["manifest"])
    assert any(e["status"] == "plain_fallback" for e in m["entries"])


@pytest.mark.parametrize("fenced", [False, True])
def test_code_only_document_keeps_body_caption_and_footnote(source, tmp_path, fenced):
    body = '  print("<script>$x$</script>")\n  print(42)'
    (source / "content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "code",
                    "sub_type": "code",
                    "page_idx": 0,
                    "code_body": f"```python\n{body}\n```" if fenced else body,
                    "code_caption": ["Example program"],
                    "code_footnote": ["Source explanation"],
                }
            ]
        )
    )
    r = ingest(source, tmp_path / "bundle")
    e = AcDocumentService(cache_root=tmp_path / "cache").export_rich_document(
        r["source"],
        output_dir=tmp_path / "out",
        pdf_source_manifest=r["manifest"],
    )
    d = rich_document_from_document(json.loads(Path(e["source"]).read_text()))
    code = next(b for b in d.blocks if b.kind.value == "code")
    assert code.payload["text"] == body
    if fenced:
        assert code.payload["language"] == "python"
    assert any("Example program" in str(b.payload) for b in d.blocks)
    assert any("Source explanation" in str(b.payload) for b in d.blocks)
    assert len(d.page_map) == len(d.blocks)


def test_active_html_and_fake_math_markup_cannot_execute(source, tmp_path):
    edit_items(
        source,
        lambda items: items[3].update(
            table_body='<table onclick="evil()"><tr><td><script>evil()</script><b>9</b></td></tr></table>'
        ),
    )
    edit_items(
        source,
        lambda items: items[1].update(text='<img src="https://evil.example/"> $x<y$'),
    )
    r = ingest(source, tmp_path / "bundle")
    html = Path(r["source"]).read_text()
    assert "<script" not in html and "onclick=" not in html
    assert '<img src="https://' not in html
    assert "&lt;img" in html


def test_output_is_never_overwritten_and_unrelated_manifest_cannot_bind(
    source, tmp_path
):
    r = ingest(source, tmp_path / "bundle")
    with pytest.raises(PDFSourceBundleError):
        ingest(source, tmp_path / "bundle")
    other = tmp_path / "other.html"
    other.write_text("<p>Another source</p>")
    with pytest.raises(PDFSourceBundleError):
        AcDocumentService(cache_root=tmp_path / "cache").export_rich_document(
            other,
            output_dir=tmp_path / "out",
            pdf_source_manifest=r["manifest"],
        )
    assert not (tmp_path / "out").exists()


def test_rich_codec_rejects_forged_pdf_page_provenance(source, tmp_path):
    r = ingest(source, tmp_path / "bundle")
    e = AcDocumentService(cache_root=tmp_path / "cache").export_rich_document(
        r["source"],
        output_dir=tmp_path / "out",
        pdf_source_manifest=r["manifest"],
    )
    d = json.loads(Path(e["source"]).read_text())
    forged = copy.deepcopy(d)
    forged["page_map"][0]["page_number"] = 99
    with pytest.raises(ValueError, match="PDF provenance disagrees"):
        rich_document_from_document(forged)


def test_public_cli_and_registry_do_not_need_web_or_mineru_runtime(
    source, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("ac_document.mineru.PdftotextExtractor", Pages)
    assert (
        main(
            [
                "import-mineru-bundle",
                str(source / "original.pdf"),
                "--content-list",
                str(source / "content_list.json"),
                "--middle-json",
                str(source / "middle.json"),
                "--output-dir",
                str(tmp_path / "bundle"),
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "completed"
    assert main(["verify-pdf-source-bundle", data["data"]["manifest"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    result = data["data"]
    assert (
        main(
            [
                "export-rich-document",
                result["source"],
                "--pdf-source-manifest",
                result["manifest"],
                "--output-dir",
                str(tmp_path / "publication"),
                "--cache-root",
                str(tmp_path / "cache"),
            ]
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    document = json.loads(Path(exported["data"]["source"]).read_text())
    assert len(document["page_map"]) == len(document["blocks"])


@pytest.mark.parametrize("role", ["original", "source", "evidence"])
def test_tampering_with_any_bound_role_is_detected(source, tmp_path, role):
    r = ingest(source, tmp_path / "bundle")
    m = verify_pdf_source_bundle(r["manifest"])
    record = m[role][0] if role == "evidence" else m[role]
    (Path(r["manifest"]).parent / record["path"]).write_bytes(b"changed")
    with pytest.raises(PDFSourceBundleError, match="bytes"):
        verify_pdf_source_bundle(r["manifest"])


def test_symlink_resource_escape_is_rejected(source, tmp_path):
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG)
    asset = source / "images/figure.png"
    asset.unlink()
    asset.symlink_to(outside)
    with pytest.raises(PDFSourceBundleError):
        ingest(source, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize(
    "field,value", [("_version_name", "3.5.0"), ("_backend", "vlm")]
)
def test_unsupported_provider_contract_has_no_output(source, tmp_path, field, value):
    p = source / "middle.json"
    d = json.loads(p.read_text())
    d[field] = value
    p.write_text(json.dumps(d))
    with pytest.raises(PDFSourceBundleError, match="3.4.5 pipeline"):
        ingest(source, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_no_usable_content_never_publishes_an_empty_success(source, tmp_path):
    (source / "content_list.json").write_text("[]")
    with pytest.raises(PDFSourceBundleError, match="no usable"):
        ingest(source, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("same_page", [False, True])
def test_missing_middle_content_is_a_coverage_gap(source, tmp_path, same_page):
    p = source / "middle.json"
    middle = json.loads(p.read_text())
    page = middle["pdf_info"][0 if same_page else 1]
    page["para_blocks"] = [
        {
            "type": "text",
            "lines": [
                {
                    "spans": [
                        {
                            "type": "text",
                            "content": "Evidence missing from content list",
                        }
                    ]
                }
            ],
        }
        for _ in range(8 if same_page else 1)
    ]
    page["discarded_blocks"] = []
    p.write_text(json.dumps(middle))
    r = ingest(source, tmp_path / "bundle")
    m = verify_pdf_source_bundle(r["manifest"])
    assert m["pages"][0 if same_page else 1]["status"] == (
        "partial" if same_page else "unavailable"
    )
    assert any("coverage" in w for w in m["warnings"])


@pytest.mark.parametrize(
    "body", ["<table><tr></tr></table>", "<table><tr><td> </td></tr></table>"]
)
def test_empty_table_only_cannot_publish_a_success(source, tmp_path, body):
    (source / "content_list.json").write_text(
        json.dumps([{"type": "table", "table_body": body, "page_idx": 0}])
    )
    with pytest.raises(PDFSourceBundleError, match="no usable"):
        ingest(source, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_empty_table_preserves_available_crop_and_caption_as_partial(source, tmp_path):
    (source / "content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "table",
                    "table_body": "<table><tr><td></td></tr></table>",
                    "page_idx": 0,
                    "img_path": "images/figure.png",
                    "table_caption": ["Available caption"],
                }
            ]
        )
    )
    r = ingest(source, tmp_path / "bundle")
    m = verify_pdf_source_bundle(r["manifest"])
    assert m["pages"][0]["status"] == "partial"
    e = AcDocumentService(cache_root=tmp_path / "cache").export_rich_document(
        r["source"],
        output_dir=tmp_path / "out",
        pdf_source_manifest=r["manifest"],
    )
    d = rich_document_from_document(json.loads(Path(e["source"]).read_text()))
    assert not any(b.kind.value == "table" for b in d.blocks)
    assert any(b.kind.value == "figure" for b in d.blocks)
    assert any("Available caption" in str(b.payload) for b in d.blocks)


def test_image_change_between_bundle_verification_and_parsing_is_detected(
    source, tmp_path, monkeypatch
):
    from ac_document.rich_document.service import RichDocumentParserService

    r = ingest(source, tmp_path / "bundle")
    m = verify_pdf_source_bundle(r["manifest"])
    original_parse = RichDocumentParserService.parse

    def changing_parse(self, bundle):
        (Path(r["manifest"]).parent / m["resources"][0]["path"]).write_bytes(
            PNG + b"changed"
        )
        return original_parse(self, bundle)

    monkeypatch.setattr(RichDocumentParserService, "parse", changing_parse)
    with pytest.raises(PDFSourceBundleError, match="image bytes changed"):
        AcDocumentService(cache_root=tmp_path / "cache").export_rich_document(
            r["source"],
            output_dir=tmp_path / "out",
            pdf_source_manifest=r["manifest"],
        )
    assert not (tmp_path / "out").exists()


def test_merged_reference_inventory_is_complete(source, tmp_path):
    p = source / "middle.json"
    middle = json.loads(p.read_text())
    middle["pdf_info"][0]["para_blocks"] = [
        {"type": "ref_text", "lines": [{"spans": [{"content": text}]}]}
        for text in ["First citation", "Second citation"]
    ]
    p.write_text(json.dumps(middle))
    (source / "content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "list",
                    "sub_type": "ref_text",
                    "list_items": ["First citation", "Second citation"],
                    "page_idx": 0,
                }
            ]
        )
    )
    r = ingest(source, tmp_path / "bundle")
    m = verify_pdf_source_bundle(r["manifest"])
    assert m["pages"][0]["status"] == "parsed"
    assert m["pages"][0]["expected_entries"] == 1


def test_empty_grid_does_not_discard_valid_formula_grid(source, tmp_path):
    (source / "content_list.json").write_text(
        json.dumps(
            [
                {
                    "type": "table",
                    "page_idx": 0,
                    "table_body": "<table><tr><td>$x^2$</td></tr></table><table><tr><td></td></tr></table>",
                }
            ]
        )
    )
    r = ingest(source, tmp_path / "bundle")
    html = Path(r["source"]).read_text()
    assert 'alttext="x^2"' in html
    assert html.count("<table") == 1
    assert verify_pdf_source_bundle(r["manifest"])["pages"][0]["status"] == "partial"


@pytest.mark.parametrize("sibling", [
    "<p>For $x^2$ use:</p>",
    "$x^2$",
    "<p onclick='bad()'>For <em>$x^2$</em> use:<script>bad()</script></p>",
    r"<p>Price \$5 and \$8; $x^2$</p>",
])
def test_table_sibling_preserves_inline_formula(source, tmp_path, sibling):
    from bs4 import BeautifulSoup

    (source / "content_list.json").write_text(json.dumps([
        {"type": "table", "page_idx": 0,
         "table_body": sibling + "<table><tr><td>1</td></tr></table>"}
    ]))
    result = ingest(source, tmp_path / "bundle")
    soup = BeautifulSoup(Path(result["source"]).read_text(), "html.parser")
    assert [math["alttext"] for math in soup.find_all("math")] == ["x^2"]
    assert soup.find("table").get_text(strip=True) == "1"
    assert soup.find("script") is None
    assert not soup.find_all(attrs={"onclick": True})
    if "Price" in sibling:
        assert "Price $5 and $8;" in soup.get_text()


def test_provider_text_escapes_are_decoded_without_changing_math_or_code(source, tmp_path):
    from bs4 import BeautifulSoup
    from ac_document.mineru_pages import _text_span

    original = ("x_y costs $5; * literal * and `code` ~ text"
                + r"; C:\files\book and \\literal and \\_path")
    formula = r"\frac{x_1}{2}"
    code = r"value = r'\_\$'"
    (source / "content_list.json").write_text(json.dumps([
        {"type": "text", "page_idx": 0,
         "text": _text_span(original) + " $" + formula + "$"},
        {"type": "code", "page_idx": 0, "code_body": code},
        {"type": "text", "page_idx": 0, "text": r"\# Heading marker"},
    ]))
    result = ingest(source, tmp_path / "bundle")
    soup = BeautifulSoup(Path(result["source"]).read_text(), "html.parser")
    paragraph = soup.find("p")
    assert paragraph.get_text().strip() == original
    assert paragraph.find("math")["alttext"] == formula
    assert soup.find("code").get_text() == code
    assert soup.find_all("p")[-1].get_text() == "# Heading marker"


@pytest.mark.parametrize(
    "blocks,expected",
    [
        ([("ref_text", ""), ("ref_text", "Readable reference")], 1),
        ([("ref_text", "A"), ("header", "Furniture"), ("ref_text", "B")], 2),
    ],
)
def test_reference_groups_preserve_coverage_gaps(source, tmp_path, blocks, expected):
    p = source / "middle.json"
    middle = json.loads(p.read_text())
    middle["pdf_info"][1]["para_blocks"] = [
        {"type": kind, "lines": [{"spans": [{"content": text}]}]}
        for kind, text in blocks
    ]
    p.write_text(json.dumps(middle))
    r = ingest(source, tmp_path / "bundle")
    page = verify_pdf_source_bundle(r["manifest"])["pages"][1]
    assert page["expected_entries"] == expected
    assert page["status"] == "unavailable"


def test_original_pdf_read_has_no_former_250_mib_ceiling(tmp_path):
    from ac_document.pdf_source import MAX_PDF_BYTES, read_bounded
    path = tmp_path / 'large.pdf'
    size = 250 * 1024 * 1024 + 1
    with path.open('wb') as stream:
        stream.write(b'%PDF-1.4\n')
        stream.truncate(size)
    assert len(read_bounded(path, MAX_PDF_BYTES)) == size


def test_recovered_title_label_does_not_mask_missing_body(source, tmp_path):
    items = [
        {'type': 'image', 'img_path': 'images/figure.png', 'bbox': [115, 134, 163, 194], 'page_idx': 0},
        {'type': 'text', 'text': 'Title', 'text_level': 1, 'bbox': [394, 133, 798, 200], 'page_idx': 0},
    ]
    (source / 'content_list.json').write_text(json.dumps(items))
    middle = json.loads((source / 'middle.json').read_text())
    page = middle['pdf_info'][0]
    page['para_blocks'] = [{'type': 'text', 'lines': [{'spans': [{'type': 'text', 'content': 'body'}]}]} for _ in range(3)]
    page['discarded_blocks'] = [{'type': 'header', 'bbox': [22, 82, 152, 101], 'lines': [{'spans': [{'type': 'text', 'content': 'PART'}]}]}]
    (source / 'middle.json').write_text(json.dumps(middle))
    result = ingest(source, tmp_path / 'bundle')
    manifest = verify_pdf_source_bundle(result['manifest'])
    assert manifest['pages'][0]['expected_entries'] == 4
    assert manifest['pages'][0]['status'] == 'partial'
    assert 'PART' in Path(result['source']).read_text()


def test_import_filters_misclassified_headers_before_body_generation(source, tmp_path):
    items = [
        {"type": "text", "text": "1.4 Vectors", "text_level": 2,
         "page_idx": 0, "bbox": [220, 99, 400, 114]},
        {"type": "text", "text": "1.4 VECTORS", "text_level": 2,
         "page_idx": 0, "bbox": [220, 500, 400, 515]},
        {"type": "text", "text": "1.4 Vectors", "text_level": 2,
         "page_idx": 2, "bbox": [220, 99, 400, 114]},
    ]
    content = json.dumps(items).encode()
    (source / "content_list.json").write_bytes(content)
    middle = json.loads((source / "middle.json").read_text())
    for page in middle["pdf_info"]:
        page["para_blocks"] = [
            {"type": "title", "lines": [{"spans": [{"content": item["text"]}]}]}
            for item in items if item["page_idx"] == page["page_idx"]
        ]
    (source / "middle.json").write_text(json.dumps(middle))
    output = tmp_path / "filtered"
    ingest(source, output)
    html = (output / "source.html").read_text()
    assert "1.4 Vectors" not in html
    assert html.count("1.4 VECTORS") == 1
    assert (output / "evidence/content-list.json").read_bytes() == content
    manifest = json.loads((output / "manifest.json").read_text())
    assert [e["status"] for e in manifest["entries"]] == ["excluded", "included", "excluded"]
    verify_pdf_source_bundle(output / "manifest.json")
