#!/usr/bin/env python3
"""
Script to validate reference offsets in JSON files generated from TEI documents.

This script processes a directory of JSON files and validates that:
1. All references have valid offset_start and offset_end values
2. The text at the specified offsets matches the reference text
3. Offsets are within bounds of the parent text
4. References have the expected structure and types

Usage:
    python validate_json_refs.py <directory_path> [--verbose] [--output report.json]

Example:
    python validate_json_refs.py ./output --verbose --output validation_report.json
"""
from __future__ import annotations

import json
import os
import argparse
import sys
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional
from collections import defaultdict
import datetime


class JSONReferenceValidator:
    """Validates reference offsets in JSON files."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.results = {
            'total_files': 0,
            'valid_files': 0,
            'invalid_files': 0,
            'total_refs': 0,
            'valid_refs': 0,
            'invalid_refs': 0,
            'errors': [],
            'warnings': [],
            'file_details': []
        }

    def validate_directory(self, directory_path: str) -> Dict[str, Any]:
        """Validate all JSON files in a directory or a single JSON file."""
        # Check if it's a single file
        if os.path.isfile(directory_path):
            if not directory_path.endswith('.json'):
                raise ValueError(f"File must be a JSON file: {directory_path}")
            json_files = [Path(directory_path)]
        elif os.path.isdir(directory_path):
            json_files = list(Path(directory_path).glob("*.json"))
        else:
            raise ValueError(f"Path does not exist: {directory_path}")

        if not json_files:
            self.results['warnings'].append(f"No JSON files found in {directory_path}")
            return self.results

        self.results['total_files'] = len(json_files)

        for json_file in json_files:
            self._validate_file(str(json_file))

        return self.results

    def _validate_file(self, file_path: str) -> None:
        """Validate a single JSON file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON in {file_path}: {str(e)}"
            self.results['errors'].append(error_msg)
            self.results['invalid_files'] += 1
            if self.verbose:
                print(f"❌ {error_msg}")
            return
        except Exception as e:
            error_msg = f"Error reading {file_path}: {str(e)}"
            self.results['errors'].append(error_msg)
            self.results['invalid_files'] += 1
            if self.verbose:
                print(f"❌ {error_msg}")
            return

        file_result = {
            'file': file_path,
            'valid': True,
            'total_refs': 0,
            'valid_refs': 0,
            'invalid_refs': 0,
            'errors': [],
            'warnings': []
        }

        # Validate different parts of the JSON structure
        self._validate_body_text_refs(data, file_result)
        self._validate_abstract_refs(data, file_result)
        self._validate_other_sections(data, file_result)

        # Update overall results
        self.results['total_refs'] += file_result['total_refs']
        self.results['valid_refs'] += file_result['valid_refs']
        self.results['invalid_refs'] += file_result['invalid_refs']

        if file_result['valid']:
            self.results['valid_files'] += 1
            if self.verbose:
                print(f"✅ {file_path}: {file_result['valid_refs']}/{file_result['total_refs']} refs valid")
        else:
            self.results['invalid_files'] += 1
            self.results['errors'].extend(file_result['errors'])
            if self.verbose:
                print(f"❌ {file_path}: {file_result['valid_refs']}/{file_result['total_refs']} refs valid")

        self.results['file_details'].append(file_result)

    def _validate_body_text_refs(self, data: Dict[str, Any], file_result: Dict[str, Any]) -> None:
        """Validate references in body_text section."""
        if 'body_text' not in data:
            return

        for i, paragraph in enumerate(data.get('body_text', [])):
            if 'text' not in paragraph or 'refs' not in paragraph:
                continue

            text = paragraph['text']
            refs = paragraph.get('refs', [])
            file_result['total_refs'] += len(refs)

            for j, ref in enumerate(refs):
                is_valid, error = self._validate_single_ref(text, ref, f"body_text[{i}].refs[{j}]")
                if is_valid:
                    file_result['valid_refs'] += 1
                else:
                    file_result['invalid_refs'] += 1
                    file_result['errors'].append(error)

    def _validate_abstract_refs(self, data: Dict[str, Any], file_result: Dict[str, Any]) -> None:
        """Validate references in abstract section."""
        if 'biblio' not in data or 'abstract' not in data['biblio']:
            return

        for i, paragraph in enumerate(data['biblio']['abstract']):
            if 'text' not in paragraph or 'refs' not in paragraph:
                continue

            text = paragraph['text']
            refs = paragraph.get('refs', [])
            file_result['total_refs'] += len(refs)

            for j, ref in enumerate(refs):
                is_valid, error = self._validate_single_ref(text, ref, f"biblio.abstract[{i}].refs[{j}]")
                if is_valid:
                    file_result['valid_refs'] += 1
                else:
                    file_result['invalid_refs'] += 1
                    file_result['errors'].append(error)

    def _validate_other_sections(self, data: Dict[str, Any], file_result: Dict[str, Any]) -> None:
        """Validate references in other sections (annex, etc.)."""
        # Look for other sections that might contain references
        for section_key in ['annex', 'notes']:
            if section_key not in data:
                continue

            section = data[section_key]
            if isinstance(section, list):
                for i, item in enumerate(section):
                    if isinstance(item, dict) and 'text' in item and 'refs' in item:
                        text = item['text']
                        refs = item.get('refs', [])
                        file_result['total_refs'] += len(refs)

                        for j, ref in enumerate(refs):
                            is_valid, error = self._validate_single_ref(text, ref, f"{section_key}[{i}].refs[{j}]")
                            if is_valid:
                                file_result['valid_refs'] += 1
                            else:
                                file_result['invalid_refs'] += 1
                                file_result['errors'].append(error)

    def _validate_single_ref(self, text: str, ref: Dict[str, Any], location: str) -> Tuple[bool, Optional[str]]:
        """Validate a single reference."""
        # Check required fields
        if not isinstance(ref, dict):
            return False, f"{location}: Reference is not a dictionary"

        required_fields = ['type', 'target', 'text', 'offset_start', 'offset_end']
        for field in required_fields:
            if field not in ref:
                return False, f"{location}: Missing required field '{field}'"

        # Check field types
        if not isinstance(ref['offset_start'], int) or not isinstance(ref['offset_end'], int):
            return False, f"{location}: Offsets must be integers"

        if not isinstance(ref['text'], str):
            return False, f"{location}: Reference text must be a string"

        # Check offset bounds
        if ref['offset_start'] < 0 or ref['offset_end'] < 0:
            return False, f"{location}: Offsets cannot be negative"

        if ref['offset_start'] >= ref['offset_end']:
            return False, f"{location}: offset_start ({ref['offset_start']}) must be less than offset_end ({ref['offset_end']})"

        if ref['offset_end'] > len(text):
            return False, f"{location}: offset_end ({ref['offset_end']}) exceeds text length ({len(text)})"

        # Extract text at offsets and compare
        extracted_text = text[ref['offset_start']:ref['offset_end']]
        if extracted_text != ref['text']:
            return False, f"{location}: Text mismatch. Expected '{ref['text']}', got '{extracted_text}'"

        # Check reference type
        valid_types = ['bibr', 'figure', 'table', 'formula', 'ref']
        if ref['type'] not in valid_types:
            return False, f"{location}: Invalid reference type '{ref['type']}'. Valid types: {valid_types}"

        return True, None

    def generate_report(self) -> str:
        """Generate a human-readable report."""
        report_lines = [
            "JSON Reference Offset Validation Report",
            "=" * 50,
            f"Generated: {datetime.datetime.now().isoformat()}",
            "",
            "Summary:",
            f"  Total files: {self.results['total_files']}",
            f"  Valid files: {self.results['valid_files']}",
            f"  Invalid files: {self.results['invalid_files']}",
            f"  Total references: {self.results['total_refs']}",
            f"  Valid references: {self.results['valid_refs']}",
            f"  Invalid references: {self.results['invalid_refs']}",
            ""
        ]

        if self.results['total_refs'] > 0:
            success_rate = (self.results['valid_refs'] / self.results['total_refs']) * 100
            report_lines.append(f"  Success rate: {success_rate:.1f}%")
            report_lines.append("")

        # Add warnings
        if self.results['warnings']:
            report_lines.append("Warnings:")
            for warning in self.results['warnings']:
                report_lines.append(f"  ⚠️  {warning}")
            report_lines.append("")

        # Add errors
        if self.results['errors']:
            report_lines.append("Errors:")
            for error in self.results['errors'][:20]:  # Limit to first 20 errors
                report_lines.append(f"  ❌ {error}")
            if len(self.results['errors']) > 20:
                report_lines.append(f"  ... and {len(self.results['errors']) - 20} more errors")
            report_lines.append("")

        # Add file details
        if self.verbose and self.results['file_details']:
            report_lines.append("File Details:")
            for detail in self.results['file_details']:
                status = "✅" if detail['valid'] else "❌"
                report_lines.append(f"  {status} {detail['file']}: {detail['valid_refs']}/{detail['total_refs']} refs")
                if detail['errors']:
                    for error in detail['errors'][:3]:  # Show first 3 errors per file
                        report_lines.append(f"      - {error}")
                report_lines.append("")

        return "\n".join(report_lines)

    def save_json_report(self, output_path: str) -> None:
        """Save detailed results as JSON."""
        report_data = {
            'metadata': {
                'generated_at': datetime.datetime.now().isoformat(),
                'validator': 'JSON Reference Validator v1.0'
            },
            'summary': {
                'total_files': self.results['total_files'],
                'valid_files': self.results['valid_files'],
                'invalid_files': self.results['invalid_files'],
                'total_refs': self.results['total_refs'],
                'valid_refs': self.results['valid_refs'],
                'invalid_refs': self.results['invalid_refs']
            },
            'warnings': self.results['warnings'],
            'errors': self.results['errors'],
            'file_details': self.results['file_details']
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)


def main() -> None:
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Validate reference offsets in JSON files generated from TEI documents"
    )
    parser.add_argument(
        "directory",
        help="Directory containing JSON files to validate"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed validation progress"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file for detailed JSON report"
    )
    parser.add_argument(
        "--text-report", "-t",
        action="store_true",
        help="Print detailed text report to stdout"
    )
    parser.add_argument(
        "--list-errors", "-e",
        action="store_true",
        help="Print only list of files with errors"
    )

    args = parser.parse_args()

    try:
        validator = JSONReferenceValidator(verbose=args.verbose)
        results = validator.validate_directory(args.directory)

        # Print basic summary
        if results['total_files'] == 0:
            print("No JSON files found to validate.")
            return 0

        print(f"\nValidation Summary:")
        print(f"  Files: {results['valid_files']}/{results['total_files']} valid")
        print(f"  References: {results['valid_refs']}/{results['total_refs']} valid")

        if results['total_refs'] > 0:
            success_rate = (results['valid_refs'] / results['total_refs']) * 100
            print(f"  Success rate: {success_rate:.1f}%")

        # Print only list of files with errors if requested
        if args.list_errors:
            error_files = [detail['file'] for detail in validator.results['file_details'] if detail['invalid_refs'] > 0]
            error_files.sort()
            for file_path in error_files:
                print(file_path)
            print(f"\nTotal files with errors: {len(error_files)}")
            return 1 if error_files else 0

        # Print detailed report if requested
        if args.text_report or args.verbose:
            print("\n" + validator.generate_report())

        # Save JSON report if requested
        if args.output:
            validator.save_json_report(args.output)
            print(f"\nDetailed report saved to: {args.output}")

        # Exit with error code if there are invalid files or references
        if results['invalid_files'] > 0 or results['invalid_refs'] > 0:
            return 1

        return 0

    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())