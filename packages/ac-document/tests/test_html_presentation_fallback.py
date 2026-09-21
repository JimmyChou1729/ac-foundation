import importlib

import pytest

from ac_document import (
    RichBlockKind, RichDocumentParserService, SourceBundle, SourceFormat,
    SourceOrigin, SourceOriginKind, SourceRepository, source_presentation, document_diagnostics,
    parse_rich_artifact_bytes,
)

rich_parser = importlib.import_module("ac_document.rich_document.parser")


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


def test_latexml_inline_svg_is_imported_as_the_figure_asset(tmp_path):
    result = parse(tmp_path, '''<article><figure class="ltx_figure" id="F1">
      <span class="ltx_inline-block"><svg id="F1.pic1" class="ltx_picture"
          width="120" height="60" viewBox="0 0 120 60">
        <path d="M0 0 L120 60"></path>
        <foreignObject x="10" y="10" width="80" height="20">
          <span>axis <math><mi>x</mi></math></span>
        </foreignObject>
      </svg></span>
      <figcaption>Contour diagram.</figcaption>
    </figure></article>''')
    document = result.document
    figures = [
        block for block in document.blocks
        if block.kind is RichBlockKind.FIGURE
    ]
    assert len(figures) == 1
    figure = figures[0]
    assert figure.payload["caption"] == "Contour diagram."
    assert figure.payload["alt_text"] == ""
    assert figure.payload["asset_digest"] == document.assets[0].artifact_digest
    assert figure.payload["media_type"] == "image/svg+xml"
    assert figure.payload["logical_name"].startswith("inline-svg-")
    assert figure.payload["target"] == figure.payload["logical_name"]
    assert "axis x" not in str([block.payload for block in document.blocks])
    repository = SourceRepository(tmp_path / "cache")
    stored = repository.get_asset(figure.payload["asset_digest"])
    svg = repository.read_asset_bytes(stored).decode()
    assert 'viewBox="0 0 120 60"' in svg
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert 'xmlns="http://www.w3.org/1999/xhtml"' in svg
    assert 'xmlns="http://www.w3.org/1998/Math/MathML"' in svg
    assert document_diagnostics(document)["visible_content"]["unaccounted"] == 0


def test_inline_svg_is_structurally_namespaced_and_sanitized(tmp_path):
    result = parse(tmp_path, '''<article><svg aria-label="x > y" viewBox="0 0 8 8"
      onclick="evil()">
      <image href="https://bad/image.png"></image><use href="#safe"></use>
      <linearGradient id="gradient" gradientUnits="userSpaceOnUse"
        spreadMethod="pad"></linearGradient>
      <filter id="filter" filterUnits="objectBoundingBox"></filter>
      <textPath startOffset="20%" pathLength="8">axis</textPath>
      <path id="safe" pathLength="8" d="M0 0L8 8"></path></svg></article>''')
    figure = next(
        block for block in result.document.blocks
        if block.kind is RichBlockKind.FIGURE
    )
    repository = SourceRepository(tmp_path / "cache")
    stored = repository.get_asset(figure.payload["asset_digest"])
    svg = repository.read_asset_bytes(stored).decode()
    assert 'aria-label="x &gt; y"' in svg
    assert 'viewBox="0 0 8 8"' in svg
    assert '<script' not in svg and 'onclick=' not in svg
    assert 'https://bad' not in svg
    assert 'href="#safe"' in svg
    assert 'gradientUnits="userSpaceOnUse"' in svg
    assert 'spreadMethod="pad"' in svg and 'filterUnits="objectBoundingBox"' in svg
    assert 'startOffset="20%"' in svg and svg.count('pathLength="8"') == 2
    sanitized = rich_parser._html_standalone_svg(
        '<svg><script>evil()</script><style>.x{fill:u\\72l(https://bad)}</style>'
        '<foreignObject><img srcset="https://bad/a 1x"/>'
        '<form action="https://bad/post"><button>send</button></form></foreignObject>'
        '<animate values="#safe;javascript:evil()"/>'
        '<path style="fill:u\\72l(https://bad)" '
        'fill="u\\72l(https://bad)" filter="u\\72l(https://bad)" '
        'd="M0 0L1 1"/></svg>'
    )
    assert '<script' not in sanitized and '<style' not in sanitized
    assert 'https://bad' not in sanitized and '<animate' not in sanitized
    assert '<form' not in sanitized and '<button' not in sanitized
    assert 'style=' not in sanitized
    assert 'fill=' not in sanitized and 'filter=' not in sanitized
    compatible = rich_parser._html_standalone_svg(
        '<svg><foreignObject><div>a&nbsp;b<br>c<img alt="x"></div>'
        '</foreignObject><path id="after" d="M0 0L1 1"/></svg>'
    )
    assert "a\xa0b" in compatible and "<br/>c" in compatible
    assert compatible.index("</foreignObject>") < compatible.index('id="after"')


def test_inline_svg_without_importer_has_bounded_warning_and_no_data_target(tmp_path):
    repository = SourceRepository(tmp_path / "cache")
    payload = b'<article><svg><path d="M0 0L8 8"></path></svg></article>'
    artifact = repository.store_bytes(
        payload,
        source_format=SourceFormat.HTML,
        origin=SourceOrigin(SourceOriginKind.LOCAL_IMPORT),
    )
    parsed = parse_rich_artifact_bytes(artifact, payload)
    assert parsed.warnings == ("local asset was not imported: <inline SVG>",)
    figure = next(
        block for block in parsed.document.blocks
        if block.kind is RichBlockKind.FIGURE
    )
    assert figure.payload["target"] == ""


def test_figure_does_not_duplicate_nested_svg_fallbacks(tmp_path):
    result = parse(tmp_path, '''<article><figure><object type="image/svg+xml"
      data="missing.svg"><svg><svg><path d="M0 0L1 1"></path></svg></svg></object>
      <figcaption>Fallback.</figcaption></figure></article>''')
    figures = [
        block for block in result.document.blocks
        if block.kind is RichBlockKind.FIGURE
    ]
    assert len(figures) == 1
    assert figures[0].payload["target"] == "missing.svg"


@pytest.mark.parametrize("markup", [
    '''<math display="block"><mtext><svg width="8" height="8">
      <path d="M0 0L8 8"></path></svg></mtext></math>''',
    '''<table class="ltx_equation"><tr class="ltx_eqn_row"><td>
      <math display="block"><mtext><svg width="8" height="8">
      <path d="M0 0L8 8"></path></svg></mtext></math></td>
      <td class="ltx_eqn_eqno"><span class="ltx_tag">(1)</span></td>
      </tr></table>''',
])
def test_svg_backed_math_without_importer_has_no_data_target(tmp_path, markup):
    repository = SourceRepository(tmp_path / "cache")
    payload = f"<article>{markup}</article>".encode()
    artifact = repository.store_bytes(
        payload,
        source_format=SourceFormat.HTML,
        origin=SourceOrigin(SourceOriginKind.LOCAL_IMPORT),
    )
    parsed = parse_rich_artifact_bytes(artifact, payload)
    figure = next(
        block for block in parsed.document.blocks
        if block.kind is RichBlockKind.FIGURE
    )
    assert figure.payload["target"] == ""
    assert parsed.warnings == ("local asset was not imported: <inline SVG>",)


def test_latexml_svg_backed_math_uses_visual_projection_instead_of_pgf_tex(
    tmp_path,
):
    result = parse(tmp_path, r'''<article><table id="E67" class="ltx_equation">
      <tr class="ltx_equation ltx_eqn_row"><td>
        <math id="E67.m1" display="block"
            alttext="\hbox{\pgfpicture\lxSVG@drawpath@unclipped}">
          <semantics><mrow>
            <mtext><svg width="80" height="32" viewBox="0 0 80 32">
              <path d="M0 16 L80 16"></path>
              <foreignObject x="20" y="2" width="20" height="20">
                <span><math><mi>x</mi></math></span>
              </foreignObject>
            </svg></mtext><mo>=</mo><mi>x</mi>
          </mrow><annotation encoding="application/x-tex">
            \hbox{\pgfpicture\lxSVG@drawpath@unclipped}
          </annotation></semantics>
        </math>
      </td><td class="ltx_eqn_eqno"><span class="ltx_tag">(67)</span></td></tr>
    </table></article>''')
    document = result.document
    assert [block.kind for block in document.blocks] == [RichBlockKind.FIGURE]
    figure = document.blocks[0]
    assert figure.payload["asset_digest"] == document.assets[0].artifact_digest
    assert figure.payload["media_type"] == "image/svg+xml"
    assert figure.payload["target"].startswith("inline-svg-")
    repository = SourceRepository(tmp_path / "cache")
    stored = repository.get_asset(figure.payload["asset_digest"])
    svg = repository.read_asset_bytes(stored).decode()
    assert "<foreignObject" in svg
    assert 'xmlns="http://www.w3.org/1998/Math/MathML"' in svg
    assert "(67)" in svg
    assert "pgfpicture" not in svg
    assert "lxSVG@" not in svg
    assert document_diagnostics(document)["visible_content"]["unaccounted"] == 0


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
