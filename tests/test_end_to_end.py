"""
End to end: the grobid_client command against a local stand-in GROBID server.

Every other client test mocks the HTTP layer. These run the real command
line in a subprocess against a small HTTP server that answers like GROBID
(isalive, then TEI for every PDF, a 500 for a PDF that asks for one), and
check what lands on disk: the input tree is mirrored, JSON and Markdown
sit next to the TEI, failures leave an error marker, archives behave like
directories, and a second run skips what is done.
"""
import http.server
import json
import os
import subprocess
import sys
import tarfile
import threading
import zipfile

import pytest

from tests.resources import TEST_DATA_PATH

TEI = open(os.path.join(TEST_DATA_PATH, '0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml'), 'rb').read()
PDF = b'%PDF-1.4 stand-in'
BROKEN = b'%PDF-1.4 FAIL500'


class _FakeGrobid(http.server.BaseHTTPRequestHandler):
    posts = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'true')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        type(self).posts.append(self.path)
        if b'FAIL500' in body:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'boom')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/xml')
        self.end_headers()
        self.wfile.write(TEI)


@pytest.fixture(scope='module')
def server():
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _FakeGrobid)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def pdf_tree(tmp_path):
    root = tmp_path / 'in'
    for rel, content in {'top.pdf': PDF, 'a/x.pdf': PDF, 'a/deep/y.pdf': PDF,
                         'b/x.pdf': PDF, 'b/broken.pdf': BROKEN, 'b/notes.txt': b'skip me'}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def run_client(server, *args, cwd):
    _FakeGrobid.posts = []
    return subprocess.run(
        [sys.executable, '-c', 'from grobid_client.grobid_client import main; main()',
         '--server', server, *args],
        capture_output=True, text=True, cwd=cwd, timeout=120)


def files_under(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob('*') if p.is_file())


def test_directory_tree_is_mirrored_with_json_and_markdown(server, pdf_tree, tmp_path):
    out = tmp_path / 'out'
    result = run_client(server, '--input', str(pdf_tree), '--output', str(out), '--json', '--markdown',
                        '--n', '4', 'processFulltextDocument', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    expected = []
    for stem in ['top', 'a/x', 'a/deep/y', 'b/x']:
        expected += [f'{stem}.grobid.tei.xml', f'{stem}.json', f'{stem}.md']
    assert files_under(out) == sorted(expected + ['b/broken_500.txt'])
    assert (out / 'a' / 'x.grobid.tei.xml').read_bytes() == TEI
    assert json.loads((out / 'a' / 'deep' / 'y.json').read_text())['body_text']
    assert (out / 'b' / 'x.md').read_text().startswith('# Multi-contact')
    assert all(p == '/api/processFulltextDocument' for p in _FakeGrobid.posts)


def test_second_run_skips_done_files_and_restores_missing_json(server, pdf_tree, tmp_path):
    out = tmp_path / 'out'
    run_client(server, '--input', str(pdf_tree), '--output', str(out), '--json',
               'processFulltextDocument', cwd=tmp_path)
    (out / 'a' / 'deep' / 'y.json').unlink()

    result = run_client(server, '--input', str(pdf_tree), '--output', str(out), '--json',
                        'processFulltextDocument', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    # only the document that failed before is sent again
    assert len(_FakeGrobid.posts) == 1
    assert (out / 'a' / 'deep' / 'y.json').exists()

    result = run_client(server, '--input', str(pdf_tree), '--output', str(out), '--skip_errors',
                        'processFulltextDocument', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert _FakeGrobid.posts == []


def test_force_reprocesses_everything(server, pdf_tree, tmp_path):
    out = tmp_path / 'out'
    run_client(server, '--input', str(pdf_tree), '--output', str(out), 'processFulltextDocument', cwd=tmp_path)
    run_client(server, '--input', str(pdf_tree), '--output', str(out), '--force',
               'processFulltextDocument', cwd=tmp_path)
    assert len(_FakeGrobid.posts) == 5


@pytest.mark.parametrize('kind', ['zip', 'tar.gz'])
def test_archive_is_processed_like_a_directory(server, pdf_tree, tmp_path, kind):
    archive = tmp_path / f'in.{kind}'
    if kind == 'zip':
        with zipfile.ZipFile(archive, 'w') as zf:
            for path in pdf_tree.rglob('*'):
                if path.is_file():
                    zf.write(path, path.relative_to(pdf_tree))
    else:
        with tarfile.open(archive, 'w:gz') as tf:
            tf.add(pdf_tree, arcname='.')
    out = tmp_path / 'out'
    result = run_client(server, '--input', str(archive), '--output', str(out), 'processFulltextDocument',
                        cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert files_under(out) == sorted(['top.grobid.tei.xml', 'a/x.grobid.tei.xml', 'a/deep/y.grobid.tei.xml',
                                       'b/x.grobid.tei.xml', 'b/broken_500.txt'])


def test_input_list_keeps_the_relative_layout(server, pdf_tree, tmp_path):
    listing = tmp_path / 'list.txt'
    listing.write_text('in/a/x.pdf\nin/b/x.pdf\n')
    out = tmp_path / 'out'
    result = run_client(server, '--input-list', str(listing), '--output', str(out), 'processFulltextDocument',
                        cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert files_under(out) == ['a/x.grobid.tei.xml', 'b/x.grobid.tei.xml']


def test_without_output_results_sit_next_to_the_inputs(server, pdf_tree, tmp_path):
    result = run_client(server, '--input', str(pdf_tree / 'a'), 'processFulltextDocument', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (pdf_tree / 'a' / 'x.grobid.tei.xml').exists()
    assert (pdf_tree / 'a' / 'deep' / 'y.grobid.tei.xml').exists()
