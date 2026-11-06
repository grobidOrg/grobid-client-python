"""
A 503 is retried in a loop: each attempt closes its stream before the wait,
and a server that stays busy for a long time cannot exhaust descriptors or
the recursion limit.
"""
import io
import os
import tempfile
from unittest.mock import Mock, patch

import requests

from grobid_client.grobid_client import GrobidClient


def _client():
    with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
        with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
            client = GrobidClient(check_server=False)
    client.logger = Mock()
    client.config['sleep_time'] = 0
    return client


def _busy_then_ok(times):
    """A post() that answers 503 `times` times, then 200."""
    calls = []

    def post(url, files=None, data=None, headers=None, timeout=None):
        calls.append(files['input'][1] if files else None)
        resp = Mock()
        if len(calls) <= times:
            resp.text = 'busy'
            return (resp, 503)
        resp.text = '<TEI>ok</TEI>'
        return (resp, 200)

    return post, calls


class TestPdfRetry:

    def test_each_attempt_uses_a_fresh_closed_stream(self):
        client = _client()
        post, calls = _busy_then_ok(3)
        with patch.object(GrobidClient, 'post', side_effect=post):
            with patch('grobid_client.grobid_client.time.sleep') as sleep:
                name, status, text = client.process_pdf('processFulltextDocument', b'%PDF-bytes')
        assert (status, text) == (200, '<TEI>ok</TEI>')
        assert sleep.call_count == 3
        assert len(calls) == 4
        assert len({id(h) for h in calls}) == 4          # a new stream per attempt
        assert all(h.closed for h in calls)              # none left open

    def test_a_file_on_disk_is_reopened_each_time(self):
        client = _client()
        post, calls = _busy_then_ok(2)
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, 'p.pdf')
            with open(pdf, 'wb') as fh:
                fh.write(b'%PDF-disk')
            with patch.object(GrobidClient, 'post', side_effect=post):
                with patch('grobid_client.grobid_client.time.sleep'):
                    _, status, _ = client.process_pdf('processFulltextDocument', pdf)
        assert status == 200
        assert all(h.closed for h in calls)

    def test_a_long_busy_spell_does_not_recurse(self):
        client = _client()
        post, calls = _busy_then_ok(5000)
        with patch.object(GrobidClient, 'post', side_effect=post):
            with patch('grobid_client.grobid_client.time.sleep'):
                _, status, _ = client.process_pdf('processFulltextDocument', b'%PDF')
        assert status == 200
        assert len(calls) == 5001

    def test_timeout_during_a_retry_is_reported_as_408(self):
        client = _client()
        answers = iter([(Mock(text='busy'), 503), requests.exceptions.ReadTimeout('slow')])

        def post(url, files=None, data=None, headers=None, timeout=None):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch.object(GrobidClient, 'post', side_effect=post):
            with patch('grobid_client.grobid_client.time.sleep'):
                _, status, text = client.process_pdf('processFulltextDocument', b'%PDF')
        assert status == 408 and 'timeout' in text.lower()


class TestTxtRetry:

    def _refs(self, d):
        path = os.path.join(d, 'refs.txt')
        with open(path, 'w', encoding='utf8') as fh:
            fh.write('one\ntwo\n')
        return path

    def test_503_then_ok(self):
        client = _client()
        post, calls = _busy_then_ok(2)
        with tempfile.TemporaryDirectory() as d:
            with patch.object(GrobidClient, 'post', side_effect=post):
                with patch('grobid_client.grobid_client.time.sleep') as sleep:
                    name, status, text = client.process_txt(
                        'processCitationList', self._refs(d), False, False, False, False, False, False, False)
        assert (status, text) == (200, '<TEI>ok</TEI>')
        assert sleep.call_count == 2 and len(calls) == 3

    def test_post_carries_the_configured_timeout(self):
        client = _client()
        client.config['timeout'] = 42
        seen = {}

        def post(url, files=None, data=None, headers=None, timeout=None):
            seen['timeout'] = timeout
            return (Mock(text='<TEI/>'), 200)

        with tempfile.TemporaryDirectory() as d:
            with patch.object(GrobidClient, 'post', side_effect=post):
                client.process_txt('processCitationList', self._refs(d), False, False, False, False, False, False, False)
        assert seen['timeout'] == 42

    def test_timeout_is_reported_as_408(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            with patch.object(GrobidClient, 'post', side_effect=requests.exceptions.ReadTimeout('slow')):
                _, status, text = client.process_txt(
                    'processCitationList', self._refs(d), False, False, False, False, False, False, False)
        assert status == 408 and 'timeout' in text.lower()
