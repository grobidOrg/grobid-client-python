#!/usr/bin/env python3
"""
Standalone CLI for TEI2Markdown converter.

This script provides a command-line interface for converting TEI XML files to Markdown format
using the TEI2MarkdownConverter.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .TEI2Markdown import TEI2MarkdownConverter


def setup_logging(verbose: bool = False) -> None:
    """Setup logging configuration."""
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def convert_single_file(input_file: Path, output_file: Path, verbose: bool = False) -> bool:
    """Convert a single TEI file to Markdown format."""
    try:
        if verbose:
            logging.info(f"Converting {input_file} to {output_file}")

        converter = TEI2MarkdownConverter()
        result = converter.convert_tei_file(input_file)

        if result is None:
            logging.error(f"Failed to convert {input_file}: TEI file is not well-formed or empty")
            return False

        # Ensure output directory exists
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Write Markdown output
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(result)

        if verbose:
            logging.info(f"Successfully converted {input_file} to {output_file}")

        return True

    except Exception as e:
        logging.error(f"Error converting {input_file}: {str(e)}")
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
        """
    )

    parser.add_argument(
        "--input", "-i",
        type=Path,
        required=True,
        help="Input TEI XML file to convert"
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

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose)

    # Validate input file
    if not args.input.exists():
        logging.error(f"Input file does not exist: {args.input}")
        sys.exit(1)

    if not args.input.is_file():
        logging.error(f"Input path is not a file: {args.input}")
        sys.exit(1)

    # Convert the file
    if args.output:
        success = convert_single_file(args.input, args.output, args.verbose)
        sys.exit(0 if success else 1)
    else:
        # Output to stdout
        try:
            converter = TEI2MarkdownConverter()
            result = converter.convert_tei_file(args.input)

            if result is None:
                logging.error(f"Failed to convert {args.input}: TEI file is not well-formed or empty")
                sys.exit(1)

            # Print Markdown to stdout
            print(result)

        except Exception as e:
            logging.error(f"Error converting {args.input}: {str(e)}")
            sys.exit(1)


if __name__ == "__main__":
    main()