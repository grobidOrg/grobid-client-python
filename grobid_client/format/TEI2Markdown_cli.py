#!/usr/bin/env python3
"""
Standalone CLI for TEI2Markdown converter.

This script provides a command-line interface for converting TEI XML files to Markdown format
using the TEI2MarkdownConverter.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .TEI2Markdown import TEI2MarkdownConverter
from .tei_archive import (
    MAX_DEFAULT_WORKERS,
    convert_archive,
    format_summary,
    looks_like_archive_input,
)
from .tei_source import STDIN_ALIAS, TEISource, describe_source, resolve_cli_input


def setup_logging(verbose: bool = False) -> None:
    """Setup logging configuration."""
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def convert_single_file(input_file: TEISource, output_file: Path, verbose: bool = False) -> bool:
    """Convert a single TEI document to Markdown format."""
    try:
        if verbose:
            logging.info(f"Converting {describe_source(input_file)} to {output_file}")

        converter = TEI2MarkdownConverter()
        result = converter.convert_tei_file(input_file)

        if result is None:
            logging.error(f"Failed to convert {describe_source(input_file)}: TEI file is not well-formed or empty")
            return False

        # Ensure output directory exists
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Write Markdown output
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(result)

        if verbose:
            logging.info(f"Successfully converted {describe_source(input_file)} to {output_file}")

        return True

    except Exception as e:
        logging.error(f"Error converting {describe_source(input_file)}: {str(e)}")
        return False


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Convert TEI XML files to Markdown format using TEI2Markdown converter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert a single TEI file
  python -m grobid_client.format.TEI2Markdown --input input.tei.xml --output output.md

  # Convert with verbose logging
  python -m grobid_client.format.TEI2Markdown --input input.tei.xml --output output.md --verbose

  # Convert and output to stdout
  python -m grobid_client.format.TEI2Markdown --input input.tei.xml

  # Read the TEI from stdin, as one stage of a pipe
  cat input.tei.xml | python -m grobid_client.format.TEI2Markdown

  # Convert every TEI in an archive, without unpacking it
  python -m grobid_client.format.TEI2Markdown --input corpus.zip --output markdown/
  python -m grobid_client.format.TEI2Markdown --input s3://bucket/corpus.zip --output markdown/
        """
    )

    parser.add_argument(
        "--input", "-i",
        # Kept as a string: an s3:// URI does not survive a round-trip through
        # Path, which collapses the double slash of the scheme.
        type=str,
        default=STDIN_ALIAS,
        help="Input TEI XML file to convert, an archive (zip/tar, local or s3://) "
             "of TEI files, or '-' to read a single document from stdin (the default)"
    )

    parser.add_argument(
        "--output", "-o",
        type=Path,
        help="Output Markdown file (if not specified, prints to stdout)"
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )

    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=None,
        help="Archive input: how many documents to convert in parallel "
             "(default: one per core, up to %d). 1 converts in this process."
             % MAX_DEFAULT_WORKERS
    )

    parser.add_argument(
        "--queue-size",
        type=int,
        default=None,
        help="Archive input: how many documents to hold in memory at once "
             "(default: 4 per worker)"
    )

    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Archive input: leave entries whose output files already exist, "
             "so an interrupted run can be resumed"
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose)

    # An archive is converted entry by entry, in memory: --output names the
    # directory the conversions go to, not a file.
    if looks_like_archive_input(args.input):
        try:
            stats = convert_archive(
                args.input,
                str(args.output) if args.output else None,
                markdown_output=True,
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

    # A path has to be there to be converted; stdin only has to be readable,
    # and that is the shell's business, not ours.
    tei_input = resolve_cli_input(args.input)

    if isinstance(tei_input, Path):
        if not tei_input.exists():
            logging.error(f"Input file does not exist: {tei_input}")
            sys.exit(1)

        if not tei_input.is_file():
            logging.error(f"Input path is not a file: {tei_input}")
            sys.exit(1)

    # Convert the document
    if args.output:
        success = convert_single_file(tei_input, args.output, args.verbose)
        sys.exit(0 if success else 1)
    else:
        # Output to stdout
        try:
            converter = TEI2MarkdownConverter()
            result = converter.convert_tei_file(tei_input)

            if result is None:
                logging.error(f"Failed to convert {describe_source(tei_input)}: TEI file is not well-formed or empty")
                sys.exit(1)

            # Print Markdown to stdout
            print(result)
            # Flush here so that a pipe closed on the other side is caught
            # below, and not by the interpreter as it shuts down.
            sys.stdout.flush()

        except BrokenPipeError:
            # The other end of the pipe left early (`| head`, an interrupted
            # pager): that is not an error, but Python would still report a
            # failed flush at shutdown unless stdout is sent to /dev/null first.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            sys.exit(0)
        except Exception as e:
            logging.error(f"Error converting {describe_source(tei_input)}: {str(e)}")
            sys.exit(1)


if __name__ == "__main__":
    main()