"""
Constructing a client with check_server=True must fail when the server does
not answer isalive with 200, not only when the connection fails.
"""
import sys
from unittest.mock import Mock, patch

import pytest
import requests

from grobid_client.grobid_client import GrobidClient, ServerUnavailableException, main


def _get(status):
    return patch('grobid_client.grobid_client.requests.get', return_value=Mock(status_code=status))


def test_non_200_isalive_raises():
    with _get(404):
        with pytest.raises(ServerUnavailableException) as exc:
            GrobidClient(check_server=True, grobid_server='http://host:8070/wrong')
    assert 'status: 404' in str(exc.value)
    assert 'http://host:8070/wrong' in str(exc.value)


def test_connection_error_still_raises():
    with patch('grobid_client.grobid_client.requests.get', side_effect=requests.exceptions.ConnectionError('refused')):
        with pytest.raises(ServerUnavailableException):
            GrobidClient(check_server=True)


def test_200_constructs():
    with _get(200):
        client = GrobidClient(check_server=True)
        assert client.ping() == (True, 200)


def test_check_server_false_skips_the_check():
    with patch('grobid_client.grobid_client.requests.get') as get:
        with patch.object(GrobidClient, '_configure_logging'):
            GrobidClient(check_server=False)
    get.assert_not_called()


def test_cli_exits_on_a_misconfigured_server(tmp_path):
    with _get(404):
        with patch.object(sys, 'argv', ['grobid_client', '--input', str(tmp_path),
                                        '--server', 'http://host:8070/wrong', 'processFulltextDocument']):
            with patch.object(GrobidClient, 'process_paths') as pp:
                with pytest.raises(SystemExit) as exc:
                    main()
    assert exc.value.code == 1
    pp.assert_not_called()


def test_ping_reports_an_unreachable_server_instead_of_raising():
    client = GrobidClient(check_server=False, grobid_server='http://127.0.0.1:9')
    with patch('grobid_client.grobid_client.requests.get',
               side_effect=requests.exceptions.ConnectionError('refused')):
        assert client.ping() == (False, 0)


def test_ping_reports_a_bad_status():
    client = GrobidClient(check_server=False)
    with _get(503):
        assert client.ping() == (False, 503)
