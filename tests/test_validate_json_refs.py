"""
validate_json_refs must report the files that hold bad references.

A file's 'valid' flag was never cleared, so a file with invalid references
was counted as valid ("Files: 2/2 valid") and its errors were left out of
the report. Only the top level of the directory was searched, while the
client writes the output of a recursive run into subdirectories.
"""
import copy
import json
import os
import sys
import subprocess

import pytest

from grobid_client.format.TEI2LossyJSON import TEI2LossyJSONConverter
from grobid_client.format.validate_json_refs import JSONReferenceValidator
from tests.resources import TEST_DATA_PATH

SAMPLE = os.path.join(TEST_DATA_PATH, '0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml')


@pytest.fixture(scope="module")
def good_document():
    return TEI2LossyJSONConverter().convert_tei_file(SAMPLE)


def _break_one_ref(document):
    broken = copy.deepcopy(document)
    passage = next(p for p in broken['body_text'] if p.get('refs'))
    passage['refs'][0]['offset_start'] += 1
    return broken


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding='utf-8')


def test_good_document_is_valid(tmp_path, good_document):
    _write(tmp_path / 'good.json', good_document)
    results = JSONReferenceValidator().validate_directory(str(tmp_path))
    assert results['valid_files'] == 1 and results['invalid_files'] == 0
    assert results['total_refs'] > 0 and results['invalid_refs'] == 0


def test_file_with_a_bad_reference_is_invalid(tmp_path, good_document):
    _write(tmp_path / 'good.json', good_document)
    _write(tmp_path / 'bad.json', _break_one_ref(good_document))
    validator = JSONReferenceValidator()
    results = validator.validate_directory(str(tmp_path))
    assert (results['valid_files'], results['invalid_files']) == (1, 1)
    assert results['invalid_refs'] == 1
    assert any('Text mismatch' in error for error in results['errors'])
    assert 'Text mismatch' in validator.generate_report()


def test_subdirectories_are_searched(tmp_path, good_document):
    _write(tmp_path / 'top.json', good_document)
    _write(tmp_path / 'a' / 'deep' / 'bad.json', _break_one_ref(good_document))
    results = JSONReferenceValidator().validate_directory(str(tmp_path))
    assert results['total_files'] == 2
    assert results['invalid_files'] == 1


@pytest.mark.parametrize("content", ['[1, 2]', '"text"', '{"biblio": {"abstract": null}}'])
def test_unexpected_json_does_not_abort_the_run(tmp_path, good_document, content):
    _write(tmp_path / 'good.json', good_document)
    (tmp_path / 'odd.json').write_text(content)
    results = JSONReferenceValidator().validate_directory(str(tmp_path))
    assert results['total_files'] == 2
    assert results['valid_files'] >= 1


def test_cli_exit_code_and_summary(tmp_path, good_document):
    _write(tmp_path / 'good.json', good_document)
    _write(tmp_path / 'sub' / 'bad.json', _break_one_ref(good_document))
    out = subprocess.run([sys.executable, '-m', 'grobid_client.format.validate_json_refs', str(tmp_path)],
                         capture_output=True, text=True)
    assert out.returncode == 1
    assert 'Files: 1/2 valid' in out.stdout

    report = tmp_path / 'report.json'
    out = subprocess.run([sys.executable, '-m', 'grobid_client.format.validate_json_refs',
                          str(tmp_path / 'good.json'), '--output', str(report)],
                         capture_output=True, text=True)
    assert out.returncode == 0
    assert json.loads(report.read_text())['summary']['valid_files'] == 1
