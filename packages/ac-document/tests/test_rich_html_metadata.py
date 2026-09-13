from __future__ import annotations

from ac_document import (
    PDFTextLayer,
    RichBlockKind,
    RichDocumentParserService,
    SourceBundle,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
    source_presentation,
)


class FakePDFTextExtractor:
    contract_id = "ac.document.tests.fake_pdf_text.v1"

    def __init__(self, payload: bytes, pages: tuple[str, ...]) -> None:
        self.payload = payload
        self.pages = pages

    def extract(self, payload: bytes) -> PDFTextLayer:
        assert payload == self.payload
        return PDFTextLayer(self.pages)


def _store(repository, payload, source_format):
    return repository.store_bytes(
        payload,
        source_format=source_format,
        origin=SourceOrigin(SourceOriginKind.LOCAL_IMPORT),
    )


def test_latexml_classification_does_not_capture_unheaded_article_body(
    tmp_path,
) -> None:
    repository = SourceRepository(tmp_path / "cache")
    html = b"""
    <article>
      <h1>Main Result</h1>
      <div class="ltx_abstract">
        <h6>Abstract</h6>
        <p>Eight stable abstract words provide unique page evidence here.</p>
      </div>
      <div class="ltx_classification">
        <h6 class="ltx_title_classification">classification</h6>
        PACS numbers: 01.23.Ab
      </div>
      <p id="body">Eight stable article words continue outside the abstract today.</p>
      <h2>References</h2>
      <p>Eight stable reference words provide another unique page anchor.</p>
    </article>
    """
    pdf = b"%PDF classification fixture"
    primary = _store(repository, html, SourceFormat.HTML)
    validator = _store(repository, pdf, SourceFormat.PDF)

    outcome = RichDocumentParserService(
        repository,
        pdf_text_extractor=FakePDFTextExtractor(
            pdf,
            (
                "Main Result\nEight stable abstract words provide unique page evidence here.\n"
                "Eight stable article words continue outside the abstract today.",
                "References\nEight stable reference words provide another unique page anchor.",
            ),
        ),
    ).parse(SourceBundle(primary=primary, validators=(validator,)))

    document = outcome.document
    assert [section.title for section in document.sections] == [
        "Main Result",
        "Abstract",
        "References",
    ]
    abstract = document.sections[1]
    abstract_heading = document.blocks[abstract.block_start]
    presentation = source_presentation(document)
    assert presentation is not None
    abstract_presentation = next(
        entry
        for entry in presentation["blocks"]
        if entry["block_id"] == abstract_heading.block_id
    )
    assert abstract.level == 2
    assert abstract.path[:-1] == document.sections[0].path
    assert abstract_heading.payload == {"text": "Abstract", "level": 2}
    assert abstract_presentation["roles"] == ("abstract",)
    classification_heading = next(
        block
        for block in document.blocks
        if block.kind is RichBlockKind.HEADING
        and block.payload["text"] == "classification"
    )
    classification = next(
        block
        for block in document.blocks
        if block.kind is RichBlockKind.PARAGRAPH
        and "PACS numbers" in block.payload["text"]
    )
    body = next(block for block in document.blocks if block.locator.source_id == "body")
    assert abstract.block_end == classification_heading.ordinal
    assert classification_heading.section_path == document.sections[0].path
    assert classification.section_path == document.sections[0].path
    assert body.section_path == document.sections[0].path
    assert not any(
        warning.startswith("PDF section evidence")
        for warning in outcome.warnings
    )


def test_pdf_math_diagnostic_warnings_are_bounded_summaries(tmp_path) -> None:
    repository = SourceRepository(tmp_path / "cache")
    math = " ".join(
        f'<math alttext="x_{index}"></math>' for index in range(20)
    )
    html = (
        "<article><h1>Result</h1><p>Eight stable prose words provide unique "
        f"section evidence today. {math}</p></article>"
    ).encode()
    pdf = b"%PDF math warning fixture"
    primary = _store(repository, html, SourceFormat.HTML)
    validator = _store(repository, pdf, SourceFormat.PDF)

    outcome = RichDocumentParserService(
        repository,
        pdf_text_extractor=FakePDFTextExtractor(
            pdf,
            ("Result\nEight stable prose words provide unique section evidence today.",),
        ),
    ).parse(SourceBundle(primary=primary, validators=(validator,)))

    math_warnings = [
        warning
        for warning in outcome.warnings
        if warning.startswith("PDF math evidence")
    ]
    assert math_warnings == [
        "PDF math evidence unreviewed: 20 source subjects; inspect the "
        "reconciliation report for exact diagnostics"
    ]
    assert len(
        [entry for entry in outcome.report.entries if entry.subject_id.startswith("math-")]
    ) == 20


def test_latexml_keywords_compose_inline_preserving_links(tmp_path):
    from bs4 import BeautifulSoup
    from ac_document._parsing.html_source import html_heading_is_document_metadata

    html = '''<article><h1>Paper</h1><div class="ltx_keywords" id="keywords">
      <h6 class="ltx_title ltx_title_keywords">Keywords: </h6>
      <a href="https://astrothesaurus.org/uat/329">Cosmic rays</a> —
      <a href="https://astrothesaurus.org/uat/711">Heliosphere</a>
      </div><p id="body">Unheaded article body.</p></article>'''
    heading = BeautifulSoup(html, "html.parser").find("h6")
    assert html_heading_is_document_metadata(heading)
    repository = SourceRepository(tmp_path / "cache")
    document = RichDocumentParserService(repository).parse_source(
        _store(repository, html.encode(), SourceFormat.HTML)
    )
    presentation = source_presentation(document)
    relation, = presentation["classifications"]
    blocks = {block.block_id:block for block in document.blocks}
    label = blocks[relation["heading_block_id"]]
    assert label.payload["text"] == "Keywords:"
    assert relation["composition"] == "inline"
    assert relation["separator"] == " "
    assert relation["separator_source"] == "latexml_keywords_inline"
    values = [blocks[key] for key in relation["value_block_ids"]]
    assert " ".join(b.payload["text"] for b in values) == "Cosmic rays — Heliosphere"
    field_entries = [entry for entry in presentation["blocks"] if entry["block_id"] in relation["value_block_ids"]]
    links = [span for entry in field_entries for field in entry["fields"] for span in field["inline_spans"] if span["kind"] == "link"]
    assert {span["target"] for span in links} == {"https://astrothesaurus.org/uat/329", "https://astrothesaurus.org/uat/711"}
    body = next(block for block in document.blocks if block.locator.source_id == "body")
    assert body.section_path == label.section_path == document.sections[0].path
    assert any(entry["block_id"] == label.block_id and entry["roles"] == ("classification",) for entry in presentation["blocks"])


def test_keyword_text_alone_does_not_classify_real_heading():
    from bs4 import BeautifulSoup
    from ac_document._parsing.html_source import html_heading_is_document_metadata
    assert not html_heading_is_document_metadata(BeautifulSoup('<h2>Keywords</h2>', 'html.parser').h2)
    assert html_heading_is_document_metadata(BeautifulSoup('<h6 class="ltx_title_keywords">Tags</h6>', 'html.parser').h6)


def test_ambiguous_keyword_wrappers_do_not_create_inline_relations(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    fragments = [
        '<div class="ltx_keywords"><h6 class="ltx_title ltx_title_classification">Labels</h6>Value</div>',
        '<div class="ltx_keywords ltx_classification"><h6 class="ltx_title ltx_title_keywords">Labels</h6>Value</div>',
        '<div class="ltx_keywords"><h6 class="ltx_title ltx_title_keywords">Labels</h6><div class="ltx_classification"><h6 class="ltx_title ltx_title_classification">Nested</h6>Value</div></div>',
    ]
    for fragment in fragments:
        document = RichDocumentParserService(repository).parse_source(
            _store(repository, ('<article><h1>Paper</h1>' + fragment + '</article>').encode(), SourceFormat.HTML)
        )
        assert not source_presentation(document)["classifications"]
