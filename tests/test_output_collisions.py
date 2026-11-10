"""
Two inputs of one run that map to the same output file.

paper.pdf and paper.PDF in one directory, or two archives listed together that
both hold doc.pdf, used to share one output: the second document overwrote the
first (and both were counted as processed), or was skipped as "already
existed". The first input of the run keeps the name, a later one is written
under its own: paper.PDF.grobid.tei.xml, doc.<archive>.grobid.tei.xml.
"""
import os
import zipfile
from unittest.mock import Mock, patch

from grobid_client.grobid_client import GrobidClient


def _client():
    with patch.object(GrobidClient, '_configure_logging'):
        client = GrobidClient(check_server=False)
    client.logger = Mock()
    return client


class _Server:
    """Answers every document with a TEI naming the bytes it was sent."""

    def __init__(self):
        self.posted = []

    def post(self, url, files=None, data=None, headers=None, timeout=None):
        if files:
            content = files['input'][1].read().decode()
        else:
            content = '|'.join(data['citations'])
        self.posted.append(content)
        return Mock(text=f'<TEI>{content}</TEI>'), 200


def _run(client, inputs, out, service='processFulltextDocument', **kwargs):
    server = _Server()
    with patch.object(client, 'post', side_effect=server.post):
        client.process_paths(service, inputs, output=str(out), n=2, force=False, **kwargs)
    return server.posted


def _outputs(out):
    return {os.path.relpath(os.path.join(root, f), out): open(os.path.join(root, f)).read()
            for root, _, files in os.walk(out) for f in files}


def _zip(path, entries):
    with zipfile.ZipFile(path, 'w') as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return str(path)


def test_same_stem_different_extension_case(tmp_path):
    src = tmp_path / 'in'
    src.mkdir()
    (src / 'paper.pdf').write_bytes(b'lower')
    (src / 'paper.PDF').write_bytes(b'upper')
    client = _client()
    _run(client, [str(src)], tmp_path / 'out')
    outputs = _outputs(tmp_path / 'out')
    assert sorted(outputs.values()) == ['<TEI>lower</TEI>', '<TEI>upper</TEI>']
    assert 'paper.grobid.tei.xml' in outputs
    assert len(outputs) == 2
    assert any('would both be written' in c[0][0] for c in client.logger.warning.call_args_list)


def test_two_archives_with_the_same_entry(tmp_path):
    a = _zip(tmp_path / 'batch0.zip', {'doc.pdf': 'zero', 'only0.pdf': 'o0'})
    b = _zip(tmp_path / 'batch1.zip', {'doc.pdf': 'one', 'only1.pdf': 'o1'})
    out = tmp_path / 'out'
    _run(_client(), [a, b], out)
    assert _outputs(out) == {
        'doc.grobid.tei.xml': '<TEI>zero</TEI>',
        'doc.batch1.grobid.tei.xml': '<TEI>one</TEI>',
        'only0.grobid.tei.xml': '<TEI>o0</TEI>',
        'only1.grobid.tei.xml': '<TEI>o1</TEI>',
    }
    # a rerun finds every document done, the renamed one included, and
    # skips them all before reading them
    client = _client()
    with patch.object(client, '_read_archive_member') as read:
        assert _run(client, [a, b], out) == []
    read.assert_not_called()


def test_a_resumed_run_finishes_only_the_missing_renamed_entry(tmp_path):
    a = _zip(tmp_path / 'batch0.zip', {'doc.pdf': 'zero'})
    b = _zip(tmp_path / 'batch1.zip', {'doc.pdf': 'one'})
    out = tmp_path / 'out'
    _run(_client(), [a, b], out)
    os.remove(out / 'doc.batch1.grobid.tei.xml')
    assert _run(_client(), [a, b], out) == ['one']
    assert _outputs(out)['doc.batch1.grobid.tei.xml'] == '<TEI>one</TEI>'
    assert _outputs(out)['doc.grobid.tei.xml'] == '<TEI>zero</TEI>'


def test_two_directories_with_the_same_file(tmp_path):
    for name in ('dirA', 'dirB'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'x.pdf').write_bytes(name.encode())
    out = tmp_path / 'out'
    _run(_client(), [str(tmp_path / 'dirA'), str(tmp_path / 'dirB')], out)
    assert _outputs(out) == {'x.grobid.tei.xml': '<TEI>dirA</TEI>',
                             'x.pdf.grobid.tei.xml': '<TEI>dirB</TEI>'}


def test_citation_lists_from_two_archives(tmp_path):
    # these go through a temporary directory rather than memory
    a = _zip(tmp_path / 'a.zip', {'refs.txt': 'ref a\n'})
    b = _zip(tmp_path / 'b.zip', {'refs.txt': 'ref b\n'})
    out = tmp_path / 'out'
    _run(_client(), [a, b], out, service='processCitationList')
    assert _outputs(out) == {'refs.grobid.tei.xml': '<TEI>ref a</TEI>',
                             'refs.b.grobid.tei.xml': '<TEI>ref b</TEI>'}


def test_a_taken_new_name_gets_a_number(tmp_path):
    for i, name in enumerate(('x.zip', 'sub/x.zip', 'other/x.zip')):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        _zip(tmp_path / name, {'doc.pdf': str(i)})
    out = tmp_path / 'out'
    _run(_client(), [str(tmp_path / 'x.zip'), str(tmp_path / 'sub/x.zip'), str(tmp_path / 'other/x.zip')], out)
    assert _outputs(out) == {'doc.grobid.tei.xml': '<TEI>0</TEI>',
                             'doc.x.grobid.tei.xml': '<TEI>1</TEI>',
                             'doc.x-2.grobid.tei.xml': '<TEI>2</TEI>'}


def test_inputs_that_do_not_collide_keep_their_names(tmp_path):
    (tmp_path / 'in' / 'sub').mkdir(parents=True)
    (tmp_path / 'in' / 'a.pdf').write_bytes(b'a')
    (tmp_path / 'in' / 'sub' / 'a.pdf').write_bytes(b'sub a')
    out = tmp_path / 'out'
    _run(_client(), [str(tmp_path / 'in')], out)
    assert sorted(_outputs(out)) == ['a.grobid.tei.xml', os.path.join('sub', 'a.grobid.tei.xml')]
