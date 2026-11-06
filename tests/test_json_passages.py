"""
Passages must not be lost: a paragraph GROBID did not segment into
sentences is kept whole, and a div that mixes paragraphs with nested divs
is traversed completely with each heading on its own section.
"""
import os
import tempfile

from grobid_client.format.TEI2LossyJSON import TEI2LossyJSONConverter
from grobid_client.format.TEI2Markdown import TEI2MarkdownConverter

HEADER = ('<teiHeader><fileDesc><titleStmt><title level="a" type="main">T</title></titleStmt>'
          '<profileDesc><abstract><p>Unsegmented abstract.</p></abstract></profileDesc>'
          '</fileDesc></teiHeader>')


def _tei(d, body, back='', header=HEADER):
    path = os.path.join(d, 'x.tei.xml')
    with open(path, 'w', encoding='utf8') as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?><TEI xmlns="http://www.tei-c.org/ns/1.0">'
                 f'{header}<text><body>{body}</body><back>{back}</back></text></TEI>')
    return path


def _json(path):
    return TEI2LossyJSONConverter().convert_tei_file(path, stream=False)


def _texts(doc):
    return [p['text'] for p in doc['body_text']]


class TestSentenceMode:

    BODY = ('<div><head>Intro</head>'
            '<p><s>One.</s> <s>Two.</s> <s>Two-b.</s></p>'
            '<p><s>Three.</s> <s>Four.</s></p></div>')
    BACK = '<div type="availability"><p>Data are available.</p></div>'

    def test_unsegmented_paragraphs_survive_in_sentence_mode(self):
        with tempfile.TemporaryDirectory() as d:
            doc = _json(_tei(d, self.BODY, self.BACK))
        assert doc['level'] == 'sentence'
        assert _texts(doc) == ['One.', 'Two.', 'Two-b.', 'Three.', 'Four.', 'Data are available.']
        assert doc['body_text'][-1]['head_section'] == 'Data Availability'

    def test_unsegmented_abstract_is_not_an_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            doc = _json(_tei(d, self.BODY))
        assert doc['biblio']['abstract'] == [[{
            'id': 0, 'text': 'Unsegmented abstract.', 'coords': [], 'refs': []}]]

    def test_segmented_abstract_is_split_into_sentences(self):
        header = HEADER.replace('<p>Unsegmented abstract.</p>', '<p><s>A one.</s><s>A two.</s></p>')
        with tempfile.TemporaryDirectory() as d:
            doc = _json(_tei(d, self.BODY, header=header))
        assert [[s['text'] for s in p] for p in doc['biblio']['abstract']] == [['A one.', 'A two.']]

    def test_streaming_yields_the_same_passages(self):
        with tempfile.TemporaryDirectory() as d:
            path = _tei(d, self.BODY, self.BACK)
            streamed = [p['text'] for p in TEI2LossyJSONConverter().convert_tei_file(path, stream=True)]
        assert streamed == ['One.', 'Two.', 'Two-b.', 'Three.', 'Four.', 'Data are available.']


class TestNestedDivs:

    BODY = ('<div><head>Outer</head><p>Outer para.</p>'
            '<div><head>Inner</head><p>Inner para.</p></div>'
            '<p>Outer para after.</p></div>')

    def test_every_paragraph_is_emitted_with_its_own_heading(self):
        with tempfile.TemporaryDirectory() as d:
            doc = _json(_tei(d, self.BODY))
        by_text = {p['text']: p.get('head_section') for p in doc['body_text']}
        assert by_text == {'Outer para.': 'Outer', 'Inner para.': 'Inner', 'Outer para after.': 'Outer'}
        assert _texts(doc) == ['Outer para.', 'Inner para.', 'Outer para after.']

    def test_container_div_with_a_heading_only_child(self):
        """A heading-only sub-div lends its heading to the next sibling."""
        body = ('<div type="annex"><div><head>Part A</head></div>'
                '<div><head>Details</head><p>Detail text.</p></div>'
                '<div><p>Untitled text.</p></div></div>')
        with tempfile.TemporaryDirectory() as d:
            doc = _json(_tei(d, body))
        passages = {p['text']: p for p in doc['body_text']}
        assert passages['Detail text.']['head_section'] == 'Details'
        assert passages['Detail text.']['head_paragraph'] == 'Part A'
        assert 'head_paragraph' not in passages['Untitled text.']
        assert 'head_section' not in passages['Untitled text.']

    def test_markdown_prints_each_heading_once(self):
        with tempfile.TemporaryDirectory() as d:
            md = TEI2MarkdownConverter().convert_tei_file(_tei(d, self.BODY))
        assert md.count('### Outer') == 1
        assert md.count('### Inner') == 1
        assert md.index('Outer para.') < md.index('### Inner') < md.index('Inner para.')
        assert 'Outer para after.' in md

    def test_markdown_annex_heading_is_the_divs_own(self):
        back = ('<div type="annex"><div><head>Supplement</head><p>Supp text.</p>'
                '<div><head>Sub</head><p>Sub text.</p></div></div></div>')
        with tempfile.TemporaryDirectory() as d:
            md = TEI2MarkdownConverter().convert_tei_file(_tei(d, '<div><p>Body.</p></div>', back))
        assert md.count('### Supplement') == 1 and md.count('### Sub') == 1
        assert md.index('### Supplement') < md.index('Supp text.') < md.index('### Sub') < md.index('Sub text.')
