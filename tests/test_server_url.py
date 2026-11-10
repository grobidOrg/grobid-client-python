"""The server URL may be written with a trailing slash."""
from unittest.mock import Mock, patch

import pytest

from grobid_client.grobid_client import GrobidClient


@pytest.mark.parametrize('server', [
    'http://localhost:8070', 'http://localhost:8070/', 'http://localhost:8070//',
])
def test_trailing_slashes_do_not_reach_the_path(server):
    client = GrobidClient(grobid_server=server, check_server=False)
    assert client.get_server_url('isalive') == 'http://localhost:8070/api/isalive'
    assert client.get_server_url('processFulltextDocument') == \
        'http://localhost:8070/api/processFulltextDocument'


def test_a_path_prefix_is_kept():
    client = GrobidClient(grobid_server='https://example.org/grobid/', check_server=False)
    assert client.get_server_url('isalive') == 'https://example.org/grobid/api/isalive'


def test_server_check_uses_the_normalised_url():
    response = Mock(status_code=200)
    with patch('grobid_client.grobid_client.requests.get', return_value=response) as get:
        GrobidClient(grobid_server='http://localhost:8070/', check_server=True)
    assert get.call_args[0][0] == 'http://localhost:8070/api/isalive'
