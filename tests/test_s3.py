"""
Tests for streaming inputs from S3 (the optional 's3' extra).

These use moto to mock S3 and mock GrobidClient.post so no GROBID server or real
AWS is needed.
"""
import io
import os
import zipfile

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
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _client(queue_size=2):
    with patch.object(GrobidClient, "_test_server_connection"):
        with patch.object(GrobidClient, "_configure_logging"):
            c = GrobidClient(check_server=False)
    c.logger = Mock()
    c.config["queue_size"] = queue_size
    return c


def _fake_post(url, files=None, data=None, headers=None, timeout=None):
    resp = Mock()
    resp.text = "<TEI>ok</TEI>"
    return (resp, 200)


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _tei(out):
    return sorted(f for _, _, fs in os.walk(out) for f in fs if f.endswith(".grobid.tei.xml"))


def test_split_and_basename():
    c = _client()
    assert c._split_s3("s3://bucket/a/b/c.zip") == ("bucket", "a/b/c.zip")
    assert c._s3_basename("s3://bucket/a/b/c.zip") == "c.zip"
    assert c._is_s3("s3://bucket/x") is True
    assert c._is_s3("/local/x") is False


def test_resolve_single_object_needs_no_listing():
    # a concrete object key is returned as-is (no S3 call at all)
    c = _client()
    assert c._resolve_s3_paths("s3://bucket/a/b/file.zip") == ["s3://bucket/a/b/file.zip"]


@mock_aws
def test_resolve_prefix_and_glob():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    for k in ["p/0000.zip", "p/0001.zip", "p/readme.txt", "q/x.zip"]:
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"x")

    c = _client()
    assert c._resolve_s3_paths(f"s3://{BUCKET}/p/") == [
        f"s3://{BUCKET}/p/0000.zip", f"s3://{BUCKET}/p/0001.zip", f"s3://{BUCKET}/p/readme.txt",
    ]
    assert c._resolve_s3_paths(f"s3://{BUCKET}/p/*.zip") == [
        f"s3://{BUCKET}/p/0000.zip", f"s3://{BUCKET}/p/0001.zip",
    ]


@mock_aws
def test_process_s3_zip_range_streamed(tmp_path):
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key="arch/docs.zip", Body=_zip_bytes({
        "0000001.pdf": b"%PDF-a",
        "sub/0000002.pdf": b"%PDF-b",
        "note.txt": b"not a pdf",
    }))
    c = _client()
    out = str(tmp_path / "out")
    with patch.object(GrobidClient, "post", side_effect=_fake_post):
        c.process("processFulltextDocument", f"s3://{BUCKET}/arch/docs.zip", output=out, force=True)
    # both PDFs processed, .txt ignored
    assert _tei(out) == ["0000001.grobid.tei.xml", "0000002.grobid.tei.xml"]


@mock_aws
def test_process_s3_loose_pdfs_glob(tmp_path):
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    for k in ["pdfs/0000001.pdf", "pdfs/0000002.pdf", "pdfs/skip.txt"]:
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"%PDF")
    c = _client()
    out = str(tmp_path / "out")
    with patch.object(GrobidClient, "post", side_effect=_fake_post):
        c.process("processFulltextDocument", f"s3://{BUCKET}/pdfs/*.pdf", output=out, force=True)
    assert _tei(out) == ["0000001.grobid.tei.xml", "0000002.grobid.tei.xml"]


@mock_aws
def test_process_paths_mixed_local_and_s3(tmp_path):
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key="a.zip", Body=_zip_bytes({"0000009.pdf": b"%PDF-z"}))
    local_pdf = tmp_path / "local.pdf"
    local_pdf.write_bytes(b"%PDF-l")

    c = _client()
    out = str(tmp_path / "out")
    with patch.object(GrobidClient, "post", side_effect=_fake_post):
        c.process_paths(
            "processFulltextDocument",
            [str(local_pdf), f"s3://{BUCKET}/a.zip"],
            output=out, force=True,
        )
    assert _tei(out) == ["0000009.grobid.tei.xml", "local.grobid.tei.xml"]


@mock_aws
def test_process_s3_never_touches_disk(tmp_path):
    """Loose PDFs and zip entries go from S3 straight to GROBID, in memory."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key="pdfs/0000001.pdf", Body=b"%PDF-mem")
    s3.put_object(Bucket=BUCKET, Key="arch/docs.zip", Body=_zip_bytes({"z.pdf": b"%PDF-z"}))

    c = _client()
    out = str(tmp_path / "out")
    posted = []

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        # content read inside the call: the handle is closed right after it
        posted.append((files["input"][0], files["input"][1].read()))
        resp = Mock()
        resp.text = "<TEI>ok</TEI>"
        return (resp, 200)

    with patch.object(GrobidClient, "post", side_effect=fake_post):
        with patch("grobid_client.grobid_client.tempfile.mkdtemp") as mock_mkdtemp:
            c.process_paths(
                "processFulltextDocument",
                [f"s3://{BUCKET}/pdfs/*.pdf", f"s3://{BUCKET}/arch/docs.zip"],
                output=out, force=True,
            )

    mock_mkdtemp.assert_not_called()
    assert sorted(posted) == [("0000001.pdf", b"%PDF-mem"), ("z.pdf", b"%PDF-z")]
    assert _tei(out) == ["0000001.grobid.tei.xml", "z.grobid.tei.xml"]


def test_missing_extra_raises_helpful_error():
    """If smart_open isn't importable, a clear install hint is raised."""
    c = _client()
    with patch.dict("sys.modules", {"smart_open": None}):
        with pytest.raises(ImportError, match=r"pip install grobid-client-python\[s3\]"):
            c._s3_open("s3://bucket/key.zip")


@mock_aws
def test_convert_a_remote_zip_of_tei_without_downloading_it(tmp_path):
    """A corpus of TEI in S3 is converted straight out of the range-streamed zip."""
    from pathlib import Path

    from grobid_client.format.tei_archive import convert_archive
    from tests.resources import TEST_DATA_PATH

    tei = (Path(TEST_DATA_PATH) / "0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml").read_bytes()
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key="arch/corpus.zip", Body=_zip_bytes({
        "0000001.tei.xml": tei,
        "sub/0000002.grobid.tei.xml": tei,
        "note.txt": b"not a TEI",
    }))

    out = tmp_path / "out"
    stats = convert_archive(f"s3://{BUCKET}/arch/corpus.zip", str(out),
                            json_output=True, markdown_output=True, workers=1)

    assert (stats.total, stats.converted, stats.failed) == (2, 2, 0)
    assert sorted(p.name for p in out.iterdir()) == [
        "0000001.json", "0000001.md", "0000002.json", "0000002.md",
    ]
    # the archive itself never landed on disk
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


@mock_aws
def test_converting_a_remote_zip_needs_the_s3_extra():
    from grobid_client.format.tei_archive import convert_archive

    with patch.dict("sys.modules", {"smart_open": None}):
        with pytest.raises(ImportError, match=r"pip install grobid-client-python\[s3\]"):
            convert_archive("s3://bucket/corpus.zip", "out", markdown_output=True)
