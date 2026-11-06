"""
Loose S3 objects are named relative to their common prefix, so two keys
that share a basename (a/x.pdf, b/x.pdf) get two output files.
"""
import os

import pytest
from unittest.mock import Mock, patch

boto3 = pytest.importorskip("boto3")
pytest.importorskip("smart_open")
pytest.importorskip("moto")
try:
    from moto import mock_aws
except ImportError:  # moto < 5
    from moto import mock_s3 as mock_aws

from grobid_client.grobid_client import GrobidClient

BUCKET = "test-bucket"
REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _client():
    with patch.object(GrobidClient, "_test_server_connection"):
        with patch.object(GrobidClient, "_configure_logging"):
            c = GrobidClient(check_server=False)
    c.logger = Mock()
    c.config["queue_size"] = 1          # one object per chunk: collisions across chunks too
    return c


def _tei(out):
    found = []
    for root, _, files in os.walk(out):
        for f in files:
            if f.endswith(".grobid.tei.xml"):
                found.append(os.path.relpath(os.path.join(root, f), out).replace(os.sep, "/"))
    return sorted(found)


def test_common_prefix():
    c = _client()
    assert c._s3_common_prefix([f"s3://{BUCKET}/pdfs/a/x.pdf", f"s3://{BUCKET}/pdfs/b/x.pdf"]) == "pdfs/"
    assert c._s3_common_prefix([f"s3://{BUCKET}/pdfs/x.pdf", f"s3://{BUCKET}/pdfs/y.pdf"]) == "pdfs/"
    assert c._s3_common_prefix([f"s3://{BUCKET}/pdfs/x.pdf"]) == "pdfs/"
    assert c._s3_common_prefix([f"s3://{BUCKET}/x.pdf", f"s3://{BUCKET}/y.pdf"]) == ""
    assert c._s3_common_prefix([f"s3://{BUCKET}/pdfs/xa.pdf", f"s3://{BUCKET}/pdfs/xb.pdf"]) == "pdfs/"
    assert c._s3_relative_name(f"s3://{BUCKET}/pdfs/a/x.pdf", "pdfs/") == os.path.join("a", "x.pdf")
    assert c._s3_relative_name(f"s3://{BUCKET}/pdfs/x.pdf", "pdfs/") == "x.pdf"


@mock_aws
def test_same_basename_under_different_prefixes(tmp_path):
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    for k in ["pdfs/a/x.pdf", "pdfs/b/x.pdf", "pdfs/top.pdf"]:
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"%PDF-" + k.encode())
    c = _client()
    out = str(tmp_path / "out")
    posted = []

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        posted.append(files["input"][0])
        resp = Mock()
        resp.text = "<TEI>ok</TEI>"
        return (resp, 200)

    with patch.object(GrobidClient, "post", side_effect=fake_post):
        c.process("processFulltextDocument", f"s3://{BUCKET}/pdfs/", output=out, force=True)
    assert sorted(posted) == [os.path.join("a", "x.pdf"), os.path.join("b", "x.pdf"), "top.pdf"]
    assert _tei(out) == ["a/x.grobid.tei.xml", "b/x.grobid.tei.xml", "top.grobid.tei.xml"]

    # a rerun without --force skips all three, none is mistaken for another
    posted.clear()
    with patch.object(GrobidClient, "post", side_effect=fake_post):
        c.process("processFulltextDocument", f"s3://{BUCKET}/pdfs/", output=out, force=False)
    assert posted == []


@mock_aws
def test_citation_lists_keep_their_prefix_on_the_disk_route(tmp_path):
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    for k in ["lists/a/refs.txt", "lists/b/refs.txt"]:
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"one ref\n")
    c = _client()
    out = str(tmp_path / "out")

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        resp = Mock()
        resp.text = "<TEI>refs</TEI>"
        return (resp, 200)

    with patch.object(GrobidClient, "post", side_effect=fake_post):
        c.process("processCitationList", f"s3://{BUCKET}/lists/", output=out, force=True)
    assert _tei(out) == ["a/refs.grobid.tei.xml", "b/refs.grobid.tei.xml"]
