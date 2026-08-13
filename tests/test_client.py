"""
Unit tests for the GROBID client base ApiClient class.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import json
import requests
from grobid_client.client import ApiClient


class TestApiClient:
    """Test cases for the base ApiClient class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.base_url = "http://localhost:8070"
        self.username = "test_user"
        self.api_key = "test_key"
        self.status_endpoint = "isalive"
        self.timeout = 30

        self.client = ApiClient(
            base_url=self.base_url,
            username=self.username,
            api_key=self.api_key,
            status_endpoint=self.status_endpoint,
            timeout=self.timeout
        )

    def test_init(self):
        """Test ApiClient initialization."""
        assert self.client.base_url == self.base_url
        assert self.client.username == self.username
        assert self.client.api_key == self.api_key
        assert self.client.timeout == self.timeout
        assert self.client.status_endpoint == f"{self.base_url}/{self.status_endpoint}"

    def test_get_credentials(self):
        """Test get_credentials method."""
        credentials = self.client.get_credentials()
        expected = {"username": self.username, "api_key": self.api_key}
        assert credentials == expected

    def test_encode_with_data(self):
        """Test encode method with data."""
        mock_request = Mock()
        test_data = {"key": "value"}

        result = ApiClient.encode(mock_request, test_data)

        mock_request.add_header.assert_called_once_with("Content-Type", "application/json")
        assert result.data == json.dumps(test_data)

    def test_encode_with_none(self):
        """Test encode method with None data."""
        mock_request = Mock()

        result = ApiClient.encode(mock_request, None)

        assert result == mock_request
        mock_request.add_header.assert_not_called()

    def test_decode_success(self):
        """Test decode method with successful JSON response."""
        mock_response = Mock()
        test_data = {"result": "success"}
        mock_response.json.return_value = test_data

        result = ApiClient.decode(mock_response)

        assert result == test_data

    def test_decode_failure(self):
        """Test decode method with JSON decode error."""
        mock_response = Mock()
        error = ValueError("Invalid JSON")
        error.message = "Invalid JSON"
        mock_response.json.side_effect = error

        result = ApiClient.decode(mock_response)

        assert result == "Invalid JSON"

    @patch('grobid_client.client.requests.request')
    def test_call_api_success(self, mock_request):
        """Test call_api method with successful response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_request.return_value = mock_response

        url = "http://test.com/api"
        headers = {"Custom": "header"}
        params = {"param": "value"}
        data = {"data": "value"}
        files = {"file": "content"}

        response, status = self.client.call_api(
            method="POST",
            url=url,
            headers=headers,
            params=params,
            data=data,
            files=files,
            timeout=30
        )

        mock_request.assert_called_once_with(
            "POST",
            url,
            headers={"Custom": "header", "Accept": "application/xml"},
            params=params,
            files=files,
            data=data,
            timeout=30
        )
        assert response == mock_response
        assert status == 200

    @patch('grobid_client.client.requests.request')
    def test_call_api_keeps_the_caller_accept_header(self, mock_request):
        """A caller asking for a representation must actually get it.

        The annotation services (issue #79) need JSON or an annotated PDF back,
        so the class-wide accept type is only the fallback.
        """
        mock_response = Mock()
        mock_response.status_code = 200
        mock_request.return_value = mock_response

        self.client.call_api(
            method="POST",
            url="http://test.com/api",
            headers={"Accept": "application/pdf"},
        )

        assert mock_request.call_args[1]['headers']['Accept'] == 'application/pdf'

    @patch('grobid_client.client.requests.request')
    def test_get_method(self, mock_request):
        """Test GET method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_request.return_value = mock_response

        url = "http://test.com/api"
        params = {"param": "value"}

        response, status = self.client.get(url, params=params)

        mock_request.assert_called_once_with(
            "GET",
            url,
            headers={"Accept": "application/xml"},
            params=params,
            files={},
            data={},
            timeout=None
        )

    @patch('grobid_client.client.requests.request')
    def test_post_method(self, mock_request):
        """Test POST method."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_request.return_value = mock_response

        url = "http://test.com/api"
        data = {"key": "value"}
        files = {"file": "content"}

        response, status = self.client.post(url, data=data, files=files)

        mock_request.assert_called_once_with(
            "POST",
            url,
            headers={"Accept": "application/xml"},
            params={},
            files=files,
            data=data,
            timeout=None
        )

    @patch('grobid_client.client.requests.request')
    def test_put_method(self, mock_request):
        """Test PUT method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_request.return_value = mock_response

        url = "http://test.com/api"
        data = {"key": "updated_value"}

        response, status = self.client.put(url, data=data)

        mock_request.assert_called_once_with(
            "PUT",
            url,
            headers={"Accept": "application/xml"},
            params={},
            files={},
            data=data,
            timeout=None
        )

    @patch('grobid_client.client.requests.request')
    def test_delete_method(self, mock_request):
        """Test DELETE method."""
        mock_response = Mock()
        mock_response.status_code = 204
        mock_request.return_value = mock_response

        url = "http://test.com/api"

        response, status = self.client.delete(url)

        mock_request.assert_called_once_with(
            "DELETE",
            url,
            headers={"Accept": "application/xml"},
            params={},
            files={},
            data={},
            timeout=None
        )

    @patch('grobid_client.client.requests.request')
    def test_service_status(self, mock_request):
        """Test service_status method."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_request.return_value = mock_response

        response, status = self.client.service_status()

        mock_request.assert_called_once_with(
            "GET",
            self.client.status_endpoint,
            headers={"Accept": "application/xml"},
            params={"format": "json"},
            files={},
            data={},
            timeout=None
        )

