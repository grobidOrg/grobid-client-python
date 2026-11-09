"""
Table cells must not break the Markdown table they are rendered into.

A '|' in a cell ended the cell early and a line break ended the row, and
a row wider than the first one lost its extra cells in renderers, which
size the table by the separator row.
"""
import io

from grobid_client.format.TEI2Markdown import TEI2MarkdownConverter


def _table_lines(rows_xml):
    tei = ('<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text><body><div><p>x</p>'
           f'<figure type="table"><head>T</head><table>{rows_xml}</table></figure>'
           '</div></body></text></TEI>')
    md = TEI2MarkdownConverter().convert_tei_file(io.StringIO(tei))
    return [line for line in md.splitlines() if line.startswith("|")]


def _cell_count(line):
    # unescaped pipes delimit the cells
    return line.replace("\\|", "").count("|") - 1


def test_pipe_in_a_cell_is_escaped():
    lines = _table_lines('<row><cell>a|b</cell><cell>c</cell></row><row><cell>1</cell><cell>2</cell></row>')
    assert lines[0] == "| a\\|b | c |"
    assert {_cell_count(line) for line in lines} == {2}


def test_line_break_in_a_cell_stays_on_one_row():
    lines = _table_lines('<row><cell>first\nsecond</cell><cell>c</cell></row>')
    assert lines[0] == "| first second | c |"
    assert len(lines) == 2  # the row and the separator


def test_ragged_rows_are_padded_to_the_widest():
    lines = _table_lines('<row><cell>h</cell></row><row><cell>1</cell><cell>2</cell><cell>3</cell></row>')
    assert {_cell_count(line) for line in lines} == {3}
    assert lines[0] == "| h |  |  |"
