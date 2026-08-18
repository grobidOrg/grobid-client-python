"""
Read TEI XML from wherever it comes from.

The converters in this package take their input from a path, from a stream, or
straight from memory: a client that got the TEI from a GROBID response, an
object store or an archive should not have to write it out to disk just to have
a converter read it back. This module is the single place that knows how to turn
any of those into a parsed document, so both converters accept the same things
and agree on what they mean.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO, Any, Union, cast

from bs4 import BeautifulSoup

# Anything the converters accept as TEI input: a path, a stream over the
# document, the markup itself, or a document parsed once already.
TEISource = Union[str, bytes, bytearray, memoryview, Path, IO[str], IO[bytes], BeautifulSoup]

# The conventional stand-in for "read the document from stdin" on a command line.
STDIN_ALIAS = '-'

# How much of a stream's name to keep in a log line.
_LABEL_MAX = 120

# How far into a string to look for the tag that gives it away as markup, and
# what may come before it - a byte-order mark included, which survives the
# decoding of a response body.
_MARKUP_PROBE = 256
_LEADING_NOISE = '\ufeff \t\r\n\f\v'


def load_tei_soup(source: TEISource) -> BeautifulSoup:
    """Parse TEI markup from ``source`` into a BeautifulSoup document.

    A ``BeautifulSoup`` passed in is returned as it is, so converting one
    document to several formats only pays for the parsing once. The converters
    never modify the document, so sharing it between them is safe.
    """
    if isinstance(source, BeautifulSoup):
        return source

    return BeautifulSoup(read_tei(source), 'xml')


def read_tei(source: TEISource) -> Union[str, bytes]:
    """Return the TEI markup itself, reading it from disk or from a stream if needed.

    Markup that comes from disk is handed over as bytes so that the encoding
    declared in the XML prolog is the one that gets used, rather than an assumed
    UTF-8 - Pub2TEI output, for one, is not always UTF-8.
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)

    if isinstance(source, str) and is_tei_markup(source):
        return source

    read = getattr(source, 'read', None)
    if callable(read):
        # A stream: the caller owns it and gets to close it. Whether it reads as
        # text or as bytes, the parser takes both. (Asking whether `read` is
        # callable rather than merely present: on a bs4 element any attribute
        # name answers, with the tag of that name or None.)
        return cast(Union[str, bytes], read())

    with open(source, 'rb') as tei_file:  # type: ignore[arg-type]
        return tei_file.read()


def is_tei_markup(source: str) -> bool:
    """Tell a document from the path to one: only markup starts with a tag.

    Only the head of the string is looked at, since a whole document would be
    copied by stripping it, and no path starts with hundreds of blanks.
    """
    return source[:_MARKUP_PROBE].lstrip(_LEADING_NOISE)[:1] == '<'


def resolve_cli_input(value: Union[str, Path]) -> TEISource:
    """Turn what a --input option carries into a TEI source.

    ``-`` is stdin, which lets the converters sit in a pipe rather than only at
    the end of one::

        cat paper.tei.xml | python -m grobid_client.format.TEI2Markdown_cli -i -

    stdin is taken as a binary stream, so a TEI that declares an encoding other
    than UTF-8 arrives intact.
    """
    if str(value) == STDIN_ALIAS:
        return getattr(sys.stdin, 'buffer', sys.stdin)

    return Path(value)


def describe_source(source: Any) -> str:
    """Name a source for a log line, without spilling a whole document into it."""
    if isinstance(source, BeautifulSoup):
        return "<parsed TEI>"

    if isinstance(source, (bytes, bytearray, memoryview)):
        return "<in-memory TEI>"

    if isinstance(source, str):
        return "<in-memory TEI>" if is_tei_markup(source) else source

    if isinstance(source, (Path, os.PathLike)):
        return os.fspath(source)

    name = getattr(source, 'name', None)
    if isinstance(name, str) and name:
        return name[:_LABEL_MAX]

    return f"<{type(source).__name__} stream>"
