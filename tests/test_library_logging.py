"""
Importing the package must leave the application's logging alone.

Both converter modules called logging.basicConfig(level=INFO) at import
time. Importing grobid_client (which imports TEI2LossyJSON) installed a
root handler at INFO in any program using the library, and turned the
format CLIs' own setup_logging() into a no-op, since basicConfig does
nothing once the root logger has a handler. TEI2LossyJSON's logger also
did not propagate, so an application's handlers never saw its warnings.
"""
import io
import logging
import subprocess
import sys

import pytest

from grobid_client.format.TEI2LossyJSON import TEI2LossyJSONConverter
from grobid_client.format.TEI2Markdown import TEI2MarkdownConverter


@pytest.mark.parametrize("module", [
    "grobid_client.grobid_client",
    "grobid_client.format.TEI2LossyJSON",
    "grobid_client.format.TEI2Markdown",
])
def test_import_does_not_configure_the_root_logger(module):
    code = (f"import logging, {module}; r = logging.getLogger(); "
            "print(len(r.handlers), logging.getLevelName(r.level))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["0", "WARNING"]


@pytest.mark.parametrize("converter", [TEI2LossyJSONConverter, TEI2MarkdownConverter])
def test_converter_warnings_reach_the_application_handlers(converter, caplog):
    with caplog.at_level(logging.WARNING):
        assert converter().convert_tei_file(io.StringIO("")) is None
    assert any("not well-formed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("name", ["TEI2LossyJSON", "TEI2Markdown"])
def test_format_cli_applies_its_own_log_format(name, tmp_path):
    empty = tmp_path / "empty.tei.xml"
    empty.write_text("")
    out = subprocess.run([sys.executable, "-m", "grobid_client.format", name, "--input", str(empty)],
                         capture_output=True, text=True)
    assert out.returncode == 1
    # setup_logging()'s format: "<date> <time> - LEVEL - message"
    assert " - ERROR - Failed to convert" in out.stderr
