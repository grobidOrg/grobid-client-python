"""A 200 without content is an error, not an empty TEI that blocks every rerun."""
import os
from unittest.mock import Mock, patch

import pytest

from grobid_client.grobid_client import GrobidClient


def _client():
    with patch.object(GrobidClient, '_configure_logging'):
        client = GrobidClient(check_server=False)
    client.logger = Mock()
    return client


@pytest.mark.parametrize('body', ['', '   \n'])
def test_process_pdf_reports_an_empty_answer(body):
    client = _client()
    with patch.object(client, 'post', return_value=(Mock(text=body), 200)):
        name, status, text = client.process_pdf('processFulltextDocument', b'%PDF')
    assert (status, text) == (500, GrobidClient.EMPTY_ANSWER)


def test_process_txt_reports_an_empty_answer(tmp_path):
    refs = tmp_path / 'refs.txt'
    refs.write_text('a ref\n')
    client = _client()
    with patch.object(client, 'post', return_value=(Mock(text=''), 200)):
        _, status, _ = client.process_txt('processCitationList', str(refs), False, False, False, False,
                                          False, False, False)
    assert status == 500


def test_an_empty_answer_leaves_a_marker_and_is_retried(tmp_path):
    src = tmp_path / 'in'
    src.mkdir()
    (src / 'p.pdf').write_bytes(b'%PDF')
    out = tmp_path / 'out'
    answers = iter([(Mock(text=''), 200), (Mock(text='<TEI>ok</TEI>'), 200)])
    client = _client()
    with patch.object(client, 'post', side_effect=lambda *a, **k: next(answers)):
        client.process_paths('processFulltextDocument', [str(src)], output=str(out), force=False)
        assert sorted(os.listdir(out)) == ['p_500.txt']
        client.process_paths('processFulltextDocument', [str(src)], output=str(out), force=False)
    assert sorted(os.listdir(out)) == ['p.grobid.tei.xml']


def test_a_real_answer_is_untouched():
    client = _client()
    with patch.object(client, 'post', return_value=(Mock(text='<TEI/>'), 200)):
        assert client.process_pdf('processFulltextDocument', b'%PDF')[1:] == (200, '<TEI/>')
