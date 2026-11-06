"""
A '~' in the output path (a quoted --output "~/out", or a library call) must
be expanded once, consistently: the TEI, the exists check on rerun, the
error markers and the JSON siblings all have to use the same directory.
"""
import os
import sys
import tempfile
from unittest.mock import Mock, patch

from grobid_client.grobid_client import GrobidClient, main


def _client():
    with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
        with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
            client = GrobidClient(check_server=False)
    client.logger = Mock()
    return client


def _post(sent):
    def post(url, files=None, data=None, headers=None, timeout=None):
        sent.append(files['input'][0])
        return (Mock(text='<TEI>ok</TEI>'), 200)
    return post


def test_library_output_with_tilde(monkeypatch):
    client = _client()
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv('HOME', home)
        monkeypatch.setenv('USERPROFILE', home)
        inp = os.path.join(home, 'in')
        os.makedirs(os.path.join(inp, 'sub'))
        open(os.path.join(inp, 'sub', 'a.pdf'), 'wb').close()
        sent = []
        with patch.object(GrobidClient, 'post', side_effect=_post(sent)):
            with patch('grobid_client.grobid_client.TEI2LossyJSONConverter.convert_tei_file',
                       return_value={'ok': 1}) as conv:
                client.process('processFulltextDocument', inp, output='~/out', json_output=True)
        assert len(sent) == 1
        assert os.path.isfile(os.path.join(home, 'out', 'sub', 'a.grobid.tei.xml'))
        assert os.path.isfile(os.path.join(home, 'out', 'sub', 'a.json'))
        # the converter was given the real path, not one with a literal '~'
        assert conv.call_args.args[0] == os.path.join(home, 'out', 'sub', 'a.grobid.tei.xml')
        assert not os.path.exists(os.path.join(os.getcwd(), '~'))

        # rerun without force: the existing TEI is found and nothing is posted
        sent.clear()
        with patch.object(GrobidClient, 'post', side_effect=_post(sent)):
            client.process('processFulltextDocument', inp, output='~/out', force=False)
        assert sent == []


def test_cli_output_with_tilde(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv('HOME', home)
        monkeypatch.setenv('USERPROFILE', home)
        inp = os.path.join(home, 'in')
        os.makedirs(inp)
        open(os.path.join(inp, 'a.pdf'), 'wb').close()
        with patch.object(GrobidClient, '__init__', lambda self, **kw: setattr(self, 'logger', Mock())):
            with patch.object(GrobidClient, 'process_paths') as pp:
                with patch.object(sys, 'argv', ['grobid_client', '--input', inp, '--output', '~/out',
                                                'processFulltextDocument']):
                    main()
        assert pp.call_args.kwargs['output'] == os.path.join(home, 'out')
        assert os.path.isdir(os.path.join(home, 'out'))
        assert not os.path.exists(os.path.join(os.getcwd(), '~'))


def test_output_file_name_expands_tilde(monkeypatch):
    client = _client()
    monkeypatch.setenv('HOME', '/home/someone')
    monkeypatch.setenv('USERPROFILE', '/home/someone')
    assert client._output_file_name('/in/a.pdf', '/in', '~/out') == os.path.join(
        os.path.expanduser('~'), 'out', 'a.grobid.tei.xml')
