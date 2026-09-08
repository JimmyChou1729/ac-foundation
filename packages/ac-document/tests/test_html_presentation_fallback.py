import pytest

from ac_document import (
    RichBlockKind, RichDocumentParserService, SourceBundle, SourceFormat,
    SourceOrigin, SourceOriginKind, SourceRepository, source_presentation, document_diagnostics,
)


def parse(tmp_path, html):
    repository = SourceRepository(tmp_path / "cache")
    artifact = repository.store_bytes(
        html.encode(), source_format=SourceFormat.HTML,
        origin=SourceOrigin(SourceOriginKind.LOCAL_IMPORT),
    )
    return RichDocumentParserService(repository).parse(SourceBundle(primary=artifact))


def test_wrapped_image_and_interleaved_caption_keep_content_and_anchors(tmp_path):
    result = parse(tmp_path, '''<article><p>Before.</p>
    <figure class="ltx_figure" id="S3.F3">
      <p><span class="ltx_text"><img class="ltx_graphics" id="g1" src="a.png" alt="A"></span></p>
      <figcaption id="cap" class="ltx_centering" style="text-align:end">The caption.</figcaption>
      <img class="ltx_graphics" id="g2" src="b.png" alt="B">
    </figure><p>After.</p></article>''')
    document = result.document
    content = str([block.payload for block in document.blocks])
    assert all(value in content for value in ("Before.", "After.", "The caption.", "a.png", "b.png"))
    diagnostic = document_diagnostics(document)
    assert diagnostic["visible_content"]["unaccounted"] == 0
    assert any(row["category"] == "caption_presentation" for row in diagnostic["projections"])



@pytest.mark.parametrize("style", [
    'style="border: 1px solid red"',
    'class="ltx_border_surprise"',
    'class="ltx_align_left" style="text-align:center"',
])
def test_table_presentation_fallback_preserves_spans_math_and_text(tmp_path, style):
    result = parse(tmp_path, f'''<article><table id="T1"><caption>Data.</caption>
      <tr><th colspan="2" id="cell" {style}>Heading <math><mi>x</mi></math></th></tr>
      <tr><td>One</td><td>Two</td></tr></table></article>''')
    presentation = source_presentation(result.document)
    cells = presentation["tables"][0]["cells"]
    assert cells[0]["column_span"] == 2
    assert cells[0]["horizontal_alignment"] is None
    assert cells[0]["rule_edges"] == ()
    table = next(block for block in result.document.blocks if block.kind is RichBlockKind.TABLE)
    assert all(word in str(table.payload) for word in ["Heading", "x", "One", "Two", "Data."])
    assert any(row["category"] == "table_presentation" for row in document_diagnostics(result.document)["projections"])


def test_caption_only_figure_is_not_lost(tmp_path):
    result = parse(tmp_path, '<article><figure id="F1"><figcaption>Caption without downloaded graphic.</figcaption></figure></article>')
    assert "Caption without downloaded graphic." in str([b.payload for b in result.document.blocks])


@pytest.mark.parametrize("extra", [
    "<p>Extra source explanation.</p>",
    '<svg><path d="M0 0 L1 1"/></svg>',
])
def test_neutral_layout_does_not_discard_extra_visible_content(tmp_path, extra):
    result = parse(tmp_path, f'''<article><figure class="ltx_figure">
    <div class="ltx_flex_figure"><div class="ltx_flex_cell ltx_flex_size_2">
    <img class="ltx_graphics" src="a.png">{extra}
    </div></div><figcaption>Caption.</figcaption></figure></article>''')
    diagnostic = document_diagnostics(result.document)
    assert diagnostic["visible_content"]["unaccounted"] == 0
    if "Extra source explanation." in extra:
        assert "Extra source explanation." in str([b.payload for b in result.document.blocks])
    else:
        assert any(row["category"] == "embedded_media" for row in diagnostic["projections"])



def test_caption_only_preserves_inline_marks_links_math_and_figure_anchor(tmp_path):
    result = parse(tmp_path, '''<article><figure id="F2"><figcaption>
    Continued <em>map</em> <a href="#F2">self</a> <math><mi>x</mi></math>.
    </figcaption></figure></article>''')
    block = result.document.blocks[0]
    assert block.locator.source_id == "F2"
    assert block.kind is RichBlockKind.PARAGRAPH
    assert all(word in str(block.payload) for word in ("map", "self", "x"))
    assert source_presentation(result.document)["blocks"][0]["fields"][0]["marks"]
    assert document_diagnostics(result.document)["visible_content"]["unaccounted"] == 0
