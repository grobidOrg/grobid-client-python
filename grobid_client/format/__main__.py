"""
Package entry point for format converters.

This provides a menu to choose between TEI2LossyJSON and TEI2Markdown converters.
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    """Main entry point that provides a menu for converter selection."""

    # Check if a converter was specified
    if len(sys.argv) < 2:
        print("GROBID format converters - Choose a converter to run")
        print("\nUsage:")
        print("  python -m grobid_client.format <converter> [options]")
        print("\nAvailable converters:")
        print("  TEI2LossyJSON  - Convert TEI XML to JSON format")
        print("  TEI2Markdown   - Convert TEI XML to Markdown format")
        print("\nExamples:")
        print("  python -m grobid_client.format TEI2LossyJSON --input file.tei.xml --output output.json")
        print("  python -m grobid_client.format TEI2Markdown --input file.tei.xml --output output.md")
        print("\nGet help for specific converter:")
        print("  python -m grobid_client.format TEI2LossyJSON --help")
        print("  python -m grobid_client.format TEI2Markdown --help")
        sys.exit(1)

    converter = sys.argv[1]

    if converter == "TEI2LossyJSON":
        from .TEI2LossyJSON_cli import main as lossy_main
        # Replace sys.argv to pass remaining args to the converter
        sys.argv = ["TEI2LossyJSON"] + sys.argv[2:]
        lossy_main()
    elif converter == "TEI2Markdown":
        from .TEI2Markdown_cli import main as markdown_main
        # Replace sys.argv to pass remaining args to the converter
        sys.argv = ["TEI2Markdown"] + sys.argv[2:]
        markdown_main()
    else:
        print(f"Unknown converter: {converter}")
        print("Available converters: TEI2LossyJSON, TEI2Markdown")
        sys.exit(1)


if __name__ == "__main__":
    main()