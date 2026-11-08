"""
A reference's URLs and identifiers must appear once in the converted output.

Both converters collected <ptr> and <idno> per section and then again with a
recursive search over the whole biblStruct, so every link was recorded twice.
"""
import os
import tempfile

from grobid_client.format.TEI2LossyJSON import TEI2LossyJSONConverter
from grobid_client.format.TEI2Markdown import TEI2MarkdownConverter
from tests.resources import TEST_DATA_PATH

SAMPLE = os.path.join(TEST_DATA_PATH, 'refs_offsets', '10.1038_s41598-023-32039-z.grobid.tei.xml')

REF = ('<biblStruct xml:id="b0">'
       '<analytic><title level="a" type="main">A paper</title>'
       '<author><persName><forename>A</forename><surname>Author</surname></persName></author>'
       '<idno type="DOI">10.1000/xyz</idno><ptr target="https://doi.org/10.1000/xyz"/></analytic>'
       '<monogr><title level="j">J. Things</title><idno type="ISSN">1234-5678</idno>'
       '<imprint><date type="published" when="2020">2020</date></imprint></monogr>'
       '<idno type="MD5">ABC</idno><ptr target="https://example.org/top"/>'
       '</biblStruct>')


def _tei(d):
    path = os.path.join(d, 'x.tei.xml')
    with open(path, 'w', encoding='utf8') as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?><TEI xmlns="http://www.tei-c.org/ns/1.0">'
                 '<teiHeader><fileDesc><titleStmt><title level="a" type="main">T</title></titleStmt>'
                 '</fileDesc></teiHeader><text><body><div><p>Body.</p></div></body>'
                 f'<back><div type="references"><listBibl>{REF}</listBibl></div></back></text></TEI>')
    return path


def test_json_links_and_identifiers_are_unique():
    with tempfile.TemporaryDirectory() as d:
        ref = TEI2LossyJSONConverter().convert_tei_file(_tei(d), stream=False)['references'][0]
    assert ref['doi'] == '10.1000/xyz'
    assert ref['issn'] == '1234-5678'
    assert ref['urls'] == ['https://doi.org/10.1000/xyz', 'https://example.org/top']
    assert 'url' not in ref
    assert ref['identifiers'] == {'biblstruct_identifiers_md5': 'ABC'}


def test_json_single_link_is_a_scalar_url():
    with tempfile.TemporaryDirectory() as d:
        path = _tei(d)
        with open(path, encoding='utf8') as fh:
            content = fh.read().replace('<ptr target="https://example.org/top"/>', '')
        with open(path, 'w', encoding='utf8') as fh:
            fh.write(content)
        ref = TEI2LossyJSONConverter().convert_tei_file(path, stream=False)['references'][0]
    assert ref['url'] == 'https://doi.org/10.1000/xyz'
    assert 'urls' not in ref


def test_markdown_prints_each_link_once():
    with tempfile.TemporaryDirectory() as d:
        md = TEI2MarkdownConverter().convert_tei_file(_tei(d))
    refs = md.split('## References')[1]
    assert refs.count('https://doi.org/10.1000/xyz') == 1
    assert refs.count('https://example.org/top') == 1
    assert refs.count('10.1000/xyz') == 1        # the DOI link only, not a second [doi.org](...) link


def test_sample_references_have_no_duplicate_urls():
    doc = TEI2LossyJSONConverter().convert_tei_file(SAMPLE, stream=False)
    assert doc['references']
    for ref in doc['references']:
        urls = ref.get('urls', [ref['url']] if 'url' in ref else [])
        assert len(urls) == len(set(urls)), ref['id']
    md = TEI2MarkdownConverter().convert_tei_file(SAMPLE)
    for line in md.split('## References')[1].splitlines():
        # the DOI link is printed once; a <ptr> to the same address is not repeated
        doi_links = [t for t in line.split() if t.startswith('https://doi.org/')]
        if doi_links:
            assert f'({doi_links[0]})' not in line, line
