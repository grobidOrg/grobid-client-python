"""
Command-line input checks.

A mistyped --input logged "Input path does not exist" and exited 0, so a
script could not tell it from a run with nothing left to do. And
processCitationPatentST36 took PDFs as inputs as well as its ST.36 XML:
GROBID answers a PDF there with 204 No Content, which left an error
marker per PDF (seen against GROBID 0.9.2).
"""
import os
from unittest.mock import Mock

import pytest

from grobid_client import grobid_client as gc_module
from grobid_client.grobid_client import GrobidClient


def _run_main(monkeypatch, *argv):
    client = Mock()
    monkeypatch.setattr(gc_module, "GrobidClient", Mock(return_value=client))
    monkeypatch.setattr("sys.argv", ["grobid_client", *argv])
    code = None
    try:
        gc_module.main()
    except SystemExit as exc:
        code = exc.code
    return code, client


def test_missing_input_exits_1(monkeypatch, tmp_path):
    code, client = _run_main(monkeypatch, "--input", str(tmp_path / "typo"), "processFulltextDocument")
    assert code == 1
    client.process_paths.assert_not_called()
    assert any("does not exist" in str(c) for c in client.logger.error.call_args_list)


@pytest.mark.parametrize("make_input", [
    lambda d: str(d),                                  # a directory
    lambda d: str(d / "*.pdf"),                        # a glob, resolved later
    lambda d: "s3://bucket/prefix/",                   # remote, resolved later
    lambda d: "~/" + os.path.relpath(str(d), os.path.expanduser("~")),  # ~ is expanded
])
def test_existing_or_deferred_inputs_are_processed(monkeypatch, tmp_path, make_input):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    folder = home / "papers"
    folder.mkdir(parents=True)
    code, client = _run_main(monkeypatch, "--input", make_input(folder), "processFulltextDocument")
    assert code is None
    client.process_paths.assert_called_once()


@pytest.fixture
def client():
    return GrobidClient(check_server=False)


def test_st36_takes_only_xml(client):
    assert client._is_eligible_input("patent.xml", "processCitationPatentST36")
    assert client._is_eligible_input("PATENT.XML", "processCitationPatentST36")
    assert not client._is_eligible_input("patent.pdf", "processCitationPatentST36")


@pytest.mark.parametrize("service", ["processFulltextDocument", "processHeaderDocument", "processReferences",
                                     "processCitationPatentPDF"])
def test_pdf_services_take_only_pdfs(client, service):
    assert client._is_eligible_input("paper.pdf", service)
    assert client._is_eligible_input("PAPER.PDF", service)
    assert not client._is_eligible_input("patent.xml", service)
