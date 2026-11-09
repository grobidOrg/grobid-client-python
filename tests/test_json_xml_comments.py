"""
Reference offsets must survive markup that get_text() leaves out.

The passage text comes from get_text(), which skips XML comments and
processing instructions, but the offsets were counted over str(node) for
every string node, comments included. A comment before a reference shifted
its offsets and the validation assert aborted the whole conversion.
"""
import io

import pytest

from grobid_client.format.TEI2LossyJSON import TEI2LossyJSONConverter

BIBL = ('<div type="references"><listBibl><biblStruct xml:id="b0">'
        '<analytic><title level="a">T</title></analytic><monogr><imprint/></monogr>'
        '</biblStruct></listBibl></div>')


def _convert(paragraph):
    tei = ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text><body>'
           f'<div><head>H</head>{paragraph}</div></body><back>{BIBL}</back></text></TEI>')
    return TEI2LossyJSONConverter().convert_tei_file(io.StringIO(tei))


@pytest.mark.parametrize("markup", [
    '<!-- an editor note -->',
    '<?grobid hint?>',
])
def test_markup_outside_the_text_does_not_shift_offsets(markup):
    doc = _convert(f'<p>Text {markup} before <ref type="bibr" target="#b0">[1]</ref> after.</p>')
    passage = doc['body_text'][0]
    assert passage['text'] == 'Text before [1] after.'
    [ref] = passage['refs']
    assert passage['text'][ref['offset_start']:ref['offset_end']] == '[1]'


def test_cdata_is_text_and_counts():
    doc = _convert('<p>A <![CDATA[x < y]]> then <ref type="bibr" target="#b0">[1]</ref>.</p>')
    passage = doc['body_text'][0]
    assert 'x < y' in passage['text']
    [ref] = passage['refs']
    assert passage['text'][ref['offset_start']:ref['offset_end']] == '[1]'


def test_comment_inside_a_sentence():
    doc = _convert('<p><s>One <!-- c --><ref type="bibr" target="#b0">[1]</ref>.</s>'
                   '<s>Two <ref type="bibr" target="#b0">[1]</ref>.</s>'
                   '<s>Three.</s></p>')
    for passage in doc['body_text']:
        for ref in passage['refs']:
            assert passage['text'][ref['offset_start']:ref['offset_end']] == ref['text']
