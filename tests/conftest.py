"""
Pytest configuration and fixtures for GROBID client tests.
"""
import pytest
import tempfile
import os
import json
from unittest.mock import patch


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)


@pytest.fixture
def temp_config_file(temp_dir):
    """Create a temporary config file for tests."""
    config = {
        'grobid_server': 'http://localhost:8070',
        'queue_size': 10,
        'coordinates': ["persName", "figure"],
        'sleep_time': 2,
        'timeout': 30,
        'logging': {
            'level': 'DEBUG',
            'console': True,
            'file': None
        }
    }

    config_file = os.path.join(temp_dir, 'test_config.json')
    with open(config_file, 'w') as f:
        json.dump(config, f)

    return config_file


@pytest.fixture
def mock_grobid_client():
    """Create a mocked GrobidClient for testing."""
    with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
        with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
            from grobid_client.grobid_client import GrobidClient
            client = GrobidClient(check_server=False)
            yield client


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line(
        "markers", "unit: mark test as a unit test"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as an integration test"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
