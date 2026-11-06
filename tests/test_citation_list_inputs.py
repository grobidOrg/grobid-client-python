"""
processCitationList must not pick up its own error markers as inputs.

Without --output the marker of a failed list (refs_500.txt) is written next
to refs.txt; on the next run it is a .txt in the input directory.
"""
import os
import tempfile
from unittest.mock import Mock, patch

from grobid_client.grobid_client import GrobidClient


def _client():
    with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
        with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
            client = GrobidClient(check_server=False)
    client.logger = Mock()
    return client


def _run(client, input_dir, failing=()):
    sent = []

    def post(url, files=None, data=None, headers=None, timeout=None):
        name = data['citations'][0] if files is None else files['input'][0]
        sent.append(name)
        resp = Mock()
        if name in failing:
            resp.text = 'boom'
            return (resp, 500)
        resp.text = '<TEI>ok</TEI>'
        return (resp, 200)

    with patch.object(GrobidClient, 'post', side_effect=post):
        client.process('processCitationList', input_dir, output=None, force=False)
    return sent


def test_eligibility():
    c = _client()
    assert c._is_eligible_input('refs.txt', 'processCitationList') is True
    assert c._is_eligible_input('refs_500.txt', 'processCitationList') is False
    assert c._is_eligible_input('refs_408.TXT', 'processCitationList') is True    # markers are lower-case
    assert c._is_eligible_input('refs_2020.txt', 'processCitationList') is True   # not a status
    assert c._is_eligible_input('paper.pdf', 'processCitationList') is False


def test_markers_of_a_previous_run_are_not_reprocessed():
    c = _client()
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, 'in')
        os.makedirs(inp)
        for name in ['good.txt', 'bad.txt']:
            with open(os.path.join(inp, name), 'w') as fh:
                fh.write(f'{name} citation\n')
        # first run: bad.txt fails and leaves bad_500.txt next to it
        assert sorted(_run(c, inp, failing={'bad.txt citation'})) == ['bad.txt citation', 'good.txt citation']
        assert os.path.isfile(os.path.join(inp, 'bad_500.txt'))
        # second run: only bad.txt is retried; the marker is not an input
        assert _run(c, inp, failing={'bad.txt citation'}) == ['bad.txt citation']
        assert sorted(os.listdir(inp)) == ['bad.txt', 'bad_500.txt', 'good.grobid.tei.xml', 'good.txt']
