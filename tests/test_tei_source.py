"""
Unit tests for how the converters take their TEI in: a path, a stream, memory,
or a document parsed once and shared.
"""
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from grobid_client.format.TEI2LossyJSON import TEI2LossyJSONConverter
from grobid_client.format.TEI2Markdown import TEI2MarkdownConverter
from grobid_client.format.tei_source import (
    describe_source,
    is_tei_markup,
    load_tei_soup,
    read_tei,
    resolve_cli_input,
)
from tests.resources import TEST_DATA_PATH

SAMPLE_TEI = Path(TEST_DATA_PATH) / "0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml"

# Passage identifiers are random, so two conversions of the same document only
# compare equal once they are taken out.
RANDOM_ID = re.compile(r'"(p|f|t)_[0-9a-f]{8}"')


def _comparable(document):
    return RANDOM_ID.sub('"id"', json.dumps(document, sort_keys=True))


@pytest.fixture(scope="module")
def tei_bytes():
    return SAMPLE_TEI.read_bytes()


class TestTEISources:
    """Every way of handing the same TEI over produces the same conversion."""

    @pytest.fixture(params=["path-str", "path", "bytes", "markup", "binary-stream", "text-stream", "soup"])
    def source(self, request, tei_bytes):
        return {
            "path-str": lambda: str(SAMPLE_TEI),
            "path": lambda: SAMPLE_TEI,
            "bytes": lambda: tei_bytes,
            "markup": lambda: tei_bytes.decode("utf-8"),
            "binary-stream": lambda: io.BytesIO(tei_bytes),
            "text-stream": lambda: io.StringIO(tei_bytes.decode("utf-8")),
            "soup": lambda: load_tei_soup(tei_bytes),
        }[request.param]()

    def test_json_conversion_is_the_same_from_any_source(self, source, tei_bytes):
        from_source = TEI2LossyJSONConverter().convert_tei_file(source, stream=False)
        from_path = TEI2LossyJSONConverter().convert_tei_file(SAMPLE_TEI, stream=False)

        assert _comparable(from_source) == _comparable(from_path)

    def test_markdown_conversion_is_the_same_from_any_source(self, source):
        from_source = TEI2MarkdownConverter().convert_tei_file(source)
        from_path = TEI2MarkdownConverter().convert_tei_file(SAMPLE_TEI)

        assert from_source == from_path

    def test_streamed_passages_come_out_of_a_stream_too(self, tei_bytes):
        passages = list(TEI2LossyJSONConverter().convert_tei_file(io.BytesIO(tei_bytes), stream=True))

        assert len(passages) > 0
        assert _comparable(passages) == _comparable(
            list(TEI2LossyJSONConverter().convert_tei_file(SAMPLE_TEI, stream=True)))

    def test_a_parsed_document_is_shared_by_both_converters(self):
        """One parse serves both formats - and neither converter modifies it."""
        soup = load_tei_soup(SAMPLE_TEI)

        document = TEI2LossyJSONConverter().convert_tei_file(soup, stream=False)
        markdown = TEI2MarkdownConverter().convert_tei_file(soup)

        assert _comparable(document) == _comparable(
            TEI2LossyJSONConverter().convert_tei_file(SAMPLE_TEI, stream=False))
        assert markdown == TEI2MarkdownConverter().convert_tei_file(SAMPLE_TEI)

    def test_markup_is_told_apart_from_a_path(self):
        assert is_tei_markup("<TEI/>")
        assert is_tei_markup("\n  <?xml version='1.0'?><TEI/>")
        assert is_tei_markup("\ufeff<TEI/>"), "a byte-order mark survives decoding a response body"
        assert not is_tei_markup("path/to/file.tei.xml")
        assert not is_tei_markup("")

    def test_a_byte_order_mark_does_not_hide_the_document(self, tei_bytes):
        with_bom = "\ufeff" + tei_bytes.decode("utf-8")

        assert (TEI2MarkdownConverter().convert_tei_file(with_bom)
                == TEI2MarkdownConverter().convert_tei_file(SAMPLE_TEI))

    def test_the_declared_encoding_wins_over_an_assumed_utf8(self, tmp_path):
        """A TEI is not always UTF-8, and its prolog says which one it is."""
        tei = ('<?xml version="1.0" encoding="ISO-8859-1"?>'
               '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader>'
               '<title type="main" level="a">Caf\xe9 r\xe9sum\xe9</title>'
               '</teiHeader><text><body/></text></TEI>')
        latin1_file = tmp_path / "latin1.tei.xml"
        latin1_file.write_bytes(tei.encode("ISO-8859-1"))

        assert "Café résumé" in TEI2MarkdownConverter().convert_tei_file(latin1_file)

    def test_a_missing_file_raises_instead_of_passing_for_a_broken_tei(self):
        with pytest.raises(OSError):
            TEI2MarkdownConverter().convert_tei_file("/nonexistent/file.tei.xml")

        with pytest.raises(OSError):
            TEI2LossyJSONConverter().convert_tei_file("/nonexistent/file.tei.xml", stream=False)

    def test_something_that_is_not_a_tei_converts_to_nothing(self):
        assert TEI2MarkdownConverter().convert_tei_file("<html></html>") is None
        assert TEI2LossyJSONConverter().convert_tei_file("<html></html>", stream=False) is None
        assert list(TEI2LossyJSONConverter().convert_tei_file("<html></html>", stream=True)) == []

    def test_a_stream_is_left_open_for_its_owner_to_close(self, tei_bytes):
        stream = io.BytesIO(tei_bytes)

        TEI2MarkdownConverter().convert_tei_file(stream)

        assert not stream.closed

    def test_read_tei_returns_the_markup_itself(self, tei_bytes):
        assert read_tei(SAMPLE_TEI) == tei_bytes
        assert read_tei(io.BytesIO(tei_bytes)) == tei_bytes
        assert read_tei(tei_bytes) == tei_bytes
        assert read_tei("<TEI/>") == "<TEI/>"


class TestSourceDescriptions:
    """Log lines name a source without spilling a whole document into them."""

    def test_a_path_is_named_by_its_path(self):
        assert describe_source("dir/file.tei.xml") == "dir/file.tei.xml"
        assert describe_source(Path("dir/file.tei.xml")) == os.path.join("dir", "file.tei.xml")

    def test_a_document_in_memory_is_not_printed(self, tei_bytes):
        assert describe_source(tei_bytes.decode("utf-8")) == "<in-memory TEI>"
        assert describe_source(tei_bytes) == "<in-memory TEI>"
        assert describe_source(load_tei_soup(tei_bytes)) == "<parsed TEI>"

    def test_a_stream_is_named_after_itself(self, tmp_path):
        tei_file = tmp_path / "named.tei.xml"
        tei_file.write_text("<TEI/>")

        with open(tei_file, "rb") as stream:
            assert describe_source(stream) == str(tei_file)

        assert describe_source(io.BytesIO(b"<TEI/>")) == "<BytesIO stream>"


class TestConverterCLIs:
    """The converters read a path, or stdin, and can sit in a pipe."""

    def _run(self, module, tei_bytes=None, *args):
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            input=tei_bytes, capture_output=True, check=True).stdout

    def test_markdown_from_stdin_is_what_a_path_gives(self, tei_bytes):
        module = "grobid_client.format.TEI2Markdown_cli"

        assert self._run(module, tei_bytes) == self._run(module, None, "--input", str(SAMPLE_TEI))

    def test_json_from_stdin_is_what_a_path_gives(self, tei_bytes):
        module = "grobid_client.format.TEI2LossyJSON_cli"

        from_stdin = json.loads(self._run(module, tei_bytes))
        from_path = json.loads(self._run(module, None, "--input", str(SAMPLE_TEI)))

        assert _comparable(from_stdin) == _comparable(from_path)

    @pytest.mark.parametrize("module", [
        "grobid_client.format.TEI2Markdown_cli",
        "grobid_client.format.TEI2LossyJSON_cli",
    ])
    def test_a_missing_input_file_is_reported(self, module):
        result = subprocess.run(
            [sys.executable, "-m", module, "--input", "/nonexistent/file.tei.xml"],
            capture_output=True, text=True)

        assert result.returncode == 1
        assert "does not exist" in result.stderr

    def test_stdin_can_be_named_explicitly(self, tei_bytes):
        result = subprocess.run(
            [sys.executable, "-m", "grobid_client.format.TEI2Markdown_cli", "-i", "-"],
            input=tei_bytes, capture_output=True, check=True)

        assert result.stdout.startswith(b"# Multi-contact functional electrical stimulation")

    def test_resolve_cli_input(self):
        assert resolve_cli_input("file.tei.xml") == Path("file.tei.xml")
        assert hasattr(resolve_cli_input("-"), "read")
