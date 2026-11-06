"""
The Markdown header (title, date) must come from the TEI header only, and a
date must be rendered at the precision GROBID gives it.
"""
import os
import tempfile

from grobid_client.format.TEI2Markdown import TEI2MarkdownConverter
from tests.resources import TEST_DATA_PATH


def _tei(d, header, body='<div><p>Body.</p></div>', back=''):
    path = os.path.join(d, 'x.tei.xml')
    with open(path, 'w', encoding='utf8') as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>'
                 '<TEI xmlns="http://www.tei-c.org/ns/1.0">'
                 f'<teiHeader>{header}</teiHeader><text><body>{body}</body><back>{back}</back></text></TEI>')
    return path


BIB = ('<div type="references"><listBibl><biblStruct xml:id="b0"><analytic>'
       '<title level="a" type="main">REF TITLE FROM BIB</title></analytic>'
       '<monogr><imprint><date type="published" when="2016-11-06">2016</date></imprint></monogr>'
       '</biblStruct></listBibl></div>')


def test_title_and_date_come_from_the_header_not_the_bibliography():
    with tempfile.TemporaryDirectory() as d:
        md = TEI2MarkdownConverter().convert_tei_file(_tei(d, '<fileDesc/>', back=BIB))
    assert 'REF TITLE FROM BIB' not in md.split('## References')[0]
    assert 'Published on' not in md


def test_header_title_and_date_are_used():
    header = ('<fileDesc><titleStmt><title level="a" type="main">My Paper</title></titleStmt>'
              '<publicationStmt><date type="published" when="2016-05-03">2016</date></publicationStmt>'
              '</fileDesc>')
    with tempfile.TemporaryDirectory() as d:
        md = TEI2MarkdownConverter().convert_tei_file(_tei(d, header, back=BIB))
    assert md.startswith('# My Paper\n')
    assert 'Published on May 03, 2016' in md


def test_date_precision_is_preserved():
    fmt = TEI2MarkdownConverter._format_iso_date
    assert fmt('2016') == '2016'
    assert fmt('2016-05') == 'May 2016'
    assert fmt('2016-05-03') == 'May 03, 2016'
    assert fmt('2016-05-03T10:00:00') == 'May 03, 2016'
    assert fmt('2016-13') == '2016-13'          # not a real month: left as is
    assert fmt('circa 2016') == 'circa 2016'


def test_year_only_date_does_not_invent_a_day():
    header = ('<fileDesc><publicationStmt><date type="published" when="2016"/></publicationStmt></fileDesc>')
    with tempfile.TemporaryDirectory() as d:
        md = TEI2MarkdownConverter().convert_tei_file(_tei(d, header))
    assert 'Published on 2016\n' in md


def test_date_without_when_falls_back_to_its_text():
    header = ('<fileDesc><publicationStmt><date type="published">Spring 2016</date></publicationStmt></fileDesc>')
    with tempfile.TemporaryDirectory() as d:
        md = TEI2MarkdownConverter().convert_tei_file(_tei(d, header))
    assert 'Published on Spring 2016' in md


def test_sample_document_still_has_its_header_metadata():
    sample = os.path.join(TEST_DATA_PATH, '0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml')
    md = TEI2MarkdownConverter().convert_tei_file(sample)
    assert md.startswith('# ')
    assert 'Published on ' in md
