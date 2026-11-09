"""
Markdown references must show the year and arXiv id GROBID extracted.

GROBID usually writes a reference date as an attribute only
(<date type="published" when="2019"/>); the year was read from the element
text, so it was missing from nearly every reference (50 of the 51 in the
sample document). arXiv identifiers were never rendered at all.
"""
import io
import os

from grobid_client.format.TEI2Markdown import TEI2MarkdownConverter
from tests.resources import TEST_DATA_PATH


def _references(bibl_struct_body):
    tei = ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text><body/><back>'
           '<div type="references"><listBibl>'
           f'<biblStruct xml:id="b0">{bibl_struct_body}</biblStruct>'
           '</listBibl></div></back></text></TEI>')
    md = TEI2MarkdownConverter().convert_tei_file(io.StringIO(tei))
    return md.split("## References", 1)[1].strip()


def test_year_from_the_when_attribute():
    ref = _references('<analytic><title level="a">A paper</title></analytic>'
                      '<monogr><imprint><date type="published" when="2019-05-03"/></imprint></monogr>')
    assert "(2019)" in ref


def test_year_from_the_element_text_still_works():
    ref = _references('<analytic><title level="a">A paper</title></analytic>'
                      '<monogr><imprint><date type="published">May 2018</date></imprint></monogr>')
    assert "(2018)" in ref


def test_published_date_wins_over_other_dates():
    ref = _references('<analytic><title level="a">A paper</title></analytic>'
                      '<monogr><imprint><date type="accessed" when="2023"/>'
                      '<date type="published" when="2017"/></imprint></monogr>')
    assert "(2017)" in ref and "2023" not in ref


def test_arxiv_identifier_is_rendered_as_a_link():
    ref = _references('<analytic><title level="a">Attention</title></analytic><monogr><imprint/></monogr>'
                      '<idno type="arXiv">arXiv:1706.03762</idno>')
    assert "[arXiv:1706.03762](https://arxiv.org/abs/1706.03762)" in ref


def test_sample_document_references_have_years():
    sample = os.path.join(TEST_DATA_PATH, '0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml')
    md = TEI2MarkdownConverter().convert_tei_file(sample)
    refs = [line for line in md.split("## References", 1)[1].splitlines() if line.startswith("**[")]
    with_year = [line for line in refs if any(f"({y})" in line for y in range(1900, 2100))]
    assert len(refs) == 49
    assert len(with_year) >= 45
