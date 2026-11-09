"""
Reading public S3 buckets without AWS credentials.

Listing and reading s3:// inputs went through the default credential
chain only, so a public bucket (for instance s3://digitalcorpora/...)
could not be read on a machine without AWS credentials: the run stopped
with "Unable to locate credentials". --no-sign-request sends the requests
unsigned, as the AWS CLI option of the same name does.
"""
import io
import os
import zipfile
from unittest.mock import Mock, patch

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("smart_open")
pytest.importorskip("moto")
try:
    from moto import mock_aws
except ImportError:  # moto < 5
    from moto import mock_s3 as mock_aws
from botocore import UNSIGNED

from grobid_client import grobid_client as gc_module
from grobid_client.grobid_client import GrobidClient

BUCKET = "public-bucket"


@pytest.fixture
def no_credentials(monkeypatch, tmp_path):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_SECURITY_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # boto3's default session caches the credentials it resolved first
    # (possibly moto's, from an earlier test): start from a fresh one
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)


def _client(**kwargs):
    with patch.object(GrobidClient, "_configure_logging"):
        client = GrobidClient(check_server=False, **kwargs)
    client.logger = Mock()
    return client


def _fake_post(url, files=None, data=None, headers=None, timeout=None):
    response = Mock()
    response.text = "<TEI>ok</TEI>"
    return response, 200


def _public_bucket():
    s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="owner", aws_secret_access_key="owner")
    s3.create_bucket(Bucket=BUCKET)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("0000000.pdf", b"%PDF-1")
        zf.writestr("0000001.pdf", b"%PDF-2")
    s3.put_object(Bucket=BUCKET, Key="corpus/zipfiles/0000.zip", Body=buf.getvalue(),
                  ACL="public-read")
    s3.put_object(Bucket=BUCKET, Key="corpus/loose/a.pdf", Body=b"%PDF-3", ACL="public-read")


def test_one_client_is_shared_and_unsigned_when_asked():
    client = _client(s3_no_sign_request=True)
    s3 = client._s3_client()
    assert s3 is client._s3_client()
    assert s3.meta.config.signature_version is UNSIGNED


def test_default_client_signs_requests():
    client = _client()
    assert client._s3_client().meta.config.signature_version is not UNSIGNED


def test_smart_open_reads_with_the_same_client():
    client = _client(s3_no_sign_request=True)
    with patch("smart_open.open") as smart_open_open:
        client._s3_open("s3://b/k.zip")
    _, kwargs = smart_open_open.call_args
    assert kwargs["transport_params"]["client"] is client._s3_client()


@mock_aws
def test_public_zip_and_prefix_are_read_without_credentials(no_credentials, tmp_path):
    _public_bucket()
    client = _client(s3_no_sign_request=True)
    with patch.object(client, "post", side_effect=_fake_post):
        client.process_paths("processFulltextDocument",
                             [f"s3://{BUCKET}/corpus/zipfiles/", f"s3://{BUCKET}/corpus/loose/a.pdf"],
                             output=str(tmp_path / "out"), n=2)
    written = sorted(os.path.relpath(os.path.join(r, f), tmp_path / "out")
                     for r, _, fs in os.walk(tmp_path / "out") for f in fs)
    assert written == ["0000000.grobid.tei.xml", "0000001.grobid.tei.xml", "a.grobid.tei.xml"]


def test_without_the_option_missing_credentials_still_fail(no_credentials):
    # no moto here: it supplies credentials of its own. botocore gives up
    # while signing, before anything is sent.
    client = _client()
    from botocore.exceptions import NoCredentialsError
    with pytest.raises(NoCredentialsError):
        client._resolve_s3_paths(f"s3://{BUCKET}/corpus/zipfiles/")


def test_cli_suggests_the_option_on_missing_credentials(monkeypatch, tmp_path):
    from botocore.exceptions import NoCredentialsError
    logged = []
    fake_client = Mock()
    fake_client.process_paths.side_effect = NoCredentialsError()
    fake_client.logger.error.side_effect = logged.append
    monkeypatch.setattr(gc_module, "GrobidClient", Mock(return_value=fake_client))
    monkeypatch.setattr("sys.argv", ["grobid_client", "--input", "s3://b/k.zip", "processFulltextDocument"])
    with pytest.raises(SystemExit) as exit_info:
        gc_module.main()
    assert exit_info.value.code == 1
    assert any("--no-sign-request" in message for message in logged)
