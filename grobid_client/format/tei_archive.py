"""
Convert the TEI documents held in an archive, without unpacking it.

Corpora travel as zips and tarballs, and a converted corpus is wanted as a
directory of Markdown or JSON. Between the two there is no reason for the TEI
to touch the disk: entries are read out of the archive into memory one by one,
converted there, and only the results are written. A zip in an object store is
range-streamed, so a remote corpus is converted without downloading it either.

This is the same principle the client applies to PDFs on their way to GROBID
(see :meth:`GrobidClient.process_archive`), with more room to work in: a TEI is
a fraction of the size of the PDF it came from, and converting one is local
work rather than a server round-trip.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from ..archive import ArchiveReader, archive_stem, is_s3, looks_like_archive, s3_basename
from ..fileio import write_atomic
from .TEI2LossyJSON import TEI2LossyJSONConverter
from .TEI2Markdown import TEI2MarkdownConverter
from .tei_source import load_tei_soup

logger = logging.getLogger(__name__)

# What an archive entry has to be named to be taken for a TEI document. Order
# matters: the longest suffix must come first, so that the stem of
# "paper.grobid.tei.xml" is "paper" and not "paper.grobid.tei".
TEI_SUFFIXES = (".grobid.tei.xml", ".tei.xml", ".tei", ".xml")

# Parsing is CPU-bound and releases nothing to wait on, so the useful number of
# workers is the number of cores - capped, since past a point the memory of one
# parsed document per worker is what the machine runs out of first.
MAX_DEFAULT_WORKERS = 8


@dataclass
class ArchiveConversionStats:
    """What became of the TEI entries of an archive."""

    total: int = 0
    converted: int = 0
    failed: int = 0
    skipped: int = 0
    empty: int = 0


def is_tei_member(name: str) -> bool:
    """Return True if an archive entry name is that of a TEI document."""
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in TEI_SUFFIXES)


def tei_stem(name: str) -> str:
    """The name an entry's outputs are built on: its basename, TEI suffix off."""
    basename = os.path.basename(name.replace("\\", "/"))
    lower = basename.lower()
    for suffix in TEI_SUFFIXES:
        if lower.endswith(suffix):
            return basename[:-len(suffix)]
    return os.path.splitext(basename)[0]


def default_output_dir(archive_path: str) -> str:
    """Where an archive's conversions go when the caller names no directory.

    Named after the archive, the way the client names the output directory of an
    archive of PDFs: ``corpus.zip`` becomes ``corpus/``.
    """
    if is_s3(archive_path):
        return archive_stem(s3_basename(archive_path))
    return archive_stem(archive_path)


def default_workers() -> int:
    return max(1, min(MAX_DEFAULT_WORKERS, os.cpu_count() or 1))


def default_queue_size(workers: int) -> int:
    """How many documents to hold in memory at once.

    This bounds the unparsed TEI waiting to be picked up, which is the cheap
    part: a few hundred KB each, against the ten-to-fifty-fold that each worker's
    parsed document costs while it is being converted. So the queue is deeper
    than the one the client keeps for PDFs - the headroom is there - while peak
    memory stays governed by the number of workers.
    """
    return max(16, 4 * workers)


def format_summary(stats: ArchiveConversionStats, source: str) -> str:
    """The end-of-run summary, in the shape a calling script can parse.

    One fact per line, each starting with its own word and leading with the
    number, so `sed -n 's/^Errors: \\([0-9]*\\).*/\\1/p'` and friends keep
    working. Batch scripts read these counts to decide what to do with the
    archive they just converted.
    """
    lines = [f"Converted {stats.converted} of {stats.total} TEI file(s) from {source}"]
    if stats.skipped:
        lines.append(f"Skipped: {stats.skipped} (outputs already existed)")
    if stats.empty:
        lines.append(f"Empty: {stats.empty} (zero-length entries skipped)")
    if stats.failed:
        lines.append(f"Errors: {stats.failed}")
    return "\n".join(lines)


def convert_archive(
        archive_path: str,
        output_dir: Optional[str] = None,
        json_output: bool = False,
        markdown_output: bool = False,
        workers: Optional[int] = None,
        queue_size: Optional[int] = None,
        skip_existing: bool = False,
        verbose: bool = False,
        log: Optional[logging.Logger] = None
) -> ArchiveConversionStats:
    """Convert every TEI document in an archive, in memory.

    Args:
        archive_path: a local zip/tarball, or an ``s3://`` zip
        output_dir: where the conversions go; defaults to the archive's name
        json_output, markdown_output: what to write for each document. Asking
            for both costs one parse, not two.
        workers: processes converting in parallel (1 converts in this process)
        queue_size: documents held in memory at once, waiting for a worker
        skip_existing: leave entries whose outputs are already there, so an
            interrupted run can be resumed
        verbose: log every entry as it is read

    A zero-length entry is counted apart, as empty rather than failed: real
    corpora carry a few of those, and nothing was written for them - there is
    nothing to fix.

    Returns:
        What became of the entries. A document that cannot be converted is
        counted and logged, never raised: one unusable TEI does not end the
        run. An archive that cannot be opened at all does raise - there is
        nothing to report on.
    """
    log = log or logger
    stats = ArchiveConversionStats()

    if not json_output and not markdown_output:
        raise ValueError("Nothing to convert to: ask for json_output, markdown_output, or both")

    workers = workers if workers is not None else default_workers()
    if workers < 1:
        raise ValueError(f"workers must be at least 1, got {workers}")
    queue_size = queue_size if queue_size is not None else default_queue_size(workers)
    if queue_size < 1:
        raise ValueError(f"queue_size must be at least 1, got {queue_size}")

    if output_dir is None:
        output_dir = default_output_dir(archive_path)

    with ArchiveReader(archive_path, log=log) as archive:
        members = [name for name in archive.names if is_tei_member(os.path.basename(name))]
        stats.total = len(members)
        if not members:
            log.warning(f"No TEI files found in archive {archive_path}")
            return stats

        os.makedirs(os.path.expanduser(output_dir), exist_ok=True)

        pending_members = []
        for name in members:
            if skip_existing and _outputs_exist(name, output_dir, json_output, markdown_output):
                log.debug(f"Outputs of {name} already exist, skipping")
                stats.skipped += 1
                continue
            pending_members.append(name)

        if workers == 1:
            _convert_serially(archive, pending_members, output_dir, json_output,
                              markdown_output, verbose, log, stats)
        else:
            _convert_in_parallel(archive, pending_members, output_dir, json_output,
                                 markdown_output, workers, queue_size, verbose, log, stats)

    return stats


def _convert_serially(
        archive: ArchiveReader,
        members: List[str],
        output_dir: str,
        json_output: bool,
        markdown_output: bool,
        verbose: bool,
        log: logging.Logger,
        stats: ArchiveConversionStats
) -> None:
    """Convert one entry at a time, holding a single document in memory."""
    for name in members:
        entry = _read_entry(archive, name, verbose, log, stats)
        if entry is None:
            continue
        _record(_convert_document(*entry, output_dir, json_output, markdown_output), log, stats)


def _convert_in_parallel(
        archive: ArchiveReader,
        members: List[str],
        output_dir: str,
        json_output: bool,
        markdown_output: bool,
        workers: int,
        queue_size: int,
        verbose: bool,
        log: logging.Logger,
        stats: ArchiveConversionStats
) -> None:
    """Convert in a pool of processes, reading ahead by at most queue_size.

    Reading runs ahead of converting so no worker waits on the archive, but only
    so far: the window is what keeps the whole corpus from being pulled into
    memory when the workers are the slow end, which they are.
    """
    with ProcessPoolExecutor(max_workers=workers) as pool:
        in_flight: set = set()
        for name in members:
            if len(in_flight) >= queue_size:
                done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    _record(future.result(), log, stats)

            entry = _read_entry(archive, name, verbose, log, stats)
            if entry is None:
                continue
            in_flight.add(pool.submit(_convert_document, *entry, output_dir,
                                      json_output, markdown_output))

        for future in in_flight:
            _record(future.result(), log, stats)


def _read_entry(
        archive: ArchiveReader,
        name: str,
        verbose: bool,
        log: logging.Logger,
        stats: ArchiveConversionStats
) -> Optional[Tuple[str, bytes]]:
    if verbose:
        log.info(f"Reading {name} from {archive.path}")
    try:
        entry = archive.read_member_data(name)
    except Exception as e:
        log.error(f"Failed to read {name} from {archive.path}: {str(e)}")
        stats.failed += 1
        return None

    if entry is None:
        stats.failed += 1
        return None

    if not entry[1]:
        # A zero-length entry is not a broken document, it is no document.
        log.debug(f"{name} is empty, skipping")
        stats.empty += 1
        return None

    return entry


def _outputs_exist(name: str, output_dir: str, json_output: bool, markdown_output: bool) -> bool:
    """True when every output asked for is already on disk for this entry."""
    stem = os.path.join(os.path.expanduser(output_dir), tei_stem(name))
    if json_output and not os.path.isfile(stem + ".json"):
        return False
    if markdown_output and not os.path.isfile(stem + ".md"):
        return False
    return True


def _convert_document(
        name: str,
        data: bytes,
        output_dir: str,
        json_output: bool,
        markdown_output: bool
) -> Tuple[str, List[str], Optional[str]]:
    """Convert one in-memory TEI and write what was asked for.

    Runs in a worker process, so it returns what happened - the entry, the files
    written, and why not if none were - rather than the conversions themselves,
    which are as large as the document and have a home on disk already.
    """
    try:
        tei = load_tei_soup(data)
        if tei.TEI is None:
            return name, [], "not a well-formed TEI"

        stem = os.path.join(os.path.expanduser(output_dir), tei_stem(name))
        written = []

        if json_output:
            document = TEI2LossyJSONConverter().convert_tei_file(tei, stream=False)
            if document is None:
                return name, [], "not a usable TEI"
            write_atomic(stem + ".json", json.dumps(document, indent=2, ensure_ascii=False))
            written.append(stem + ".json")

        if markdown_output:
            markdown = TEI2MarkdownConverter().convert_tei_file(tei)
            if markdown is None:
                return name, [], "not a usable TEI"
            write_atomic(stem + ".md", markdown)
            written.append(stem + ".md")

        return name, written, None
    except Exception as e:
        return name, [], str(e)


def _record(
        result: Tuple[str, List[str], Optional[str]],
        log: logging.Logger,
        stats: ArchiveConversionStats
) -> None:
    name, written, error = result
    if error is not None:
        log.error(f"Failed to convert {name}: {error}")
        stats.failed += 1
        return

    stats.converted += 1
    log.debug(f"Converted {name} to {', '.join(written)}")


def looks_like_archive_input(path: Any) -> bool:
    """True when a --input value names an archive rather than a single document."""
    return isinstance(path, str) and looks_like_archive(path)


def main() -> None:
    """CLI: convert an archive of TEI to JSON, Markdown, or both at once.

    The per-format CLIs (TEI2LossyJSON_cli, TEI2Markdown_cli) take an archive
    too, but each parses the documents for itself. Asking for both formats here
    parses each document once, which is most of the work.
    """
    import argparse
    import logging
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m grobid_client.format.tei_archive",
        description="Convert the TEI documents held in an archive, without unpacking it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # both formats, one parse per document, 8 documents at a time
  python -m grobid_client.format.tei_archive --input corpus.zip --output out/ \\
      --json --markdown --workers 8

  # a remote corpus, resumed where an interrupted run left off
  python -m grobid_client.format.tei_archive --input s3://bucket/corpus.zip \\
      --output out/ --markdown --skip-existing
        """
    )
    parser.add_argument("--input", "-i", required=True,
                        help="Archive of TEI files: zip or tar, local or s3:// (zip only)")
    parser.add_argument("--output", "-o", default=None,
                        help="Directory the conversions are written to "
                             "(default: a directory named after the archive)")
    parser.add_argument("--json", action="store_true", help="Write a .json per document")
    parser.add_argument("--markdown", "--md", action="store_true",
                        help="Write a .md per document")
    parser.add_argument("--workers", "-w", type=int, default=None,
                        help="Documents converted in parallel (default: one per core, "
                             "up to %d). 1 converts in this process." % MAX_DEFAULT_WORKERS)
    parser.add_argument("--queue-size", type=int, default=None,
                        help="Documents held in memory at once (default: 4 per worker)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Leave entries whose output files already exist")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    if not args.json and not args.markdown:
        parser.error("nothing to convert to: pass --json, --markdown, or both")

    try:
        stats = convert_archive(
            args.input,
            args.output,
            json_output=args.json,
            markdown_output=args.markdown,
            workers=args.workers,
            queue_size=args.queue_size,
            skip_existing=args.skip_existing,
            verbose=args.verbose,
        )
    except Exception as e:
        logging.error(f"Could not read archive {args.input}: {str(e)}")
        sys.exit(1)

    print(format_summary(stats, args.input))
    sys.exit(0 if stats.failed == 0 else 1)


if __name__ == "__main__":
    main()
