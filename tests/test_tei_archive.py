"""
Unit tests for converting the TEI documents of an archive in memory.
"""
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from grobid_client.archive import ArchiveReader
from grobid_client.format import tei_archive
from grobid_client.format.tei_archive import (
    ArchiveConversionStats,
    convert_archive,
    default_output_dir,
    default_queue_size,
    is_tei_member,
    looks_like_archive_input,
    tei_stem,
)
from tests.resources import TEST_DATA_PATH

SAMPLE_TEI = Path(TEST_DATA_PATH) / "0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml"


@pytest.fixture(scope="module")
def tei_bytes():
    return SAMPLE_TEI.read_bytes()


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return str(path)


def make_targz(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return str(path)


class TestArchiveNaming:
    """What counts as a TEI entry, and what its conversions are called."""

    def test_tei_entries_are_recognized_by_name(self):
        assert is_tei_member("paper.tei.xml")
        assert is_tei_member("paper.grobid.tei.xml")
        assert is_tei_member("PAPER.TEI.XML")
        assert is_tei_member("pub2tei/paper.xml")
        assert not is_tei_member("paper.pdf")
        assert not is_tei_member("readme.txt")

    def test_outputs_are_named_after_the_entry(self):
        assert tei_stem("paper.grobid.tei.xml") == "paper"
        assert tei_stem("sub/dir/paper.tei.xml") == "paper"
        assert tei_stem("sub\\dir\\paper.tei") == "paper"
        assert tei_stem("paper.xml") == "paper"

    def test_the_output_directory_is_named_after_the_archive(self):
        assert default_output_dir("/data/corpus.zip") == "/data/corpus"
        assert default_output_dir("/data/corpus.tar.gz") == "/data/corpus"
        assert default_output_dir("s3://bucket/deep/corpus.zip") == "corpus"

    def test_an_archive_is_told_apart_from_a_document(self):
        assert looks_like_archive_input("corpus.zip")
        assert looks_like_archive_input("s3://bucket/corpus.zip")
        assert not looks_like_archive_input("paper.tei.xml")
        assert not looks_like_archive_input("-")

    def test_the_queue_is_deeper_than_the_worker_count(self):
        """TEI is small enough that reading ahead is not what bounds memory."""
        assert default_queue_size(1) >= 4
        assert default_queue_size(8) >= 32


class TestArchiveConversion:
    """Entries are converted in memory, and only the results reach the disk."""

    def test_a_zip_converts_to_both_formats(self, tmp_path, tei_bytes):
        archive = make_zip(tmp_path / "corpus.zip", {
            "a.tei.xml": tei_bytes,
            "sub/b.grobid.tei.xml": tei_bytes,
            "readme.txt": b"not a TEI",
        })
        out = tmp_path / "out"

        stats = convert_archive(archive, str(out), json_output=True,
                                markdown_output=True, workers=1)

        assert (stats.total, stats.converted, stats.failed) == (2, 2, 0)
        assert sorted(p.name for p in out.iterdir()) == ["a.json", "a.md", "b.json", "b.md"]
        assert json.loads((out / "a.json").read_text())["biblio"]["title"]
        assert (out / "b.md").read_text().startswith("# Multi-contact")

    def test_a_tarball_converts_the_same_way(self, tmp_path, tei_bytes):
        archive = make_targz(tmp_path / "corpus.tar.gz", {
            "x.tei.xml": tei_bytes,
            "nested/y.tei.xml": tei_bytes,
        })
        out = tmp_path / "out"

        stats = convert_archive(archive, str(out), markdown_output=True, workers=1)

        assert (stats.total, stats.converted) == (2, 2)
        assert sorted(p.name for p in out.iterdir()) == ["x.md", "y.md"]

    def test_converting_in_parallel_gives_the_same_result(self, tmp_path, tei_bytes):
        entries = {f"{i}.tei.xml": tei_bytes for i in range(6)}
        serial_out = tmp_path / "serial"
        parallel_out = tmp_path / "parallel"
        archive = make_zip(tmp_path / "corpus.zip", entries)

        convert_archive(archive, str(serial_out), markdown_output=True, workers=1)
        stats = convert_archive(archive, str(parallel_out), markdown_output=True,
                                workers=3, queue_size=2)

        assert stats.converted == 6
        assert ([p.name for p in sorted(serial_out.iterdir())]
                == [p.name for p in sorted(parallel_out.iterdir())])
        assert ((serial_out / "0.md").read_text() == (parallel_out / "0.md").read_text())

    def test_entries_are_never_extracted_to_disk(self, tmp_path, tei_bytes, monkeypatch):
        """The TEI goes from the archive to the parser without a detour."""
        archive = make_zip(tmp_path / "corpus.zip", {"a.tei.xml": tei_bytes})
        out = tmp_path / "out"

        def refuse(*args, **kwargs):
            raise AssertionError("an entry was written out to disk")

        monkeypatch.setattr(ArchiveReader, "extract_member", refuse)

        stats = convert_archive(archive, str(out), markdown_output=True, workers=1)

        assert stats.converted == 1
        # nothing was left behind next to the archive either
        assert sorted(p.name for p in tmp_path.iterdir()) == ["corpus.zip", "out"]

    def test_no_more_than_a_queue_of_documents_is_read_ahead(self, tmp_path, tei_bytes):
        """The window is what keeps a whole corpus out of memory."""
        archive = make_zip(tmp_path / "corpus.zip",
                           {f"{i}.tei.xml": tei_bytes for i in range(10)})
        queue_size = 3
        counters = {"read": 0, "done": 0, "peak": 0}
        read_entry, record = tei_archive._read_entry, tei_archive._record

        def counting_read(*args, **kwargs):
            entry = read_entry(*args, **kwargs)
            counters["read"] += 1
            counters["peak"] = max(counters["peak"], counters["read"] - counters["done"])
            return entry

        def counting_record(*args, **kwargs):
            counters["done"] += 1
            return record(*args, **kwargs)

        tei_archive._read_entry = counting_read
        tei_archive._record = counting_record
        try:
            stats = convert_archive(archive, str(tmp_path / "out"), markdown_output=True,
                                    workers=2, queue_size=queue_size)
        finally:
            tei_archive._read_entry = read_entry
            tei_archive._record = record

        assert stats.converted == 10
        assert counters["peak"] <= queue_size + 1, "read ahead beyond the queue"

    def test_a_document_that_fails_does_not_stop_the_others(self, tmp_path, tei_bytes):
        archive = make_zip(tmp_path / "corpus.zip", {
            "good.tei.xml": tei_bytes,
            "bad.tei.xml": b"<html></html>",
        })
        out = tmp_path / "out"

        stats = convert_archive(archive, str(out), markdown_output=True, workers=1)

        assert (stats.total, stats.converted, stats.failed) == (2, 1, 1)
        assert [p.name for p in out.iterdir()] == ["good.md"]

    def test_skip_existing_resumes_an_interrupted_run(self, tmp_path, tei_bytes):
        archive = make_zip(tmp_path / "corpus.zip",
                           {"a.tei.xml": tei_bytes, "b.tei.xml": tei_bytes})
        out = tmp_path / "out"
        convert_archive(archive, str(out), markdown_output=True, workers=1)
        (out / "b.md").unlink()

        stats = convert_archive(archive, str(out), markdown_output=True,
                                workers=1, skip_existing=True)

        assert (stats.converted, stats.skipped) == (1, 1)
        assert sorted(p.name for p in out.iterdir()) == ["a.md", "b.md"]

    def test_an_entry_cannot_escape_the_output_directory(self, tmp_path, tei_bytes):
        """A "zip slip" name is written under the output directory like any other."""
        archive = make_zip(tmp_path / "corpus.zip", {"../../evil.tei.xml": tei_bytes})
        out = tmp_path / "out"

        convert_archive(archive, str(out), markdown_output=True, workers=1)

        assert [p.name for p in out.iterdir()] == ["evil.md"]
        assert not (tmp_path.parent / "evil.md").exists()

    def test_the_output_directory_defaults_to_the_archive_name(self, tmp_path, tei_bytes):
        archive = make_zip(tmp_path / "corpus.zip", {"a.tei.xml": tei_bytes})

        convert_archive(archive, None, markdown_output=True, workers=1)

        assert (tmp_path / "corpus" / "a.md").is_file()

    def test_an_archive_without_tei_is_reported_as_empty(self, tmp_path):
        archive = make_zip(tmp_path / "corpus.zip", {"readme.txt": b"nothing here"})

        stats = convert_archive(archive, str(tmp_path / "out"), markdown_output=True)

        assert stats == ArchiveConversionStats(total=0, converted=0, failed=0, skipped=0)

    def test_an_archive_that_cannot_be_opened_raises(self, tmp_path):
        with pytest.raises(OSError):
            convert_archive(str(tmp_path / "missing.zip"), str(tmp_path / "out"),
                            markdown_output=True)

    def test_asking_for_no_format_is_refused(self, tmp_path, tei_bytes):
        archive = make_zip(tmp_path / "corpus.zip", {"a.tei.xml": tei_bytes})

        with pytest.raises(ValueError):
            convert_archive(archive, str(tmp_path / "out"))


class TestArchiveCLIs:
    """Both converters take an archive on --input and fill a directory."""

    def _run(self, module, *args):
        return subprocess.run([sys.executable, "-m", module, *args],
                              capture_output=True, text=True)

    def test_markdown_cli_converts_an_archive(self, tmp_path, tei_bytes):
        archive = make_zip(tmp_path / "corpus.zip",
                           {"a.tei.xml": tei_bytes, "sub/b.tei.xml": tei_bytes})
        out = tmp_path / "md"

        result = self._run("grobid_client.format.TEI2Markdown_cli",
                           "--input", archive, "--output", str(out), "--workers", "1")

        assert result.returncode == 0, result.stderr
        assert "Converted 2 of 2" in result.stdout
        assert sorted(p.name for p in out.iterdir()) == ["a.md", "b.md"]

    def test_json_cli_converts_an_archive(self, tmp_path, tei_bytes):
        archive = make_zip(tmp_path / "corpus.zip", {"a.tei.xml": tei_bytes})
        out = tmp_path / "json"

        result = self._run("grobid_client.format.TEI2LossyJSON_cli",
                           "--input", archive, "--output", str(out), "--workers", "1")

        assert result.returncode == 0, result.stderr
        assert json.loads((out / "a.json").read_text())["biblio"]["title"]

    def test_a_failed_document_makes_the_run_fail(self, tmp_path, tei_bytes):
        archive = make_zip(tmp_path / "corpus.zip",
                           {"good.tei.xml": tei_bytes, "bad.tei.xml": b"<html/>"})

        result = self._run("grobid_client.format.TEI2Markdown_cli",
                           "--input", archive, "--output", str(tmp_path / "md"),
                           "--workers", "1")

        assert result.returncode == 1
        assert "Errors: 1" in result.stdout

    def test_an_unreadable_archive_is_reported(self, tmp_path):
        result = self._run("grobid_client.format.TEI2Markdown_cli",
                           "--input", str(tmp_path / "missing.zip"),
                           "--output", str(tmp_path / "md"))

        assert result.returncode == 1
        assert "Could not read archive" in result.stderr
