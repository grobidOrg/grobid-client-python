"""
TEI2LossyJSONConverter.process_directory, serial and parallel.

The parallel worker built a default converter, so validate_refs was
ignored, and it first read each file with open(path, 'r') -- the locale's
encoding, not UTF-8 -- only to throw the result away. Where the locale is
not UTF-8 (cp1252 on Windows) bytes such as those of a closing double
quote cannot be decoded, and every document holding one came back None.
"""
import builtins
import concurrent.futures
import os
import shutil

import pytest

from grobid_client.format import TEI2LossyJSON
from grobid_client.format.TEI2LossyJSON import TEI2LossyJSONConverter, _convert_file_worker
from tests.resources import TEST_DATA_PATH

SAMPLES = [
    os.path.join(TEST_DATA_PATH, '0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml'),
    os.path.join(TEST_DATA_PATH, 'refs_offsets', '10.1038_s41598-023-32039-z.grobid.tei.xml'),
]


@pytest.fixture
def tei_dir(tmp_path):
    (tmp_path / 'sub').mkdir()
    shutil.copy(SAMPLES[0], tmp_path / 'a.tei.xml')
    shutil.copy(SAMPLES[1], tmp_path / 'sub' / 'b.tei.xml')
    (tmp_path / 'ignored.json').write_text('{}')
    return tmp_path


def _texts(document):
    return [p['text'] for p in document['body_text']]


def _by_name(items):
    return {item['path'].name: item['document'] for item in items}


def test_serial_and_parallel_agree(tei_dir):
    converter = TEI2LossyJSONConverter()
    serial = _by_name(converter.process_directory(tei_dir, parallel=False))
    parallel = _by_name(converter.process_directory(tei_dir, parallel=True, workers=2))
    assert set(serial) == set(parallel) == {'a.tei.xml', 'b.tei.xml'}
    for name in serial:
        assert parallel[name] is not None
        assert _texts(parallel[name]) == _texts(serial[name])


def test_parallel_worker_gets_the_converter_settings(tei_dir, monkeypatch):
    calls = []

    def spy(path, validate_refs=True):
        calls.append(validate_refs)
        return {'body_text': []}

    # threads instead of processes, so the spy sees the calls
    monkeypatch.setattr(TEI2LossyJSON, 'ProcessPoolExecutor', concurrent.futures.ThreadPoolExecutor)
    monkeypatch.setattr(TEI2LossyJSON, '_convert_file_worker', spy)
    list(TEI2LossyJSONConverter(validate_refs=False).process_directory(tei_dir, parallel=True, workers=2))
    assert calls == [False, False]


def test_worker_reads_files_as_utf8(monkeypatch):
    real_open = builtins.open
    text_opens = []

    def recording_open(file, mode='r', *args, **kwargs):
        if 'b' not in mode:
            text_opens.append(kwargs.get('encoding') or (args[1] if len(args) > 1 else None))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', recording_open)
    assert _convert_file_worker(SAMPLES[0]) is not None
    assert text_opens and all(encoding == 'utf-8' for encoding in text_opens)
