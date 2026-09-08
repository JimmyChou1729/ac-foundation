import pytest

from ac_document import (
    RichBlockKind,
    RichDocumentParserService,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
    source_presentation, document_diagnostics,
)


def parse(tmp_path, contents):
    repository = SourceRepository(tmp_path / "cache")
    artifact = repository.store_bytes(
        (
            '<article><figure class="ltx_figure" id="F1">'
            '<div class="ltx_flex_figure">' + contents + "</div>"
            "<figcaption>Shared caption.</figcaption></figure></article>"
        ).encode(),
        source_format=SourceFormat.HTML,
        origin=SourceOrigin(SourceOriginKind.LOCAL_IMPORT),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def test_captionless_latexml_wrapped_panels_preserve_graphics_and_layout(tmp_path):
    cells = "".join(
        f'<div class="ltx_flex_cell ltx_flex_size_1">'
        f'<figure class="ltx_figure ltx_figure_panel" id="F1.fig{index}">'
        f'<img class="ltx_graphics" id="F1.g{index}" src="panel-{index}.png" '
        'width="381" height="381" style="aspect-ratio:381/381;">'
        "</figure></div>"
        for index in (1, 2)
    )
    document = parse(tmp_path, cells)
    figures = [block for block in document.blocks if block.kind is RichBlockKind.FIGURE]
    assert len(figures) == 1
    assert figures[0].payload["caption"] == "Shared caption."
    presentation = source_presentation(document)["figures"][0]
    assert presentation["layout"]["rows"] == ((0,), (1,))
    assert [panel["display_width"] for panel in presentation["panels"]] == [381, 381]
    assert [panel["source_id"] for panel in presentation["panels"]] == [
        "F1.g1",
        "F1.g2",
    ]


@pytest.mark.parametrize(
    "extra",
    [
        "<figcaption>A distinct panel caption.</figcaption>",
        "Visible panel prose.",
        '<img class="ltx_graphics" src="another.png">',
    ],
)
def test_wrapped_panels_do_not_silently_discard_extra_content(tmp_path, extra):
    document = parse(
        tmp_path,
        '<div class="ltx_flex_cell ltx_flex_size_1">'
        '<figure class="ltx_figure ltx_figure_panel">'
        '<img class="ltx_graphics" src="panel.png">' + extra + "</figure></div>",
    )
    payload = str([block.payload for block in document.blocks])
    assert "Shared caption." in payload and "panel.png" in payload
    expected = ("A distinct panel caption." if "figcaption" in extra else
                "another.png" if "another.png" in extra else "Visible panel prose.")
    assert expected in payload
    diagnostic = document_diagnostics(document)
    assert diagnostic["visible_content"]["unaccounted"] == 0
    assert any(row["category"] == "figure_layout" for row in diagnostic["projections"])
