"""The shipped config.json must not set a logging level the client ignores."""
import json
import logging
import os
from unittest.mock import patch

from grobid_client.grobid_client import GrobidClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_shipped_config_level_is_effective():
    with open(os.path.join(ROOT, 'config.json'), encoding='utf8') as fh:
        level = json.load(fh)['logging']['level']
    with patch.object(GrobidClient, '_test_server_connection'):
        client = GrobidClient(check_server=False, config_path=os.path.join(ROOT, 'config.json'))
    assert client.logger.level == getattr(logging, level)
