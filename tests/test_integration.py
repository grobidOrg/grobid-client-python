"""
Integration tests for the GROBID client.
These tests require a running GROBID server for full functionality.
"""
import pytest
import tempfile
import os
import json
import requests
from unittest.mock import patch, Mock, mock_open

from grobid_client.grobid_client import GrobidClient, ServerUnavailableException


class TestGrobidClientIntegration:
    """Integration test cases for the GrobidClient class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.test_server_url = 'http://localhost:8070'

        # Create a temporary config file
        self.temp_config = {
            'grobid_server': self.test_server_url,
            'batch_size': 10,
            'coordinates': ["persName", "figure"],
            'sleep_time': 2,
            'timeout': 30,
            'logging': {
                'level': 'DEBUG',
                'console': True,
                'file': None
            }
        }

        self.temp_dir = tempfile.mkdtemp()
        self.config_file = os.path.join(self.temp_dir, 'test_config.json')

        with open(self.config_file, 'w') as f:
            json.dump(self.temp_config, f)

    def teardown_method(self):
        """Clean up test fixtures."""
        if os.path.exists(self.config_file):
            os.remove(self.config_file)
        # Use shutil.rmtree for better cleanup of non-empty directories
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    @patch('grobid_client.grobid_client.requests.get')
    def test_client_initialization_with_config_file(self, mock_get):
        """Test client initialization with a configuration file."""
        # Mock server response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        client = GrobidClient(config_path=self.config_file)

        # Verify config was loaded
        assert client.config['grobid_server'] == self.test_server_url
        assert client.config['batch_size'] == 10
        assert client.config['sleep_time'] == 2
        assert client.config['timeout'] == 30

    @patch('grobid_client.grobid_client.requests.get')
    def test_server_connection_check(self, mock_get):
        """Test server connection checking functionality."""
        # Test successful connection
        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        client = GrobidClient(check_server=False)
        is_available, status = client._test_server_connection()

        assert is_available is True
        assert status == 200

        # Test failed connection
        mock_response.status_code = 500
        is_available, status = client._test_server_connection()

        assert is_available is False
        assert status == 500

    def test_configuration_validation(self):
        """Test configuration validation and merging."""
        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
                client = GrobidClient(
                    grobid_server='http://custom:9090',
                    batch_size=500,
                    config_path=self.config_file,
                    check_server=False
                )

                # Constructor values should override config file values (CLI precedence)
                assert client.config['grobid_server'] == 'http://custom:9090'
                assert client.config['batch_size'] == 500

    def test_logging_configuration(self):
        """Test logging configuration from config file."""
        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            client = GrobidClient(config_path=self.config_file, check_server=False)

            # Verify logger was configured
            assert client.logger is not None
            assert client.logger.level == 10  # DEBUG level

    def test_file_processing_workflow(self):
        """Test the complete file processing workflow."""
        # Create temporary test files
        test_input_dir = os.path.join(self.temp_dir, 'input')
        test_output_dir = os.path.join(self.temp_dir, 'output')
        os.makedirs(test_input_dir)
        os.makedirs(test_output_dir)

        # Create a dummy PDF file
        test_pdf = os.path.join(test_input_dir, 'test.pdf')
        with open(test_pdf, 'wb') as f:
            f.write(b'%PDF-1.4 dummy content')

        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            with patch('grobid_client.grobid_client.GrobidClient.process_pdf') as mock_process_pdf:
                mock_process_pdf.return_value = (test_pdf, 200, '<TEI>test content</TEI>')

                client = GrobidClient(check_server=False)

                # Test the processing workflow
                client.process(
                    'processFulltextDocument',
                    test_input_dir,
                    output=test_output_dir,
                    n=1,
                    force=True,
                    verbose=True
                )

                # Verify process_pdf was called
                mock_process_pdf.assert_called()

    def test_batch_processing(self):
        """Test batch processing functionality."""
        test_files = [f'/test/file_{i}.pdf' for i in range(5)]

        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
                with patch('os.path.isfile', return_value=False):
                    with patch('pathlib.Path'):
                        with patch('builtins.open', mock_open()):
                            with patch('concurrent.futures.ThreadPoolExecutor') as mock_executor:
                                # Setup mock executor
                                mock_future = Mock()
                                mock_future.result.return_value = ('/test/file_0.pdf', 200, '<TEI>content</TEI>')
                                mock_executor_instance = Mock()
                                mock_executor_instance.submit.return_value = mock_future
                                mock_executor_instance.__enter__ = Mock(return_value=mock_executor_instance)
                                mock_executor_instance.__exit__ = Mock(return_value=None)
                                mock_executor.return_value = mock_executor_instance

                                with patch('concurrent.futures.as_completed', return_value=[mock_future] * 5):
                                    client = GrobidClient(check_server=False)
                                    # Ensure logger is available for process_batch
                                    client.logger = Mock()

                                    processed_count = client.process_batch(
                                        'processFulltextDocument',
                                        test_files,
                                        '/test',
                                        '/output',
                                        n=2,
                                        generate_ids=False,
                                        consolidate_header=False,
                                        consolidate_citations=False,
                                        include_raw_citations=False,
                                        include_raw_affiliations=False,
                                        tei_coordinates=False,
                                        segment_sentences=False,
                                        force=True
                                    )

                                    assert processed_count == (5, 0, 0)

    def test_error_handling_and_recovery(self):
        """Test error handling and recovery mechanisms."""
        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
                client = GrobidClient(check_server=False)
                # Ensure logger is available
                client.logger = Mock()

                # Test file not found error
                with patch('builtins.open', side_effect=IOError("File not found")):
                    result = client.process_pdf(
                        'processFulltextDocument',
                        '/nonexistent/file.pdf',
                        False, False, False, False, False, False, False
                    )

                    assert result[1] == 400
                    assert 'Failed to open file' in result[2]

    def test_different_file_types(self):
        """Test processing different file types."""
        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
                client = GrobidClient(check_server=False)

                # Test TXT file processing
                with patch('builtins.open', mock_open(read_data='Reference 1\nReference 2')):
                    with patch('grobid_client.grobid_client.GrobidClient.post') as mock_post:
                        mock_response = Mock()
                        mock_response.text = '<citations>parsed</citations>'
                        mock_post.return_value = (mock_response, 200)

                        result = client.process_txt(
                            'processCitationList',
                            '/test/refs.txt',
                            False, False, True, True, False, False, False
                        )

                        assert result[1] == 200

    @patch('grobid_client.grobid_client.requests.get')
    def test_server_unavailable_exception(self, mock_get):
        """Test ServerUnavailableException is raised when server is down."""
        mock_get.side_effect = requests.exceptions.RequestException("Connection refused")

        with pytest.raises(ServerUnavailableException):
            GrobidClient(check_server=True)

    def test_config_file_error_handling(self):
        """Test configuration file error handling."""
        # Test with invalid JSON
        invalid_config_file = os.path.join(self.temp_dir, 'invalid_config.json')
        with open(invalid_config_file, 'w') as f:
            f.write('invalid json content')

        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
                client = GrobidClient(check_server=False)

                with pytest.raises(json.JSONDecodeError):
                    client._load_config(invalid_config_file)

        os.remove(invalid_config_file)

    def test_output_directory_creation(self):
        """Test automatic output directory creation."""
        test_input_dir = os.path.join(self.temp_dir, 'input')
        test_output_dir = os.path.join(self.temp_dir, 'output_new')
        os.makedirs(test_input_dir)

        # Create a dummy PDF file
        test_pdf = os.path.join(test_input_dir, 'test.pdf')
        with open(test_pdf, 'wb') as f:
            f.write(b'%PDF-1.4 dummy content')

        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            with patch('grobid_client.grobid_client.GrobidClient.process_pdf') as mock_process_pdf:
                mock_process_pdf.return_value = (test_pdf, 200, '<TEI>test content</TEI>')

                client = GrobidClient(check_server=False)

                # The output directory should be created automatically
                with patch('pathlib.Path.mkdir') as mock_mkdir:
                    client.process(
                        'processFulltextDocument',
                        test_input_dir,
                        output=test_output_dir,
                        force=True
                    )

                    # Verify mkdir was called (directory creation is handled in process_batch)
                    mock_mkdir.assert_called()

    def test_real_file_processing_with_test_resources(self):
        """Test processing with real test files from the resources directory."""
        # Use the actual test files in the resources directory
        test_pdf_dir = '/Users/lfoppiano/development/projects/grobid-client-python/resources/test_pdf'

        if os.path.exists(test_pdf_dir):
            with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
                with patch('grobid_client.grobid_client.GrobidClient.process_pdf') as mock_process_pdf:
                    mock_process_pdf.return_value = ('test.pdf', 200, '<TEI>mocked content</TEI>')

                    client = GrobidClient(check_server=False)

                    # Test that the client can discover real PDF files
                    pdf_files = []
                    for root, dirs, files in os.walk(test_pdf_dir):
                        for file in files:
                            if file.endswith('.pdf'):
                                pdf_files.append(os.path.join(root, file))

                    assert len(pdf_files) > 0, "Should find at least one PDF file in test resources"

    def test_concurrent_processing_stress(self):
        """Test concurrent processing with multiple files."""
        # Create multiple test files
        test_files = [f'/test/file_{i}.pdf' for i in range(20)]

        with patch('grobid_client.grobid_client.GrobidClient._test_server_connection'):
            with patch('grobid_client.grobid_client.GrobidClient._configure_logging'):
                with patch('os.path.isfile', return_value=False):
                    with patch('pathlib.Path'):
                        with patch('builtins.open', mock_open()):
                            with patch('concurrent.futures.ThreadPoolExecutor') as mock_executor:
                                # Setup mock executor for stress test
                                mock_futures = []
                                for i in range(20):
                                    mock_future = Mock()
                                    mock_future.result.return_value = (f'/test/file_{i}.pdf', 200, f'<TEI>content_{i}</TEI>')
                                    mock_futures.append(mock_future)

                                mock_executor_instance = Mock()
                                mock_executor_instance.submit.side_effect = mock_futures
                                mock_executor_instance.__enter__ = Mock(return_value=mock_executor_instance)
                                mock_executor_instance.__exit__ = Mock(return_value=None)
                                mock_executor.return_value = mock_executor_instance

                                with patch('concurrent.futures.as_completed', return_value=mock_futures):
                                    client = GrobidClient(check_server=False)
                                    # Ensure logger is available for process_batch
                                    client.logger = Mock()

                                    processed_count = client.process_batch(
                                        'processFulltextDocument',
                                        test_files,
                                        '/test',
                                        '/output',
                                        n=5,  # 5 concurrent threads
                                        generate_ids=False,
                                        consolidate_header=False,
                                        consolidate_citations=False,
                                        include_raw_citations=False,
                                        include_raw_affiliations=False,
                                        tei_coordinates=False,
                                        segment_sentences=False,
                                        force=True
                                    )

                                    assert processed_count == (20, 0, 0)
