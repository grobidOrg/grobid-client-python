"""
Tests for the command line entry point and the per-document outputs.

Covers the parts that the rest of the suite leaves out: ``main()`` (argument
parsing and what reaches ``process_paths``), the JSON/Markdown files written
next to each TEI, error markers inside subdirectories, the chunking of a
directory into batches of ``queue_size``, citation-list processing and the
logging configuration.
"""
import json
import logging
import os
import sys
import tempfile
from unittest.mock import Mock, patch

import pytest
import requests

from grobid_client.grobid_client import GrobidClient, ServerUnavailableException, main

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_TEI = os.path.join(HERE, 'resources', '0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml')


def _client(queue_size=50):
    with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
        with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
            client = GrobidClient(check_server=False)
    client.logger = Mock()
    client.config['queue_size'] = queue_size
    return client


def _touch(*parts, content=b''):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(content)
    return path


def _outputs(output_dir, suffix):
    """Files ending in suffix under output_dir, relative, posix separators."""
    found = []
    for root, _, files in os.walk(output_dir):
        for f in files:
            if f.endswith(suffix):
                rel = os.path.relpath(os.path.join(root, f), output_dir)
                found.append(rel.replace(os.sep, '/'))
    return sorted(found)


def _fake_post(failing=(), text='<TEI>ok</TEI>'):
    """A GrobidClient.post replacement; records the names it was sent."""
    sent = []

    def post(url, files=None, data=None, headers=None, timeout=None):
        name = files['input'][0] if files else None
        sent.append(name)
        resp = Mock()
        if name is not None and os.path.basename(name) in failing:
            resp.text = 'boom'
            return (resp, 500)
        resp.text = text
        return (resp, 200)

    return post, sent


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    """The CLI must hand every option through to process_paths unchanged."""

    def _run_main(self, argv, process_paths=None, init_side_effect=None):
        """Run main() with argv; return (process_paths mock, constructor kwargs)."""
        constructed = {}

        def fake_init(self, **kwargs):
            constructed.update(kwargs)
            if init_side_effect is not None:
                raise init_side_effect
            self.logger = Mock()

        with patch.object(GrobidClient, '__init__', fake_init):
            with patch.object(GrobidClient, 'process_paths', process_paths or Mock()) as pp:
                with patch.object(sys, 'argv', ['grobid_client'] + argv):
                    main()
        return pp, constructed

    def test_all_flags_are_forwarded(self):
        with tempfile.TemporaryDirectory() as d:
            pp, constructed = self._run_main([
                '--input', d, '--output', os.path.join(d, 'out'),
                '--server', 'http://srv:8070', '--config', 'my.json', '--n', '8',
                '--generate_ids', '--consolidate_header', '--consolidate_citations',
                '--include_raw_citations', '--include_raw_affiliations', '--force',
                '--skip_errors', '--tei_coordinates', '--segment_sentences', '--verbose',
                '--flavor', 'article/light', '--json', '--markdown',
                'processFulltextDocument',
            ])
            pp.assert_called_once()
            args, kwargs = pp.call_args
            assert args == ('processFulltextDocument', [d])
            assert kwargs == {
                'output': os.path.join(d, 'out'), 'n': 8,
                'generate_ids': True, 'consolidate_header': True,
                'consolidate_citations': True, 'include_raw_citations': True,
                'include_raw_affiliations': True, 'tei_coordinates': True,
                'segment_sentences': True, 'force': True, 'verbose': True,
                'flavor': 'article/light', 'json_output': True,
                'markdown_output': True, 'skip_errors': True,
            }
            assert constructed == {'config_path': 'my.json', 'verbose': True,
                                   'grobid_server': 'http://srv:8070'}

    def test_defaults_when_no_flag_is_given(self):
        with tempfile.TemporaryDirectory() as d:
            pp, constructed = self._run_main(['--input', d, 'processHeaderDocument'])
            _, kwargs = pp.call_args
            assert kwargs['n'] == 10
            assert kwargs['output'] is None
            assert kwargs['flavor'] is None
            for flag in ('generate_ids', 'consolidate_header', 'consolidate_citations',
                         'include_raw_citations', 'include_raw_affiliations',
                         'tei_coordinates', 'segment_sentences', 'force', 'verbose',
                         'json_output', 'markdown_output', 'skip_errors'):
                assert kwargs[flag] is False, flag
            # the server is only overridden when given on the command line
            assert constructed == {'config_path': None, 'verbose': False}

    def test_camel_case_aliases(self):
        """--teiCoordinates, --generateIDs, --segmentSentences, --skip-errors."""
        with tempfile.TemporaryDirectory() as d:
            pp, _ = self._run_main([
                '--input', d, '--teiCoordinates', '--generateIDs', '--segmentSentences',
                '--skip-errors', 'processFulltextDocument',
            ])
            _, kwargs = pp.call_args
            assert kwargs['tei_coordinates'] is True
            assert kwargs['generate_ids'] is True
            assert kwargs['segment_sentences'] is True
            assert kwargs['skip_errors'] is True

    def test_invalid_n_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            pp, _ = self._run_main(['--input', d, '--n', 'many', 'processFulltextDocument'])
            assert pp.call_args.kwargs['n'] == 10

    def test_output_directory_is_created(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'new', 'deeper')
            self._run_main(['--input', d, '--output', out, 'processFulltextDocument'])
            assert os.path.isdir(out)

    def test_input_list_is_parsed_and_merged_with_input(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = os.path.join(d, 'paths.txt')
            with open(manifest, 'w', encoding='utf-8') as fh:
                fh.write('# a comment\n\n  /data/a.pdf  \n/data/b/*.zip\n\n# trailing\ns3://bucket/c.pdf\n')
            pp, _ = self._run_main(['--input', d, '--input-list', manifest, 'processFulltextDocument'])
            args, _ = pp.call_args
            assert args[1] == [d, '/data/a.pdf', '/data/b/*.zip', 's3://bucket/c.pdf']

    def test_input_list_alone_is_enough(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = os.path.join(d, 'paths.txt')
            with open(manifest, 'w', encoding='utf-8') as fh:
                fh.write('/data/a.pdf\n')
            pp, _ = self._run_main(['--input-list', manifest, 'processFulltextDocument'])
            assert pp.call_args.args[1] == ['/data/a.pdf']

    def test_no_input_exits_with_error(self):
        with pytest.raises(SystemExit) as exc:
            self._run_main(['processFulltextDocument'])
        assert exc.value.code == 1

    def test_missing_input_list_exits_with_error(self):
        with pytest.raises(SystemExit) as exc:
            self._run_main(['--input-list', '/nope/paths.txt', 'processFulltextDocument'])
        assert exc.value.code == 1

    def test_unknown_service_is_rejected_by_argparse(self):
        with pytest.raises(SystemExit) as exc:
            self._run_main(['--input', '.', 'processEverything'])
        assert exc.value.code == 2

    def test_service_is_required(self):
        with pytest.raises(SystemExit) as exc:
            self._run_main(['--input', '.'])
        assert exc.value.code == 2

    def test_server_unavailable_exits_with_error(self):
        with pytest.raises(SystemExit) as exc:
            self._run_main(['--input', '.', 'processFulltextDocument'],
                           init_side_effect=ServerUnavailableException('down'))
        assert exc.value.code == 1

    def test_processing_failure_exits_with_error(self):
        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(SystemExit) as exc:
                self._run_main(['--input', d, 'processFulltextDocument'],
                               process_paths=Mock(side_effect=RuntimeError('kaboom')))
            assert exc.value.code == 1

    def test_unwritable_output_directory_exits_with_error(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = _touch(d, 'file')          # a file where a directory is wanted
            with pytest.raises(SystemExit) as exc:
                self._run_main(['--input', d, '--output', os.path.join(blocker, 'out'),
                                'processFulltextDocument'])
            assert exc.value.code == 1


# ---------------------------------------------------------------------------
# JSON / Markdown companions
# ---------------------------------------------------------------------------

class TestJsonAndMarkdownOutputs:
    """--json / --markdown write a sibling next to every TEI, nested dirs included."""

    def _run(self, client, input_dir, output, **kwargs):
        post, sent = _fake_post(failing=kwargs.pop('failing', ()), text=kwargs.pop('text', '<TEI>ok</TEI>'))
        kwargs.setdefault('force', True)
        with patch.object(GrobidClient, 'post', side_effect=post):
            client.process('processFulltextDocument', input_dir, output=output, **kwargs)
        return sent

    def test_siblings_follow_the_tei_into_subdirectories(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', '2023', 'p.pdf')
            _touch(d, 'in', 'q.pdf')
            out = os.path.join(d, 'out')
            with patch('grobid_client.grobid_client.TEI2LossyJSONConverter.convert_tei_file',
                       return_value={'title': 'T'}):
                with patch('grobid_client.format.TEI2Markdown.TEI2MarkdownConverter.convert_tei_file',
                           return_value='# T'):
                    self._run(client, os.path.join(d, 'in'), out, json_output=True, markdown_output=True)
            assert _outputs(out, '.grobid.tei.xml') == ['2023/p.grobid.tei.xml', 'q.grobid.tei.xml']
            assert _outputs(out, '.json') == ['2023/p.json', 'q.json']
            assert _outputs(out, '.md') == ['2023/p.md', 'q.md']
            with open(os.path.join(out, '2023', 'p.json'), encoding='utf8') as fh:
                assert json.load(fh) == {'title': 'T'}
            with open(os.path.join(out, '2023', 'p.md'), encoding='utf8') as fh:
                assert fh.read() == '# T'

    def test_missing_sibling_is_generated_for_a_skipped_document(self):
        """A rerun with --json on an existing TEI converts it without reposting."""
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'sub', 'p.pdf')
            out = os.path.join(d, 'out')
            sent = self._run(client, os.path.join(d, 'in'), out)
            assert sent and _outputs(out, '.json') == []

            with patch('grobid_client.grobid_client.TEI2LossyJSONConverter.convert_tei_file',
                       return_value={'k': 1}) as conv:
                sent = self._run(client, os.path.join(d, 'in'), out, force=False, json_output=True)
            assert sent == []                                   # TEI existed: not reposted
            conv.assert_called_once()
            assert _outputs(out, '.json') == ['sub/p.json']

    def test_existing_sibling_is_not_regenerated_for_a_skipped_document(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'p.pdf')
            out = os.path.join(d, 'out')
            _touch(out, 'p.grobid.tei.xml', content=b'<TEI/>')
            _touch(out, 'p.json', content=b'{}')
            with patch('grobid_client.grobid_client.TEI2LossyJSONConverter.convert_tei_file') as conv:
                sent = self._run(client, os.path.join(d, 'in'), out, force=False, json_output=True)
            assert sent == []
            conv.assert_not_called()

    def test_failed_conversion_keeps_the_tei_and_counts_as_processed(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'p.pdf')
            out = os.path.join(d, 'out')
            with patch('grobid_client.grobid_client.TEI2LossyJSONConverter.convert_tei_file',
                       side_effect=RuntimeError('bad tei')):
                with patch.object(GrobidClient, '_print_processing_summary') as summary:
                    self._run(client, os.path.join(d, 'in'), out, json_output=True)
            assert _outputs(out, '.grobid.tei.xml') == ['p.grobid.tei.xml']
            assert _outputs(out, '.json') == []
            processed, errors, skipped = summary.call_args.args[:3]
            assert (processed, errors, skipped) == (1, 0, 0)
            client.logger.error.assert_called()
            assert 'JSON' in client.logger.error.call_args[0][0]

    def test_converter_returning_nothing_only_warns(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'p.pdf')
            out = os.path.join(d, 'out')
            with patch('grobid_client.format.TEI2Markdown.TEI2MarkdownConverter.convert_tei_file',
                       return_value=None):
                self._run(client, os.path.join(d, 'in'), out, markdown_output=True)
            assert _outputs(out, '.md') == []
            client.logger.warning.assert_called()

    def test_real_converters_on_a_grobid_response(self):
        """End to end through the actual converters, using a real TEI as the response."""
        client = _client()
        with open(SAMPLE_TEI, encoding='utf8') as fh:
            tei = fh.read()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'conf', 'paper.pdf')
            out = os.path.join(d, 'out')
            self._run(client, os.path.join(d, 'in'), out, text=tei, json_output=True, markdown_output=True)
            assert _outputs(out, '.json') == ['conf/paper.json']
            assert _outputs(out, '.md') == ['conf/paper.md']
            with open(os.path.join(out, 'conf', 'paper.json'), encoding='utf8') as fh:
                data = json.load(fh)
            assert isinstance(data, dict) and data
            with open(os.path.join(out, 'conf', 'paper.md'), encoding='utf8') as fh:
                assert fh.read().strip()
            # the TEI itself is the server response, byte for byte
            with open(os.path.join(out, 'conf', 'paper.grobid.tei.xml'), encoding='utf8') as fh:
                assert fh.read() == tei


# ---------------------------------------------------------------------------
# Error markers and resume inside subdirectories
# ---------------------------------------------------------------------------

class TestNestedErrorMarkers:

    def _run(self, client, input_dir, output, failing=(), **kwargs):
        post, sent = _fake_post(failing=failing)
        with patch.object(GrobidClient, 'post', side_effect=post):
            client.process('processFulltextDocument', input_dir, output=output, **kwargs)
        return [os.path.relpath(s, input_dir).replace(os.sep, '/') for s in sent]

    def test_error_marker_lands_next_to_the_expected_tei(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'a', 'bad.pdf')
            _touch(d, 'in', 'b', 'bad.pdf')       # same basename, succeeds
            out = os.path.join(d, 'out')
            with patch.object(GrobidClient, '_print_processing_summary') as summary:
                self._run(client, os.path.join(d, 'in'), out, failing={'bad.pdf'}, force=True)
            # both fail (same basename), each marker in its own directory
            assert _outputs(out, '.txt') == ['a/bad_500.txt', 'b/bad_500.txt']
            assert _outputs(out, '.grobid.tei.xml') == []
            assert summary.call_args.args[:3] == (0, 2, 0)
            with open(os.path.join(out, 'a', 'bad_500.txt'), encoding='utf8') as fh:
                assert fh.read() == 'boom'

    def test_skip_errors_honours_nested_markers(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'a', 'bad.pdf')
            _touch(d, 'in', 'a', 'ok.pdf')
            out = os.path.join(d, 'out')
            self._run(client, os.path.join(d, 'in'), out, failing={'bad.pdf'}, force=True)
            sent = self._run(client, os.path.join(d, 'in'), out, failing={'bad.pdf'},
                             force=False, skip_errors=True)
            assert sent == []
            # without --skip_errors the failed one is retried
            sent = self._run(client, os.path.join(d, 'in'), out, failing={'bad.pdf'}, force=False)
            assert sent == ['a/bad.pdf']

    def test_marker_is_removed_once_the_document_succeeds(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'a', 'doc.pdf')
            out = os.path.join(d, 'out')
            self._run(client, os.path.join(d, 'in'), out, failing={'doc.pdf'}, force=True)
            assert _outputs(out, '.txt') == ['a/doc_500.txt']
            self._run(client, os.path.join(d, 'in'), out, force=False)
            assert _outputs(out, '.txt') == []
            assert _outputs(out, '.grobid.tei.xml') == ['a/doc.grobid.tei.xml']


# ---------------------------------------------------------------------------
# Batching of a directory
# ---------------------------------------------------------------------------

class TestDirectoryBatching:

    def test_files_are_sent_in_chunks_of_queue_size(self):
        client = _client(queue_size=2)
        with tempfile.TemporaryDirectory() as d:
            for name in ['a', 'b', 'c', 'd', 'e']:
                _touch(d, 'in', f'{name}.pdf')
            with patch.object(GrobidClient, 'process_batch', return_value=(1, 0, 0)) as pb:
                client.process('processFulltextDocument', os.path.join(d, 'in'),
                               output=os.path.join(d, 'out'))
            sizes = [len(call.args[1]) for call in pb.call_args_list]
            assert sizes == [2, 2, 1]
            # every batch is computed against the directory given as input
            assert {call.args[2] for call in pb.call_args_list} == {os.path.join(d, 'in')}

    def test_counts_are_aggregated_across_batches(self):
        client = _client(queue_size=2)
        with tempfile.TemporaryDirectory() as d:
            for name in ['a', 'b', 'c']:
                _touch(d, 'in', f'{name}.pdf')
            with patch.object(GrobidClient, 'process_batch', side_effect=[(1, 1, 0), (0, 0, 1)]):
                with patch.object(GrobidClient, '_print_processing_summary') as summary:
                    client.process('processFulltextDocument', os.path.join(d, 'in'),
                                   output=os.path.join(d, 'out'))
            processed, errors, skipped, total = summary.call_args.args[:4]
            assert (processed, errors, skipped, total) == (1, 1, 1, 3)

    def test_queue_size_defaults_for_local_directories(self):
        client = _client(queue_size=None)
        assert client._effective_queue_size(10, local_files=True) == GrobidClient.LOCAL_DIR_QUEUE_SIZE
        assert client._effective_queue_size(10) == 12

    def test_a_single_file_input_is_processed(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            pdf = _touch(d, 'in', 'only.pdf')
            out = os.path.join(d, 'out')
            post, sent = _fake_post()
            with patch.object(GrobidClient, 'post', side_effect=post):
                client.process('processFulltextDocument', pdf, output=out)
            assert sent == [pdf]
            assert _outputs(out, '.grobid.tei.xml') == ['only.grobid.tei.xml']

    def test_non_pdf_files_are_ignored(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'a.pdf')
            _touch(d, 'in', 'A.PDF')
            _touch(d, 'in', 'notes.txt')
            _touch(d, 'in', '._a.pdf')            # macOS resource fork
            _touch(d, 'in', 'a.pdf.bak')
            post, sent = _fake_post()
            with patch.object(GrobidClient, 'post', side_effect=post):
                client.process('processFulltextDocument', os.path.join(d, 'in'), output=os.path.join(d, 'out'))
            assert sorted(os.path.basename(s) for s in sent) == ['A.PDF', 'a.pdf']


# ---------------------------------------------------------------------------
# Citation lists
# ---------------------------------------------------------------------------

class TestCitationList:

    def test_txt_lines_are_posted_as_citations(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            _touch(d, 'in', 'refs.txt', content='Smith 2020. A paper.\nDoe 2021. Another.\n\n'.encode('utf8'))
            _touch(d, 'in', 'paper.pdf')           # not a citation list: ignored
            out = os.path.join(d, 'out')
            posted = []

            def post(url, files=None, data=None, headers=None, timeout=None):
                posted.append((url, files, data))
                resp = Mock()
                resp.text = '<TEI>refs</TEI>'
                return (resp, 200)

            with patch.object(GrobidClient, 'post', side_effect=post):
                client.process('processCitationList', os.path.join(d, 'in'), output=out,
                               consolidate_citations=True, include_raw_citations=True)
            txt_calls = [c for c in posted if c[1] is None]
            assert len(txt_calls) == 1
            url, _, data = txt_calls[0]
            assert url.endswith('/api/processCitationList')
            assert data['citations'] == ['Smith 2020. A paper.', 'Doe 2021. Another.', '']
            assert data['consolidateCitations'] == '1'
            assert data['includeRawCitations'] == '1'
            assert len(posted) == 1
            assert _outputs(out, '.grobid.tei.xml') == ['refs.grobid.tei.xml']

    def test_txt_files_are_not_eligible_for_other_services(self):
        client = _client()
        assert client._is_eligible_input('refs.txt', 'processFulltextDocument') is False
        assert client._is_eligible_input('paper.pdf', 'processCitationList') is False
        assert client._is_eligible_input('refs.TXT', 'processCitationList') is True
        assert client._is_eligible_input('patent.xml', 'processCitationPatentST36') is True
        assert client._is_eligible_input('patent.xml', 'processFulltextDocument') is False

    def test_unreadable_txt_is_reported_as_error(self):
        client = _client()
        name, status, text = client.process_txt('processCitationList', '/nope/refs.txt',
                                                False, False, False, False, False, False, False)
        assert (name, status) == ('/nope/refs.txt', 500)
        assert 'Failed to read' in text


# ---------------------------------------------------------------------------
# process_pdf error mapping
# ---------------------------------------------------------------------------

class TestProcessPdfErrors:

    def _pdf(self, d):
        return _touch(d, 'p.pdf', content=b'%PDF-1.4')

    def test_read_timeout_maps_to_408(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            with patch.object(GrobidClient, 'post', side_effect=requests.exceptions.ReadTimeout('slow')):
                name, status, text = client.process_pdf('processFulltextDocument', self._pdf(d))
            assert status == 408 and 'timeout' in text.lower()

    def test_connection_error_maps_to_500(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            with patch.object(GrobidClient, 'post', side_effect=requests.exceptions.ConnectionError('refused')):
                name, status, text = client.process_pdf('processFulltextDocument', self._pdf(d))
            assert status == 500 and 'refused' in text

    def test_unexpected_exception_maps_to_500(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            with patch.object(GrobidClient, 'post', side_effect=ValueError('weird')):
                name, status, text = client.process_pdf('processFulltextDocument', self._pdf(d))
            assert status == 500 and 'weird' in text

    def test_request_parameters_are_mapped_to_grobid_form_fields(self):
        client = _client()
        client.config['coordinates'] = ['figure', 'persName']
        with tempfile.TemporaryDirectory() as d:
            captured = {}

            def post(url, files=None, data=None, headers=None, timeout=None):
                captured.update(url=url, data=data, headers=headers, timeout=timeout)
                resp = Mock()
                resp.text = '<TEI/>'
                return (resp, 200)

            with patch.object(GrobidClient, 'post', side_effect=post):
                client.process_pdf('processFulltextDocument', self._pdf(d),
                                   generate_ids=True, consolidate_header=True,
                                   consolidate_citations=True, include_raw_citations=True,
                                   include_raw_affiliations=True, tei_coordinates=True,
                                   segment_sentences=True, flavor='sdo/ieee', start=2, end=5)
            assert captured['data'] == {
                'generateIDs': '1', 'consolidateHeader': '1', 'consolidateCitations': '1',
                'includeRawCitations': '1', 'includeRawAffiliations': '1',
                'teiCoordinates': ['figure', 'persName'], 'segmentSentences': '1',
                'flavor': 'sdo/ieee', 'start': '2', 'end': '5',
            }
            assert captured['headers'] == {'Accept': 'text/plain'}
            assert captured['timeout'] == client.config['timeout']

    def test_no_optional_parameters_sends_an_empty_form(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            captured = {}

            def post(url, files=None, data=None, headers=None, timeout=None):
                captured['data'] = data
                resp = Mock()
                resp.text = '<TEI/>'
                return (resp, 200)

            with patch.object(GrobidClient, 'post', side_effect=post):
                client.process_pdf('processFulltextDocument', self._pdf(d), consolidate_header=False)
            assert captured['data'] == {}


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

class TestLoggingConfiguration:

    def _client_with_logging(self, log_config, verbose=False):
        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            client = GrobidClient(check_server=False, verbose=verbose)
            client.config['logging'] = log_config
            client._configure_logging()
        return client

    def test_verbose_forces_info_level(self):
        client = self._client_with_logging({'level': 'ERROR'}, verbose=True)
        assert client.logger.level == logging.INFO

    def test_info_in_config_is_downgraded_to_warning_without_verbose(self):
        client = self._client_with_logging({'level': 'INFO'})
        assert client.logger.level == logging.WARNING

    def test_debug_in_config_is_honoured(self):
        client = self._client_with_logging({'level': 'DEBUG'})
        assert client.logger.level == logging.DEBUG

    def test_console_can_be_disabled(self):
        client = self._client_with_logging({'console': False})
        assert not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
                       for h in client.logger.handlers)

    def test_file_logging_uses_a_rotating_handler(self):
        from logging.handlers import RotatingFileHandler
        with tempfile.TemporaryDirectory() as d:
            log_file = os.path.join(d, 'client.log')
            client = self._client_with_logging({
                'level': 'WARNING', 'file': log_file, 'max_file_size': '1MB', 'backup_count': 2,
            })
            handlers = [h for h in client.logger.handlers if isinstance(h, RotatingFileHandler)]
            assert len(handlers) == 1
            assert handlers[0].maxBytes == 1024 * 1024
            assert handlers[0].backupCount == 2
            client.logger.warning('hello file')
            handlers[0].flush()
            with open(log_file, encoding='utf8') as fh:
                assert 'hello file' in fh.read()
            for h in handlers:
                h.close()
                client.logger.removeHandler(h)

    def test_reconfiguring_does_not_stack_handlers(self):
        client = self._client_with_logging({'console': True})
        client._configure_logging()
        client._configure_logging()
        assert len(client.logger.handlers) == 1
