"""
The format converter command lines: `python -m grobid_client.format <converter>`.

These entry points had no tests. `--help` on the menu was reported as an
unknown converter, and the examples in each converter's --help ran
`python -m grobid_client.format.TEI2Markdown`, which imports the module
and exits 0 without converting anything.
"""
import json
import os
import re
import subprocess
import sys

import pytest

from tests.resources import TEST_DATA_PATH

SAMPLE = os.path.join(TEST_DATA_PATH, '0046d83a-edd6-4631-b57c-755cdcce8b7f.tei.xml')
TITLE = 'Multi-contact functional electrical stimulation for hand opening'


def run(*args):
    return subprocess.run([sys.executable, '-m', 'grobid_client.format', *args],
                          capture_output=True, text=True, timeout=120)


class TestMenu:
    def test_without_arguments_prints_the_menu_and_fails(self):
        out = run()
        assert out.returncode == 1
        assert 'Available converters' in out.stdout

    @pytest.mark.parametrize('flag', ['--help', '-h'])
    def test_help_prints_the_menu(self, flag):
        out = run(flag)
        assert out.returncode == 0
        assert 'Available converters' in out.stdout
        assert 'Unknown converter' not in out.stdout + out.stderr

    def test_unknown_converter(self):
        out = run('TEI2Html')
        assert out.returncode == 1
        assert 'Unknown converter: TEI2Html' in out.stderr


class TestConverters:
    def test_json_to_file(self, tmp_path):
        target = tmp_path / 'nested' / 'out.json'
        out = run('TEI2LossyJSON', '--input', SAMPLE, '--output', str(target))
        assert out.returncode == 0, out.stderr
        document = json.loads(target.read_text(encoding='utf-8'))
        assert document['biblio']['title'].startswith(TITLE)
        assert document['body_text']

    def test_json_to_stdout(self):
        out = run('TEI2LossyJSON', '--input', SAMPLE)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)['biblio']['title'].startswith(TITLE)

    def test_markdown_to_file(self, tmp_path):
        target = tmp_path / 'out.md'
        out = run('TEI2Markdown', '--input', SAMPLE, '--output', str(target))
        assert out.returncode == 0, out.stderr
        assert target.read_text(encoding='utf-8').startswith(f'# {TITLE}')

    def test_markdown_to_stdout(self):
        out = run('TEI2Markdown', '--input', SAMPLE)
        assert out.returncode == 0, out.stderr
        assert out.stdout.startswith(f'# {TITLE}')

    @pytest.mark.parametrize('converter', ['TEI2LossyJSON', 'TEI2Markdown'])
    def test_missing_input(self, converter, tmp_path):
        out = run(converter, '--input', str(tmp_path / 'nope.tei.xml'))
        assert out.returncode == 1
        assert 'does not exist' in out.stderr

    @pytest.mark.parametrize('converter', ['TEI2LossyJSON', 'TEI2Markdown'])
    def test_directory_input(self, converter, tmp_path):
        out = run(converter, '--input', str(tmp_path))
        assert out.returncode == 1
        assert 'not a file' in out.stderr

    @pytest.mark.parametrize('converter', ['TEI2LossyJSON', 'TEI2Markdown'])
    def test_not_tei_fails_without_writing(self, converter, tmp_path):
        source = tmp_path / 'notes.xml'
        source.write_text('hello world')
        target = tmp_path / 'out'
        out = run(converter, '--input', str(source), '--output', str(target))
        assert out.returncode == 1
        assert not target.exists()

    @pytest.mark.parametrize('converter', ['TEI2LossyJSON', 'TEI2Markdown'])
    def test_help_examples_name_a_working_command(self, converter, tmp_path):
        out = run(converter, '--help')
        assert out.returncode == 0
        examples = re.findall(r'^\s*(python -m \S+(?: \S+)?) --input', out.stdout, re.MULTILINE)
        assert examples
        for example in set(examples):
            module_args = example.split()[2:]  # drop "python -m"
            result = subprocess.run([sys.executable, '-m', *module_args, '--input', SAMPLE],
                                    capture_output=True, text=True, timeout=120)
            assert result.returncode == 0, example
            assert TITLE in result.stdout, f"{example} printed nothing"
