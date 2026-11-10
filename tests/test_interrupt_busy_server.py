"""
Ctrl-C while the server is busy.

A document the server answers 503 to is re-posted after ``sleep_time`` for as
long as the server stays busy. That retry ran in the pool's worker threads, which
the interrupted main thread then waited for: the client printed "Interrupted"
and kept posting, so a permanently busy server needed a second Ctrl-C (or a kill).
"""
import http.server
import json
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import Mock, patch

import pytest

from grobid_client.grobid_client import GrobidClient


def _client(sleep_time=60):
    with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
        with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
            client = GrobidClient(check_server=False)
    client.logger = Mock()
    client.config['sleep_time'] = sleep_time
    return client


def _always_busy():
    calls = []

    def post(url, files=None, data=None, headers=None, timeout=None):
        calls.append(url)
        return Mock(text='busy'), 503

    return post, calls


def test_a_stopped_client_does_not_wait_for_a_busy_server():
    client = _client()
    client._stopping.set()
    post, calls = _always_busy()
    with patch.object(GrobidClient, 'post', side_effect=post):
        start = time.time()
        name, status, text = client.process_pdf('processFulltextDocument', b'%PDF')
    assert time.time() - start < 5
    assert (status, text) == (503, GrobidClient.BUSY_SERVER_INTERRUPTED)
    assert len(calls) == 1


def test_interrupting_a_batch_ends_the_busy_retries(tmp_path):
    """KeyboardInterrupt in the main thread must not leave workers retrying."""
    client = _client(sleep_time=60)
    post, calls = _always_busy()
    pdf = tmp_path / 'busy.pdf'
    pdf.write_bytes(b'%PDF')

    def interrupted(futures):
        deadline = time.time() + 10
        while not calls and time.time() < deadline:
            time.sleep(0.01)
        raise KeyboardInterrupt

    with patch.object(GrobidClient, 'post', side_effect=post):
        with patch('grobid_client.grobid_client.concurrent.futures.as_completed', side_effect=interrupted):
            start = time.time()
            with pytest.raises(KeyboardInterrupt):
                client.process_batch('processFulltextDocument', [str(pdf)], str(tmp_path), str(tmp_path / 'out'),
                                     2, False, False, False, False, False, False, False, False)
    assert time.time() - start < 10      # not the 60 s sleep_time
    assert len(calls) == 1
    assert not (tmp_path / 'out').exists()  # an interrupted document leaves no error marker


def test_a_new_batch_after_an_interrupt_retries_again(tmp_path):
    client = _client(sleep_time=0)
    client._stopping.set()                 # left over from an interrupted batch
    answers = iter([503, 200])

    def post(url, files=None, data=None, headers=None, timeout=None):
        return Mock(text='<TEI/>'), next(answers)

    pdf = tmp_path / 'p.pdf'
    pdf.write_bytes(b'%PDF')
    with patch.object(GrobidClient, 'post', side_effect=post):
        processed, errors, skipped = client.process_batch(
            'processFulltextDocument', [str(pdf)], str(tmp_path), str(tmp_path / 'out'),
            1, False, False, False, False, False, False, False, False)
    assert (processed, errors) == (1, 0)


class _Busy(http.server.BaseHTTPRequestHandler):
    posts = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '4')
        self.end_headers()
        self.wfile.write(b'true')

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        type(self).posts += 1
        self.send_response(503)
        self.send_header('Content-Length', '4')
        self.end_headers()
        self.wfile.write(b'busy')


@pytest.mark.skipif(sys.platform == 'win32', reason='needs POSIX signals')
def test_cli_stops_on_the_first_ctrl_c_against_a_busy_server(tmp_path):
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Busy)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f'http://127.0.0.1:{httpd.server_address[1]}'
        (tmp_path / 'in').mkdir()
        for name in ('a.pdf', 'b.pdf', 'c.pdf'):
            (tmp_path / 'in' / name).write_bytes(b'%PDF')
        config = tmp_path / 'config.json'
        config.write_text(json.dumps({'grobid_server': url, 'sleep_time': 30}))
        proc = subprocess.Popen(
            [sys.executable, '-c', 'from grobid_client.grobid_client import main; main()',
             '--config', str(config), '--input', str(tmp_path / 'in'), '--output', str(tmp_path / 'out'),
             '--n', '2', 'processFulltextDocument'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(tmp_path))
        deadline = time.time() + 30
        while _Busy.posts < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert _Busy.posts >= 2, 'the client never reached the server'
        proc.send_signal(signal.SIGINT)
        start = time.time()
        try:
            log, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail('still retrying the busy server 15 s after Ctrl-C')
        assert proc.returncode == 130, log
        assert time.time() - start < 15
        assert 'run the same command again' in log
        # the documents interrupted while waiting are not recorded as failures
        assert not list((tmp_path / 'out').glob('*_503.txt'))
    finally:
        httpd.shutdown()
        httpd.server_close()
