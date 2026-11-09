"""
A reference GROBID describes with anything at all must be kept in the JSON.

Entries were kept only when they had one of a shortlist of fields (title,
authors, journal, a few identifiers). An entry with only an arXiv id, a URL,
editors or a date was dropped, so the in-text refs pointing at it had no
target in references[]. arXiv-only entries are common in ML papers.
"""
import io

import pytest

from grobid_client.format.TEI2LossyJSON import TEI2LossyJSONConverter


def _references(bibl_struct_body):
    tei = ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text><body/><back>'
           '<div type="references"><listBibl>'
           f'<biblStruct xml:id="b0">{bibl_struct_body}</biblStruct>'
           '</listBibl></div></back></text></TEI>')
    return TEI2LossyJSONConverter().convert_tei_file(io.StringIO(tei))['references']


@pytest.mark.parametrize("body, field, value", [
    ('<monogr><imprint/></monogr><idno type="arXiv">arXiv:1706.03762</idno>', 'arxiv', 'arXiv:1706.03762'),
    ('<monogr><imprint/></monogr><ptr target="https://example.org/x"/>', 'url', 'https://example.org/x'),
    ('<monogr><editor><persName><surname>Smith</surname></persName></editor><imprint/></monogr>',
     'editors', 'Smith'),
    ('<monogr><imprint><publisher>ACM</publisher></imprint></monogr>', 'publisher', 'ACM'),
])
def test_entry_with_some_information_is_kept(body, field, value):
    [ref] = _references(body)
    assert ref['id'] == 'b0'
    assert ref[field] == value


def test_empty_entry_is_still_dropped():
    assert _references('<monogr><imprint/></monogr>') == []
