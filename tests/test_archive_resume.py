"""
A resumed archive run must not read the entries it already finished.

process_batch skips an entry whose TEI exists, but only once it holds the
entry's bytes, so a rerun read every finished entry again first. For an
s3:// zip that is a download: on a public 1.3 GB corpus zip a rerun after
330 finished documents spent over a minute fetching them again.
"""
import io
import os
import tarfile
import zipfile
from unittest.mock import Mock, patch

import pytest

from grobid_client.grobid_client import GrobidClient

TEI = "<TEI xmlns=\"http://www.tei-c.org/ns/1.0\"><teiHeader/><text><body/></text></TEI>"
NAMES = ["a.pdf", "sub/b.pdf", "sub/c.pdf", "d.pdf"]


def _client():
    with patch.object(GrobidClient, "_configure_logging"):
        client = GrobidClient(check_server=False)
    client.logger = Mock()
    client.config["queue_size"] = 2
    return client


def _fake_post(url, files=None, data=None, headers=None, timeout=None):
    response = Mock()
    response.text = TEI
    return response, 200


@pytest.fixture(params=["zip", "tar"])
def archive(request, tmp_path):
    path = tmp_path / f"in.{request.param}"
    if request.param == "zip":
        with zipfile.ZipFile(path, "w") as zf:
            for name in NAMES:
                zf.writestr(name, b"%PDF " + name.encode())
    else:
        with tarfile.open(path, "w") as tf:
            for name in NAMES:
                data = b"%PDF " + name.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return str(path)


def _run(client, archive, out, **kwargs):
    """Process the archive; return the entries read and the summary counts."""
    read = []
    real_read = client._read_archive_member

    def spy(kind, handle, member_name):
        read.append(member_name)
        return real_read(kind, handle, member_name)

    with patch.object(client, "post", side_effect=_fake_post), \
            patch.object(client, "_read_archive_member", side_effect=spy):
        counts = client._process_archive_core(
            "processFulltextDocument", archive, str(out), 2, False, False, False, False, False,
            False, False, kwargs.get("force", False), False, None,
            kwargs.get("json_output", False), False, skip_errors=kwargs.get("skip_errors", False))
    return sorted(read), counts


def test_rerun_reads_nothing(archive, tmp_path):
    client, out = _client(), tmp_path / "out"
    read, counts = _run(client, archive, out)
    assert read == sorted(NAMES)
    assert counts == (4, 4, 0, 0)

    read, counts = _run(client, archive, out)
    assert read == []
    assert counts == (4, 0, 0, 4)


def test_only_missing_entries_are_read(archive, tmp_path):
    client, out = _client(), tmp_path / "out"
    _run(client, archive, out)
    os.remove(out / "sub" / "b.grobid.tei.xml")
    read, counts = _run(client, archive, out)
    assert read == ["sub/b.pdf"]
    assert counts == (4, 1, 0, 3)


def test_force_reads_everything(archive, tmp_path):
    client, out = _client(), tmp_path / "out"
    _run(client, archive, out)
    read, _ = _run(client, archive, out, force=True)
    assert read == sorted(NAMES)


def test_missing_json_is_still_regenerated(archive, tmp_path):
    client, out = _client(), tmp_path / "out"
    _run(client, archive, out, json_output=True)
    os.remove(out / "d.json")
    read, _ = _run(client, archive, out, json_output=True)
    assert read == ["d.pdf"]
    assert (out / "d.json").exists()


def test_previous_failure_is_not_read_with_skip_errors(archive, tmp_path):
    client, out = _client(), tmp_path / "out"
    _run(client, archive, out)
    os.remove(out / "a.grobid.tei.xml")
    (out / "a_500.txt").write_text("boom")
    read, counts = _run(client, archive, out, skip_errors=True)
    assert read == []
    assert counts == (4, 0, 0, 4)
    read, _ = _run(client, archive, out)  # without --skip_errors it is retried
    assert read == ["a.pdf"]


def test_ctrl_c_exits_cleanly_with_a_resume_hint(monkeypatch, tmp_path):
    from grobid_client import grobid_client as gc_module
    logged = []
    fake_client = Mock()
    fake_client.process_paths.side_effect = KeyboardInterrupt
    fake_client.logger.error.side_effect = logged.append
    monkeypatch.setattr(gc_module, "GrobidClient", Mock(return_value=fake_client))
    monkeypatch.setattr("sys.argv", ["grobid_client", "--input", str(tmp_path), "processFulltextDocument"])
    with pytest.raises(SystemExit) as exit_info:
        gc_module.main()
    assert exit_info.value.code == 130
    assert any("run the same command again" in message for message in logged)
