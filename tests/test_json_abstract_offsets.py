"""
Abstract passages must carry the text their reference offsets index into.

Reference offsets are computed on whitespace-normalised text, as for body
passages, but the abstract's text was the raw paragraph text. An abstract
with a line break or a double space before a citation got offsets that
point a few characters off (seen on GROBID 0.9.2 output with the sdo/ietf
flavor). At sentence level the abstract is a list of lists, which
validate_json_refs skipped, so this was never reported.
"""
import io
import json

import pytest

from grobid_client.format.TEI2LossyJSON import TEI2LossyJSONConverter
from grobid_client.format.validate_json_refs import JSONReferenceValidator

PARAGRAPH = ('<p>Networks of depth sixteen [41]  to\n      thirty <ref type="bibr" target="#b15">[16]</ref>.\n'
             '   Other tasks <ref type="bibr" target="#b7">[8,</ref><ref type="bibr" target="#b11">12]</ref> too.</p>')
SENTENCES = ('<p><s>Networks of depth sixteen [41]  to\n thirty <ref type="bibr" target="#b15">[16]</ref>.</s>'
             '<s>  Other tasks <ref type="bibr" target="#b7">[8]</ref>\n too.</s></p>')


def _convert(abstract, body='<div><p>x</p></div>'):
    tei = ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><profileDesc>'
           f'<abstract><div>{abstract}</div></abstract></profileDesc></teiHeader>'
           f'<text><body>{body}</body></text></TEI>')
    return TEI2LossyJSONConverter().convert_tei_file(io.StringIO(tei))


def _abstract_passages(document):
    for item in document['biblio']['abstract']:
        yield from (item if isinstance(item, list) else [item])


@pytest.mark.parametrize("abstract, body", [
    (PARAGRAPH, '<div><p>x</p></div>'),
    # more <s> than <p> in the document: sentence level
    (SENTENCES, '<div><p><s>a</s><s>b</s><s>c</s></p></div>'),
])
def test_abstract_ref_offsets_point_at_the_ref_text(abstract, body):
    document = _convert(abstract, body)
    passages = list(_abstract_passages(document))
    refs = [(p, r) for p in passages for r in p['refs']]
    assert refs
    for passage, ref in refs:
        assert passage['text'] == ' '.join(passage['text'].split())
        assert passage['text'][ref['offset_start']:ref['offset_end']] == ref['text']


def test_validator_checks_sentence_level_abstracts(tmp_path):
    document = _convert(SENTENCES, '<div><p><s>a</s><s>b</s><s>c</s></p></div>')
    assert isinstance(document['biblio']['abstract'][0], list)
    ref_count = sum(len(p['refs']) for p in _abstract_passages(document))
    good = tmp_path / 'good.json'
    good.write_text(json.dumps(document))
    results = JSONReferenceValidator().validate_directory(str(good))
    assert results['total_refs'] == ref_count and results['invalid_refs'] == 0

    document['biblio']['abstract'][0][0]['refs'][0]['offset_start'] += 1
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps(document))
    results = JSONReferenceValidator().validate_directory(str(bad))
    assert results['invalid_refs'] == 1
    assert 'biblio.abstract[0][0].refs[0]' in results['file_details'][0]['errors'][0]
