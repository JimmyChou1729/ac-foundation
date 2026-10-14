import pytest
from bs4 import BeautifulSoup

from ac_document import RichBlockKind, RichDocumentParserService, SourceRepository
from ac_document.rich_document.parser import _html_single_inline_segment


@pytest.mark.parametrize('caption', [None, '', ' \n\t ', '&nbsp;', '<em></em>'])
def test_empty_table_and_figure_captions_parse_as_missing(tmp_path, caption):
    (tmp_path/'figure.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    table_caption = '' if caption is None else f'<caption>{caption}</caption>'
    figure_caption = '' if caption is None else f'<figcaption>{caption}</figcaption>'
    text = (f'<table id="t">{table_caption}<tbody><tr><td>1.25</td><td>kg</td></tr></tbody></table>'
            f'<figure id="f"><img src="figure.svg" alt="Diagram">{figure_caption}</figure>')
    source = tmp_path/'source.html'; source.write_text(text)
    repository = SourceRepository(tmp_path/'cache')
    document = RichDocumentParserService(repository).parse_source(repository.import_path(source))
    assert [b.kind for b in document.blocks] == [RichBlockKind.TABLE, RichBlockKind.FIGURE]
    assert all(b.payload['caption'] == '' for b in document.blocks)
    assert document.blocks[0].payload['rows'] == (('1.25', 'kg'),)
    assert document.blocks[1].payload['alt_text'] == 'Diagram'
    assert source.read_text() == text


@pytest.mark.parametrize('tag', ['caption', 'figcaption'])
def test_nonempty_caption_keeps_inline_semantics(tag):
    node = BeautifulSoup(f'<{tag}><em>Value</em> <math alttext="E=mc^2"></math></{tag}>', 'html.parser').find(tag)
    result = _html_single_inline_segment(node, allow_empty=True)
    assert result['text'].startswith('Value')
    assert any(span['kind'] == 'math' and span['tex'] == 'E=mc^2' for span in result['inline_spans'])


@pytest.mark.parametrize('content', ['<img src="image.png">', '<math display="block" alttext="E=mc^2"></math>'])
@pytest.mark.parametrize('tag', ['caption', 'figcaption'])
def test_caption_does_not_silently_discard_unsupported_content(tag, content):
    node = BeautifulSoup(f'<{tag}>{content}</{tag}>', 'html.parser').find(tag)
    with pytest.raises(ValueError, match='single inline segment'):
        _html_single_inline_segment(node, allow_empty=True)


def test_required_inline_field_still_rejects_empty():
    with pytest.raises(ValueError, match='no inline content'):
        _html_single_inline_segment(BeautifulSoup('<h2></h2>', 'html.parser').h2)
