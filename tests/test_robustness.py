"""Small robustness fixes: exit codes, n validation, counts, handle leaks, archive names."""
import builtins
import io
import os
import sys
import tarfile
import tempfile
from unittest.mock import Mock, patch

import pytest

from grobid_client.grobid_client import GrobidClient, main


def _client():
    with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
        with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
            client = GrobidClient(check_server=False)
    client.logger = Mock()
    return client


def _run_main(argv):
    with patch.object(GrobidClient, '__init__', lambda self, **kw: setattr(self, 'logger', Mock())):
        with patch.object(GrobidClient, 'process_paths') as pp:
            with patch.object(sys, 'argv', ['grobid_client'] + argv):
                main()
    return pp


class TestCli:

    def test_exits_without_the_site_builtin(self, tmp_path):
        """sys.exit, not the `exit` helper that only the site module injects."""
        with patch.object(builtins, 'exit', side_effect=AssertionError('builtin exit used')):
            with pytest.raises(SystemExit) as exc:
                _run_main(['processFulltextDocument'])
        assert exc.value.code == 1

    @pytest.mark.parametrize('n', ['0', '-3'])
    def test_n_below_one_is_rejected(self, tmp_path, n):
        with pytest.raises(SystemExit) as exc:
            _run_main(['--input', str(tmp_path), '--n', n, 'processFulltextDocument'])
        assert exc.value.code == 1

    def test_n_one_is_accepted(self, tmp_path):
        pp = _run_main(['--input', str(tmp_path), '--n', '1', 'processFulltextDocument'])
        assert pp.call_args.kwargs['n'] == 1


class TestLibraryWithZeroWorkers:

    def test_process_batch_runs_with_n_zero(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, 'a.pdf')
            open(pdf, 'wb').close()
            with patch.object(GrobidClient, 'post', return_value=(Mock(text='<TEI/>'), 200)):
                result = client.process_batch(
                    'processFulltextDocument', [pdf], d, os.path.join(d, 'out'), 0,
                    False, False, False, False, False, False, False, True)
            assert result == (1, 0, 0)
        assert client._effective_queue_size(0) == 2


class TestProcessedCount:

    def test_a_failed_write_is_an_error_not_a_success(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, 'a.pdf')
            open(pdf, 'wb').close()
            with patch.object(GrobidClient, 'post', return_value=(Mock(text='<TEI/>'), 200)):
                with patch.object(GrobidClient, '_write_atomic', side_effect=OSError('disk full')):
                    result = client.process_batch(
                        'processFulltextDocument', [pdf], d, os.path.join(d, 'out'), 1,
                        False, False, False, False, False, False, False, True)
        assert result == (0, 1, 0)


class TestArchiveHandles:

    def test_bad_s3_zip_closes_the_stream(self):
        client = _client()
        stream = io.BytesIO(b'this is not a zip file')
        with patch.object(GrobidClient, '_s3_open', return_value=stream):
            with pytest.raises(Exception):
                client._open_archive('s3://bucket/bad.zip')
        assert stream.closed

    def test_tar_that_fails_to_list_is_closed(self):
        client = _client()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'x.tar')
            with tarfile.open(path, 'w') as tar:
                info = tarfile.TarInfo('a.pdf')
                info.size = 0
                tar.addfile(info, io.BytesIO(b''))
            with patch.object(tarfile.TarFile, 'getmembers', side_effect=tarfile.ReadError('truncated')):
                with patch.object(tarfile.TarFile, 'close', autospec=True) as close:
                    with pytest.raises(tarfile.ReadError):
                        client._open_archive(path)
            close.assert_called()

    def test_drive_letters_are_stripped_from_entry_names(self):
        client = _client()
        assert client._safe_member_path('dest', 'C:/foo.pdf') == os.path.join('dest', 'foo.pdf')
        assert client._safe_member_path('dest', 'c:\\sub\\foo.pdf') == os.path.join('dest', 'sub', 'foo.pdf')
        assert client._safe_member_path('dest', '/abs/../x.pdf') == os.path.join('dest', 'abs', 'x.pdf')
        assert client._safe_member_path('dest', 'C:') is None
