"""

Grobid Python Client

This version uses the standard ThreadPoolExecutor for parallelizing the
concurrent calls to the GROBID services.  Given the limits of
ThreadPoolExecutor (input stored in memory, blocking Executor.map until the 
whole input is acquired), it works with batches of PDF of a size indicated 
in the config.json file (default is 1000 entries). We are moving from first 
batch to the second one only when the first is entirely processed - which 
means it is slightly sub-optimal, but should scale better. Working without 
batch would mean acquiring a list of millions of files in directories and 
would require something scalable too (e.g. done in a separate thread), 
which is not implemented for the moment.

"""
from __future__ import annotations

import os
import json
import argparse
import fnmatch
import glob
import time
import concurrent.futures
import ntpath
import re
import requests
import pathlib
import logging
import shutil
import tarfile
import tempfile
import zipfile
from typing import Any, BinaryIO, Optional, Tuple, Union
import copy

from .format.TEI2LossyJSON import TEI2LossyJSONConverter
from .client import ApiClient


def _default_file_mode() -> int:
    """The mode open(..., 'w') would have produced, i.e. 0666 minus the umask.

    tempfile.mkstemp hardcodes 0600, so files written through it and renamed
    into place would end up private -- unreadable to the group on shared
    scratch, where the outputs of a cluster run usually have to be. Read the
    umask once here, at import, because querying it means temporarily setting
    it and that is not safe to do from worker threads.
    """
    umask = os.umask(0o022)
    os.umask(umask)
    return 0o666 & ~umask


_DEFAULT_FILE_MODE = _default_file_mode()


class ServerUnavailableException(Exception):
    """Exception raised when GROBID server is not available or not responding."""

    def __init__(self, message: str = "GROBID server is not available") -> None:
        super().__init__(message)
        self.message = message


class GrobidClient(ApiClient):
    # Below this client-side timeout (in seconds) citation consolidation is
    # likely to trigger HTTP 408 (Request Timeout) errors, so we warn the user.
    # See https://github.com/grobidOrg/grobid-client-python/issues/54
    CONSOLIDATE_CITATIONS_MIN_TIMEOUT = 120

    # Archive extensions that can be streamed entry-by-entry via --input instead
    # of being fully decompressed first. Order matters: multi-dot suffixes must
    # come before their single-dot prefixes when stripping (see _archive_stem).
    ARCHIVE_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tgz", ".tbz2", ".zip", ".tar")

    # Default configuration values
    DEFAULT_CONFIG: dict = {
        'grobid_server': 'http://localhost:8070',
        'batch_size': 10,
        'sleep_time': 5,
        'timeout': 180,
        'coordinates': [
            "title",
            "persName",
            "affiliation",
            "orgName",
            "formula",
            "figure",
            "ref",
            "biblStruct",
            "head",
            "p",
            "s",
            "note"
        ],
        'logging': {
            'level': 'WARNING',
            'format': '%(asctime)s - %(levelname)s - %(message)s',
            'console': True,
            'file': None,  # Disabled by default
            'max_file_size': '10MB',
            'backup_count': 3
        }
    }

    def __init__(
            self,
            grobid_server: Optional[str] = None,
            batch_size: Optional[int] = None,
            coordinates: Optional[list] = None,
            sleep_time: Optional[int] = None,
            timeout: Optional[int] = None,
            config_path: Optional[str] = None,
            check_server: bool = True,
            verbose: bool = False
    ) -> None:
        # Store verbose parameter for logging configuration
        self.verbose = verbose

        # Initialize config with defaults
        self.config = copy.deepcopy(self.DEFAULT_CONFIG)

        # Load config file (which may override current values)
        if config_path:
            self._load_config(config_path)

        # Constructor parameters take precedence over config file values
        # This ensures CLI arguments override config file values
        self._set_config_params({
            'grobid_server': grobid_server,
            'batch_size': batch_size,
            'coordinates': coordinates,
            'sleep_time': sleep_time,
            'timeout': timeout
        })

        # Configure logging based on config and verbose flag
        self._configure_logging()

        if check_server:
            self._test_server_connection()

    def _set_config_params(self, params: dict) -> None:
        """Set configuration parameters, only if they are not None."""
        for key, value in params.items():
            if value is not None:
                self.config[key] = value

    def _warn_on_consolidation_timeout(self, consolidate_citations: bool) -> None:
        """Warn when citation consolidation is enabled with a low client timeout.

        Consolidating citations makes GROBID query external services and can be
        much slower than a plain extraction. A short client-side timeout often
        leads to HTTP 408 errors, so we recommend at least a couple of minutes.
        See https://github.com/grobidOrg/grobid-client-python/issues/54
        """
        if not consolidate_citations:
            return

        timeout = self.config.get("timeout", self.DEFAULT_CONFIG["timeout"])
        if timeout < self.CONSOLIDATE_CITATIONS_MIN_TIMEOUT:
            self.logger.warning(
                f"Citation consolidation is enabled but the timeout is only {timeout}s. "
                f"Consolidation queries external services and can be slow; a low timeout "
                f"frequently causes HTTP 408 (Request Timeout) errors. Consider increasing "
                f"the 'timeout' setting to at least {self.CONSOLIDATE_CITATIONS_MIN_TIMEOUT}s "
                f"(2-3 minutes is recommended)."
            )

    def _handle_server_busy_retry(self, file_path: str, retry_func: Any, *args: Any, **kwargs: Any) -> Any:
        """Handle server busy (503) retry logic."""
        self.logger.warning(f"Server busy (503), retrying {file_path} after {self.config['sleep_time']} seconds")
        time.sleep(self.config["sleep_time"])
        return retry_func(*args, **kwargs)

    def _handle_request_error(
            self, file_path: str, error: Exception, error_type: str = "Request"
    ) -> Tuple[str, int, str]:
        """Handle request errors with consistent logging and return format."""
        self.logger.error(f"{error_type} failed for {file_path}: {str(error)}")
        return (file_path, 500, f"{error_type} failed: {str(error)}")

    def _handle_unexpected_error(self, file_path: str, error: Exception) -> Tuple[str, int, str]:
        """Handle unexpected errors with consistent logging and return format."""
        self.logger.error(f"Unexpected error processing {file_path}: {str(error)}")
        return (file_path, 500, f"Unexpected error: {str(error)}")

    def _configure_logging(self) -> None:
        """Configure logging based on the configuration settings."""
        # Get logging config with defaults
        log_config = self.config.get('logging', {})

        # Parse log level - verbose flag takes precedence over config
        if self.verbose:
            # When verbose is explicitly set via command line, always use INFO level
            log_level_str = 'INFO'
            log_level = logging.INFO
        else:
            # Use config file level when not verbose, but default to WARNING
            config_level_str = log_config.get('level', 'WARNING').upper()
            # If config specifies INFO but verbose is False, use WARNING instead
            if config_level_str == 'INFO':
                log_level_str = 'WARNING'
            else:
                log_level_str = config_level_str
            log_level = getattr(logging, log_level_str, logging.WARNING)

        # Parse log format
        log_format = log_config.get('format', '%(asctime)s - %(levelname)s - %(message)s')

        # Create formatter
        formatter = logging.Formatter(log_format)

        # Configure the logger
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(log_level)
        self.logger.propagate = False  # Prevent propagation to root logger to avoid duplicates

        # Clear any existing handlers to avoid duplicates
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)

        # Console handler
        if log_config.get('console', True):
            console_handler = logging.StreamHandler()
            console_handler.setLevel(log_level)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

        # File handler
        log_file = log_config.get('file')
        if log_file:
            try:
                # Parse file size (support formats like "10MB", "1GB", etc.)
                max_bytes = self._parse_file_size(log_config.get('max_file_size', '10MB'))
                backup_count = log_config.get('backup_count', 3)

                from logging.handlers import RotatingFileHandler
                file_handler: logging.Handler = RotatingFileHandler(
                    log_file,
                    maxBytes=max_bytes,
                    backupCount=backup_count
                )
                file_handler.setLevel(log_level)
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)

                self.logger.debug(
                    f"File logging configured: {log_file} (max size: {max_bytes}, backups: {backup_count})")
            except Exception as e:
                # Fallback to basic file handler if rotating handler fails
                try:
                    file_handler = logging.FileHandler(log_file)
                    file_handler.setLevel(log_level)
                    file_handler.setFormatter(formatter)
                    self.logger.addHandler(file_handler)
                    self.logger.warning(f"Using basic file handler due to error with rotating handler: {e}")
                except Exception as file_error:
                    self.logger.warning(f"Could not configure file logging: {file_error}")

        self.logger.info(
            f"Logging configured - Level: {log_level_str}, Console: {log_config.get('console', True)}, File: {log_file or 'disabled'}")

    def _parse_file_size(self, size_str: Union[str, int]) -> int:
        """Parse file size string like '10MB', '1GB' to bytes."""
        size_str = str(size_str).upper().strip()

        # Extract number and unit
        match = re.match(r'(\d+(?:\.\d+)?)\s*([KMGT]?B?)', size_str)
        if not match:
            return 10 * 1024 * 1024  # Default 10MB

        number = float(match.group(1))
        unit = match.group(2)

        # Convert to bytes
        multipliers = {
            '': 1,
            'B': 1,
            'KB': 1024,
            'MB': 1024 ** 2,
            'GB': 1024 ** 3,
            'TB': 1024 ** 4
        }

        return int(number * multipliers.get(unit, 1))

    def _load_config(self, path: str = "./config.json") -> None:
        """
        Load and merge configuration from a JSON file with default values.
        If the file doesn't exist, keep the default values.

        Args:
            path (str): Path to the JSON configuration file

        Raises:
            FileNotFoundError: If the config file is not found
            json.JSONDecodeError: If the config file contains invalid JSON
            Exception: For other file reading errors
        """
        # Create a temporary logger for configuration loading since main logger isn't configured yet
        temp_logger = logging.getLogger(f"{__name__}.config_loader")
        temp_logger.propagate = False  # Prevent propagation to avoid duplicates
        if not temp_logger.handlers:
            temp_handler = logging.StreamHandler()
            temp_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
            temp_logger.addHandler(temp_handler)
            temp_logger.setLevel(logging.INFO)

        try:
            temp_logger.info(f"Loading configuration file from {path}")
            with open(path, 'r') as config_file:
                config_json = config_file.read()
                # Update the default config with values from the file
                file_config = json.loads(config_json)
                self.config.update(file_config)
                temp_logger.info("Configuration file loaded successfully")
        except FileNotFoundError as e:
            # If config file doesn't exist, keep using default values
            error_msg = f"The specified config file {path} was not found. Check the path or leave it blank to use the default configuration."
            temp_logger.error(error_msg)
            raise FileNotFoundError(error_msg) from e
        except json.JSONDecodeError as e:
            # If config exists, but it's invalid, we raise an exception
            error_msg = f"Could not parse config file at {path}: {str(e)}"
            temp_logger.error(error_msg)
            raise json.JSONDecodeError(error_msg, e.doc, e.pos) from e
        except Exception as e:
            error_msg = f"Error reading config file at {path}: {str(e)}"
            temp_logger.error(error_msg)
            raise Exception(error_msg) from e

    def _test_server_connection(self) -> Tuple[bool, int]:
        """Test if the server is up and running.

        Returns:
            tuple: (is_available, status_code)

        Raises:
            ServerUnavailableException: If server is not reachable
        """
        the_url = self.get_server_url("isalive")
        try:
            r = requests.get(the_url, timeout=10)
            status = r.status_code

            if status != 200:
                error_msg = f"GROBID server {self.config['grobid_server']} does not appear up and running (status: {status})"
                self.logger.error(error_msg)
                return False, status
            else:
                self.logger.info(f"GROBID server {self.config['grobid_server']} is up and running")
                return True, status

        except requests.exceptions.RequestException as e:
            error_msg = f"GROBID server {self.config['grobid_server']} does not appear up and running, connection failed: {str(e)}"
            self.logger.error(error_msg)
            raise ServerUnavailableException(error_msg) from e

    def _output_file_name(
            self,
            input_file: str,
            input_path: str,
            output: Optional[str],
    ) -> str:
        # Use pathlib for consistent cross-platform path handling
        input_file_path = pathlib.Path(input_file)

        if output is not None:
            # Calculate relative path from input_path, then join with output directory
            input_path_abs = pathlib.Path(input_path).resolve()
            input_file_rel = input_file_path.resolve().relative_to(input_path_abs)
            filename = pathlib.Path(output) / f"{input_file_rel.stem}.grobid.tei.xml"
        else:
            # Use the same directory as the input file
            filename = input_file_path.parent / f"{input_file_path.stem}.grobid.tei.xml"

        return str(filename)

    def _write_atomic(self, filename: str, text: str) -> None:
        """Write text to filename via a temp file in the same directory, then os.replace.

        A killed process must never leave a partial output behind. process_batch
        decides a document is already done with os.path.isfile() alone, so a TEI
        truncated by an OOM kill or a wall-clock timeout is indistinguishable
        from a complete one and is skipped on every subsequent run -- the
        corruption is permanent and silent. Writing to a temp file and renaming
        means the destination either does not exist or is the whole document.

        The temp file goes in the DESTINATION directory, not TMPDIR: os.replace
        is only atomic within a filesystem, and on a cluster TMPDIR is usually a
        different mount. The "." prefix and ".tmp" suffix keep the temp file from
        matching *.grobid.tei.xml or *_[0-9]*.txt, so output counting is
        unaffected while a write is in flight.

        Residual risk: a SIGKILL between mkstemp and replace leaks a temp file.
        That is visible and harmless, unlike a truncated TEI.
        """
        dest = pathlib.Path(os.path.expanduser(filename))
        dest.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp names are unique, so concurrent writers from the
        # ThreadPoolExecutor cannot collide on the temp path.
        fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent), prefix=".", suffix=".tmp")
        try:
            tmp_file = os.fdopen(fd, "w", encoding="utf8")
        except BaseException:
            # fdopen did not take ownership of fd, so we still have to close it.
            # Past this point the file object owns it and closing it here too
            # could close an unrelated descriptor that reused the number.
            os.close(fd)
            self._unlink_quietly(tmp_path)
            raise
        try:
            with tmp_file:
                tmp_file.write(text)
            os.chmod(tmp_path, _DEFAULT_FILE_MODE)   # mkstemp gives 0600
            os.replace(tmp_path, str(dest))
        except BaseException:
            self._unlink_quietly(tmp_path)
            raise

    @staticmethod
    def _unlink_quietly(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def ping(self) -> Tuple[bool, int]:
        """
        Check the Grobid service. Returns True if the service is up.
        In addition, returns also the status code.
        """
        return self._test_server_connection()

    def process(
            self,
            service: str,
            input_path: str,
            output: Optional[str] = None,
            n: int = 10,
            generate_ids: bool = False,
            consolidate_header: bool = True,
            consolidate_citations: bool = False,
            include_raw_citations: bool = False,
            include_raw_affiliations: bool = False,
            tei_coordinates: bool = False,
            segment_sentences: bool = False,
            force: bool = True,
            verbose: bool = False,
            flavor: Optional[str] = None,
            json_output: bool = False,
            markdown_output: bool = False
    ) -> None:
        if input_path is None:
            self.logger.warning("No input path provided")
            return
        return self.process_paths(
            service, [input_path], output=output, n=n, generate_ids=generate_ids,
            consolidate_header=consolidate_header, consolidate_citations=consolidate_citations,
            include_raw_citations=include_raw_citations,
            include_raw_affiliations=include_raw_affiliations,
            tei_coordinates=tei_coordinates, segment_sentences=segment_sentences,
            force=force, verbose=verbose, flavor=flavor,
            json_output=json_output, markdown_output=markdown_output,
        )

    def process_paths(
            self,
            service: str,
            inputs: list,
            output: Optional[str] = None,
            n: int = 10,
            generate_ids: bool = False,
            consolidate_header: bool = True,
            consolidate_citations: bool = False,
            include_raw_citations: bool = False,
            include_raw_affiliations: bool = False,
            tei_coordinates: bool = False,
            segment_sentences: bool = False,
            force: bool = True,
            verbose: bool = False,
            flavor: Optional[str] = None,
            json_output: bool = False,
            markdown_output: bool = False
    ) -> None:
        """Process a list of inputs.

        Each input may be a local path, a shell glob (``**/*.pdf``), a directory,
        a local archive, or an ``s3://`` object/prefix/glob. This backs both the
        ``--input`` option (a single input) and ``--input-list`` (a manifest file
        of paths). Results from all inputs are aggregated into one summary.
        """
        start_time = time.time()

        # See https://github.com/grobidOrg/grobid-client-python/issues/54
        self._warn_on_consolidation_timeout(consolidate_citations)

        matched_paths = []
        for inp in inputs:
            matched_paths.extend(self._resolve_input_paths(inp))
        if not matched_paths:
            self.logger.warning(f"No files match input(s): {inputs}")
            return

        # Partition into archives (streamed), remote loose files (s3) and local
        # filesystem files (directories are expanded to their eligible files).
        archive_paths = []
        remote_files = []
        fs_files = []
        for path in matched_paths:
            if self._is_s3(path):
                if self._looks_like_archive(path):
                    archive_paths.append(path)
                elif self._is_eligible_input(self._s3_basename(path), service):
                    remote_files.append(path)
                else:
                    self.logger.debug(f"Skipping s3 input (not an eligible file/archive): {path}")
            elif self._is_archive(path):
                archive_paths.append(path)
            elif os.path.isdir(path):
                fs_files.extend(self._collect_directory_files(path, service))
            elif os.path.isfile(path) and self._is_eligible_input(os.path.basename(path), service):
                fs_files.append(path)
            else:
                self.logger.debug(f"Skipping input (not an eligible file/dir/archive): {path}")

        if not fs_files and not archive_paths and not remote_files:
            self.logger.warning(f"No eligible files found in input(s): {inputs}")
            return

        processed_files_count = 0
        errors_files_count = 0
        skipped_files_count = 0
        total_files = 0

        # Local files gathered from directories and/or loose glob matches
        if fs_files:
            print(f"Found {len(fs_files)} local file(s) to process")
            bp, be, bs = self._run_file_batches(
                service, fs_files, self._common_base(fs_files), output, n,
                generate_ids, consolidate_header, consolidate_citations,
                include_raw_citations, include_raw_affiliations, tei_coordinates,
                segment_sentences, force, verbose, flavor, json_output, markdown_output
            )
            processed_files_count += bp
            errors_files_count += be
            skipped_files_count += bs
            total_files += len(fs_files)

        # Loose remote (s3) files: streamed to a temp dir one chunk at a time
        if remote_files:
            rt, rp, re_count, rs = self._process_remote_files(
                service, remote_files, output, n,
                generate_ids, consolidate_header, consolidate_citations,
                include_raw_citations, include_raw_affiliations, tei_coordinates,
                segment_sentences, force, verbose, flavor, json_output, markdown_output
            )
            processed_files_count += rp
            errors_files_count += re_count
            skipped_files_count += rs
            total_files += rt

        # Archives (local or s3 zip) are streamed entry-by-entry per chunk
        for archive_path in archive_paths:
            at, ap, ae, as_count = self._process_archive_core(
                service, archive_path, output, n,
                generate_ids, consolidate_header, consolidate_citations,
                include_raw_citations, include_raw_affiliations, tei_coordinates,
                segment_sentences, force, verbose, flavor, json_output, markdown_output
            )
            processed_files_count += ap
            errors_files_count += ae
            skipped_files_count += as_count
            total_files += at

        if total_files == 0:
            self.logger.warning(f"No eligible files found in input(s): {inputs}")
            return

        runtime = time.time() - start_time
        self._print_processing_summary(
            processed_files_count, errors_files_count, skipped_files_count, total_files, runtime
        )

    def _resolve_input_paths(self, input_path: str) -> list:
        """Resolve an input into a sorted list of concrete paths.

        Handles ``s3://`` URIs/prefixes/globs, shell-style glob patterns
        (including the recursive ``**``) and ``~`` expansion. A plain local path
        without glob metacharacters is returned as-is (so callers can still
        handle a missing path themselves).
        """
        if self._is_s3(input_path):
            return self._resolve_s3_paths(input_path)
        expanded = os.path.expanduser(input_path)
        if glob.has_magic(expanded):
            return sorted(glob.glob(expanded, recursive=True))
        return [expanded]

    def _collect_directory_files(self, directory: str, service: str) -> list:
        """Recursively collect eligible input files from a directory."""
        files = []
        for path in sorted(pathlib.Path(directory).rglob('*')):
            if path.is_file() and self._is_eligible_input(path.name, service):
                files.append(str(path))
        return files

    def _common_base(self, files: list) -> str:
        """Return a directory that is an ancestor of all given files.

        Used as ``input_path`` for output-name computation; only needs to be a
        common ancestor so ``Path.relative_to`` does not fail.
        """
        abs_files = [os.path.abspath(f) for f in files]
        if len(abs_files) == 1:
            return os.path.dirname(abs_files[0])
        try:
            base = os.path.commonpath(abs_files)
        except ValueError:
            # e.g. paths on different drives (Windows); fall back to first parent
            return os.path.dirname(abs_files[0])
        return base if os.path.isdir(base) else os.path.dirname(base)

    # ---- S3 support (optional 's3' extra: smart_open + boto3) ----

    @staticmethod
    def _is_s3(path: Any) -> bool:
        """Return True if path is an s3:// URI."""
        return isinstance(path, str) and path.startswith("s3://")

    @staticmethod
    def _split_s3(uri: str) -> Tuple[str, str]:
        """Split an s3://bucket/key URI into (bucket, key)."""
        bucket, _, key = uri[len("s3://"):].partition("/")
        return bucket, key

    def _s3_basename(self, uri: str) -> str:
        """Return the last path component of an s3:// key."""
        return self._split_s3(uri)[1].rsplit("/", 1)[-1]

    def _import_smart_open(self) -> Any:
        try:
            import smart_open  # noqa: F401
            return smart_open
        except ImportError as e:
            raise ImportError(
                "Reading from s3:// requires the optional 's3' extra. "
                "Install it with: pip install grobid-client-python[s3]"
            ) from e

    def _import_boto3(self) -> Any:
        try:
            import boto3  # noqa: F401
            return boto3
        except ImportError as e:
            raise ImportError(
                "Listing s3:// requires the optional 's3' extra. "
                "Install it with: pip install grobid-client-python[s3]"
            ) from e

    def _s3_open(self, uri: str) -> BinaryIO:
        """Open an S3 object as a seekable binary stream (HTTP range-streamed).

        The returned stream lets zipfile read only the central directory and the
        requested entries, so a remote zip is never fully downloaded.
        """
        return self._import_smart_open().open(uri, "rb")

    def _resolve_s3_paths(self, uri: str) -> list:
        """Resolve an s3:// object/prefix/glob into a sorted list of object URIs.

        - ``s3://bucket/path/file.zip``  -> that single object
        - ``s3://bucket/prefix/``        -> every object under the prefix
        - ``s3://bucket/prefix/*.zip``   -> objects under the prefix matching the glob
        """
        bucket, key = self._split_s3(uri)
        if not bucket:
            self.logger.warning(f"Invalid s3 uri: {uri}")
            return []

        pattern = None
        if glob.has_magic(key):
            magic = min(key.find(c) for c in "*?[" if c in key)
            prefix = key[:magic]
            pattern = key
        elif key == "" or key.endswith("/"):
            prefix = key
        else:
            return [uri]  # a concrete object key

        s3 = self._import_boto3().client("s3")
        keys = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if k.endswith("/"):
                    continue
                if pattern is None or fnmatch.fnmatch(k, pattern):
                    keys.append(k)
        return [f"s3://{bucket}/{k}" for k in sorted(keys)]

    def _print_processing_summary(
            self,
            processed: int,
            errors: int,
            skipped: int,
            total: int,
            runtime: float
    ) -> None:
        """Print the final processing statistics (shared by all input modes)."""
        docs_per_second = processed / runtime if runtime > 0 else 0
        seconds_per_doc = runtime / processed if processed > 0 else 0

        print(f"Processing completed: {processed} out of {total} files processed")
        print(f"Errors: {errors} out of {total} files processed")
        if skipped > 0:
            print(f"Skipped: {skipped} out of {total} files (already existed, use --force to reprocess)")

        print(f"⏱️  Total runtime: {runtime:.2f} seconds")
        print(f"🚀 Speed: {docs_per_second:.2f} documents/second")
        print(f" Throughput: {seconds_per_doc:.2f} seconds/document")

    def _run_file_batches(
            self,
            service: str,
            input_files: list,
            input_path: str,
            output: Optional[str],
            n: int,
            generate_ids: bool,
            consolidate_header: bool,
            consolidate_citations: bool,
            include_raw_citations: bool,
            include_raw_affiliations: bool,
            tei_coordinates: bool,
            segment_sentences: bool,
            force: bool,
            verbose: bool,
            flavor: Optional[str],
            json_output: bool,
            markdown_output: bool
    ) -> Tuple[int, int, int]:
        """Run process_batch over a list of files in chunks of batch_size.

        Returns the aggregated (processed, errors, skipped) counts.
        """
        batch_size_pdf = self.config["batch_size"]
        processed_files_count = 0
        errors_files_count = 0
        skipped_files_count = 0

        batch = []
        for input_file in input_files:
            if verbose:
                try:
                    self.logger.info(f"Found file: {os.path.basename(input_file)}")
                except UnicodeEncodeError:
                    # may happen on linux see https://stackoverflow.com/questions/27366479/python-3-os-walk-file-paths-unicodeencodeerror-utf-8-codec-cant-encode-s
                    self.logger.warning("Could not log filename due to encoding issues")

            batch.append(input_file)

            if len(batch) == batch_size_pdf:
                batch_processed, batch_errors, batch_skipped = self.process_batch(
                    service, batch, input_path, output, n, generate_ids,
                    consolidate_header, consolidate_citations, include_raw_citations,
                    include_raw_affiliations, tei_coordinates, segment_sentences,
                    force, verbose, flavor, json_output, markdown_output
                )
                processed_files_count += batch_processed
                errors_files_count += batch_errors
                skipped_files_count += batch_skipped
                batch = []

        if batch:
            batch_processed, batch_errors, batch_skipped = self.process_batch(
                service, batch, input_path, output, n, generate_ids,
                consolidate_header, consolidate_citations, include_raw_citations,
                include_raw_affiliations, tei_coordinates, segment_sentences,
                force, verbose, flavor, json_output, markdown_output
            )
            processed_files_count += batch_processed
            errors_files_count += batch_errors
            skipped_files_count += batch_skipped

        return processed_files_count, errors_files_count, skipped_files_count

    def _is_eligible_input(self, filename: str, service: str) -> bool:
        """Return True if a file name is a valid input for the given service."""
        if filename.endswith(".pdf") or filename.endswith(".PDF"):
            return True
        if service == 'processCitationList' and (
                filename.endswith(".txt") or filename.endswith(".TXT")):
            return True
        if service == 'processCitationPatentST36' and (
                filename.endswith(".xml") or filename.endswith(".XML")):
            return True
        return False

    def _looks_like_archive(self, path: str) -> bool:
        """Return True if the path/URI name has a known archive extension."""
        lower = path.lower()
        return any(lower.endswith(ext) for ext in self.ARCHIVE_EXTENSIONS)

    def _is_archive(self, path: str) -> bool:
        """Return True if path is an existing local zip/tar archive file."""
        return os.path.isfile(path) and self._looks_like_archive(path)

    def _archive_stem(self, path: str) -> str:
        """Strip a known archive extension from path (e.g. docs.tar.gz -> docs)."""
        lower = path.lower()
        for ext in self.ARCHIVE_EXTENSIONS:
            if lower.endswith(ext):
                return path[:-len(ext)]
        return os.path.splitext(path)[0]

    def _safe_member_path(self, dest_dir: str, arcname: str) -> Optional[str]:
        """Resolve an archive entry name to a safe path under dest_dir.

        Leading slashes, drive letters and '..' components are stripped to
        prevent path-traversal ("zip slip") outside of dest_dir. Returns None
        if the entry name has no usable path component.
        """
        normalized = arcname.replace("\\", "/")
        parts = [p for p in normalized.split("/") if p not in ("", ".", "..")]
        if not parts:
            return None
        return os.path.join(dest_dir, *parts)

    def _open_archive(self, archive_path: str) -> Tuple[str, Any, list]:
        """Open a zip/tar archive and return (kind, handle, member_names).

        member_names contains only regular files (directories are skipped).
        For s3:// zips the archive is range-streamed (not fully downloaded); the
        underlying stream is stashed on the handle so the caller can close it.

        The handle is a ZipFile or a TarFile, which share no common interface
        here: which one it is, is what the returned "kind" tag is for, and it is
        the tag - not the type - that the callers dispatch on.
        """
        archive: Any
        if self._is_s3(archive_path):
            if not archive_path.lower().endswith(".zip"):
                raise ValueError(
                    f"Only .zip archives can be range-streamed over s3://: {archive_path}"
                )
            stream = self._s3_open(archive_path)
            archive = zipfile.ZipFile(stream)
            archive._grobid_stream = stream  # closed by _process_archive_core
            names = [n for n in archive.namelist() if not n.endswith("/")]
            return "zip", archive, names

        if archive_path.lower().endswith(".zip"):
            archive = zipfile.ZipFile(archive_path)
            names = [n for n in archive.namelist() if not n.endswith("/")]
            return "zip", archive, names

        archive = tarfile.open(archive_path, "r:*")
        names = [m.name for m in archive.getmembers() if m.isfile()]
        return "tar", archive, names

    def _extract_archive_member(
            self,
            kind: str,
            archive: Any,
            member_name: str,
            dest_dir: str
    ) -> Optional[str]:
        """Stream a single archive entry to dest_dir, preserving its relative path.

        Returns the path of the extracted file, or None if it was skipped.
        """
        target = self._safe_member_path(dest_dir, member_name)
        if target is None:
            self.logger.warning(f"Skipping archive entry with unsafe path: {member_name}")
            return None

        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if kind == "zip":
            source = archive.open(member_name)
        else:
            source = archive.extractfile(archive.getmember(member_name))
            if source is None:
                return None

        try:
            with open(target, "wb") as out_file:
                shutil.copyfileobj(source, out_file)
        finally:
            source.close()

        return target

    def process_archive(
            self,
            service: str,
            archive_path: str,
            output: Optional[str] = None,
            n: int = 10,
            generate_ids: bool = False,
            consolidate_header: bool = True,
            consolidate_citations: bool = False,
            include_raw_citations: bool = False,
            include_raw_affiliations: bool = False,
            tei_coordinates: bool = False,
            segment_sentences: bool = False,
            force: bool = True,
            verbose: bool = False,
            flavor: Optional[str] = None,
            json_output: bool = False,
            markdown_output: bool = False
    ) -> None:
        """Process the eligible files contained in a zip/tar archive.

        The archive is never fully decompressed: entries are streamed to a
        temporary directory in chunks of ``batch_size`` (from the config), each
        chunk is sent to GROBID via ``process_batch``, and the temporary files
        are removed before the next chunk is extracted. This keeps disk usage
        bounded regardless of the archive size. Output files follow the same
        flat naming convention as directory processing (one ``<stem>`` per
        result, in ``output``).
        """
        start_time = time.time()
        self._warn_on_consolidation_timeout(consolidate_citations)

        total_files, processed, errors, skipped = self._process_archive_core(
            service, archive_path, output, n, generate_ids, consolidate_header,
            consolidate_citations, include_raw_citations, include_raw_affiliations,
            tei_coordinates, segment_sentences, force, verbose, flavor,
            json_output, markdown_output
        )

        if total_files == 0:
            return

        runtime = time.time() - start_time
        self._print_processing_summary(processed, errors, skipped, total_files, runtime)

    def _process_archive_core(
            self,
            service: str,
            archive_path: str,
            output: Optional[str],
            n: int,
            generate_ids: bool,
            consolidate_header: bool,
            consolidate_citations: bool,
            include_raw_citations: bool,
            include_raw_affiliations: bool,
            tei_coordinates: bool,
            segment_sentences: bool,
            force: bool,
            verbose: bool,
            flavor: Optional[str],
            json_output: bool,
            markdown_output: bool
    ) -> Tuple[int, int, int, int]:
        """Stream and process an archive; return (total, processed, errors, skipped).

        Does not print the final summary (the caller does), so it can be
        aggregated with other inputs when resolving a glob pattern.
        """
        batch_size_pdf = self.config["batch_size"]

        # Results must survive the temporary extraction directories, so when no
        # output is given we default to a directory named after the archive. For
        # s3 archives there is no local home, so use the object's basename.
        if output is None:
            if self._is_s3(archive_path):
                output = self._archive_stem(self._s3_basename(archive_path))
            else:
                output = self._archive_stem(archive_path)

        try:
            kind, archive, member_names = self._open_archive(archive_path)
        except Exception as e:
            self.logger.error(f"Could not open archive {archive_path}: {str(e)}")
            return 0, 0, 0, 0

        processed_files_count = 0
        errors_files_count = 0
        skipped_files_count = 0
        total_files = 0

        try:
            eligible_members = [
                name for name in member_names
                if self._is_eligible_input(os.path.basename(name), service)
            ]
            total_files = len(eligible_members)
            if total_files == 0:
                self.logger.warning(f"No eligible files found in archive {archive_path}")
                return 0, 0, 0, 0

            print(f"Found {total_files} file(s) to process in {archive_path}")

            for chunk_start in range(0, total_files, batch_size_pdf):
                chunk = eligible_members[chunk_start:chunk_start + batch_size_pdf]
                temp_dir = tempfile.mkdtemp(prefix="grobid_archive_")
                try:
                    extracted_files = []
                    for member_name in chunk:
                        if verbose:
                            self.logger.info(f"Extracting {member_name} from {archive_path}")
                        extracted = self._extract_archive_member(kind, archive, member_name, temp_dir)
                        if extracted is not None:
                            extracted_files.append(extracted)

                    if not extracted_files:
                        continue

                    batch_processed, batch_errors, batch_skipped = self.process_batch(
                        service,
                        extracted_files,
                        temp_dir,
                        output,
                        n,
                        generate_ids,
                        consolidate_header,
                        consolidate_citations,
                        include_raw_citations,
                        include_raw_affiliations,
                        tei_coordinates,
                        segment_sentences,
                        force,
                        verbose,
                        flavor,
                        json_output,
                        markdown_output
                    )
                    processed_files_count += batch_processed
                    errors_files_count += batch_errors
                    skipped_files_count += batch_skipped
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)
        finally:
            try:
                archive.close()
            finally:
                # ZipFile does not close a file object we passed in (the s3 stream)
                stream = getattr(archive, "_grobid_stream", None)
                if stream is not None:
                    stream.close()

        return total_files, processed_files_count, errors_files_count, skipped_files_count

    def _process_remote_files(
            self,
            service: str,
            uris: list,
            output: Optional[str],
            n: int,
            generate_ids: bool,
            consolidate_header: bool,
            consolidate_citations: bool,
            include_raw_citations: bool,
            include_raw_affiliations: bool,
            tei_coordinates: bool,
            segment_sentences: bool,
            force: bool,
            verbose: bool,
            flavor: Optional[str],
            json_output: bool,
            markdown_output: bool
    ) -> Tuple[int, int, int, int]:
        """Stream loose remote (s3) files to a temp dir in chunks and process them.

        Returns (total, processed, errors, skipped). Objects are fetched a
        batch at a time and deleted before the next chunk, so disk stays bounded.
        """
        total = len(uris)
        if total == 0:
            return 0, 0, 0, 0
        # Remote files have no local home; default output to the current dir.
        if output is None:
            output = "."

        batch_size_pdf = self.config["batch_size"]
        print(f"Found {total} remote file(s) to process")
        processed_count = 0
        error_count = 0
        skipped_count = 0

        for chunk_start in range(0, total, batch_size_pdf):
            chunk = uris[chunk_start:chunk_start + batch_size_pdf]
            temp_dir = tempfile.mkdtemp(prefix="grobid_s3_")
            try:
                local_files = []
                for uri in chunk:
                    if verbose:
                        self.logger.info(f"Fetching {uri}")
                    dest = os.path.join(temp_dir, self._s3_basename(uri))
                    try:
                        with self._s3_open(uri) as src, open(dest, "wb") as out_file:
                            shutil.copyfileobj(src, out_file)
                        local_files.append(dest)
                    except Exception as e:
                        self.logger.error(f"Failed to fetch {uri}: {str(e)}")
                        error_count += 1

                if not local_files:
                    continue

                batch_processed, batch_errors, batch_skipped = self.process_batch(
                    service,
                    local_files,
                    temp_dir,
                    output,
                    n,
                    generate_ids,
                    consolidate_header,
                    consolidate_citations,
                    include_raw_citations,
                    include_raw_affiliations,
                    tei_coordinates,
                    segment_sentences,
                    force,
                    verbose,
                    flavor,
                    json_output,
                    markdown_output
                )
                processed_count += batch_processed
                error_count += batch_errors
                skipped_count += batch_skipped
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        return total, processed_count, error_count, skipped_count

    def process_batch(
            self,
            service: str,
            input_files: list,
            input_path: str,
            output: Optional[str],
            n: int,
            generate_ids: bool,
            consolidate_header: bool,
            consolidate_citations: bool,
            include_raw_citations: bool,
            include_raw_affiliations: bool,
            tei_coordinates: bool,
            segment_sentences: bool,
            force: bool,
            verbose: bool = False,
            flavor: Optional[str] = None,
            json_output: bool = False,
            markdown_output: bool = False
    ) -> Tuple[int, int, int]:
        batch_start_time = time.time()
        if verbose:
            self.logger.info(f"{len(input_files)} files to process in current batch")

        processed_count = 0
        error_count = 0
        skipped_count = 0

        # we use ThreadPoolExecutor and not ProcessPoolExecutor because it is an I/O intensive process
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as executor:
            # with concurrent.futures.ProcessPoolExecutor(max_workers=n) as executor:
            results = []
            for input_file in input_files:
                # check if TEI file is already produced
                filename = self._output_file_name(input_file, input_path, output)
                if not force and os.path.isfile(filename):
                    self.logger.info(
                        f"{filename} already exists, skipping... (use --force to reprocess pdf input files)")
                    skipped_count += 1

                    # Check if JSON output is needed but JSON file doesn't exist
                    if json_output:
                        json_filename = filename.replace('.grobid.tei.xml', '.json')
                        # Expand ~ to home directory before checking file existence
                        json_filename_expanded = os.path.expanduser(json_filename)
                        if not os.path.isfile(json_filename_expanded):
                            self.logger.info(f"JSON file {json_filename} does not exist, generating JSON from existing TEI...")
                            try:
                                converter: Any = TEI2LossyJSONConverter()
                                json_data = converter.convert_tei_file(filename, stream=False)

                                if json_data:
                                    self._write_atomic(
                                        json_filename_expanded,
                                        json.dumps(json_data, indent=2, ensure_ascii=False))
                                    self.logger.debug(f"Successfully created JSON file: {json_filename_expanded}")
                                else:
                                    self.logger.warning(f"Failed to convert TEI to JSON for {filename}")
                            except Exception as e:
                                self.logger.error(f"Failed to convert TEI to JSON for {filename}: {str(e)}")

                    # Check if Markdown output is needed but Markdown file doesn't exist
                    if markdown_output:
                        markdown_filename = filename.replace('.grobid.tei.xml', '.md')
                        # Expand ~ to home directory before checking file existence
                        markdown_filename_expanded = os.path.expanduser(markdown_filename)
                        if not os.path.isfile(markdown_filename_expanded):
                            self.logger.info(f"Markdown file {markdown_filename} does not exist, generating Markdown from existing TEI...")
                            try:
                                from .format.TEI2Markdown import TEI2MarkdownConverter
                                converter = TEI2MarkdownConverter()
                                markdown_data = converter.convert_tei_file(filename)

                                if markdown_data:
                                    self._write_atomic(markdown_filename_expanded, markdown_data)
                                    self.logger.debug(f"Successfully created Markdown file: {markdown_filename_expanded}")
                                else:
                                    self.logger.warning(f"Failed to convert TEI to Markdown for {filename}")
                            except Exception as e:
                                self.logger.error(f"Failed to convert TEI to Markdown for {filename}: {str(e)}")

                    continue

                selected_process: Any = self.process_pdf
                if service == 'processCitationList':
                    selected_process = self.process_txt

                if verbose:
                    self.logger.info(f"Adding {input_file} to the queue")

                r = executor.submit(
                    selected_process,
                    service,
                    input_file,
                    generate_ids,
                    consolidate_header,
                    consolidate_citations,
                    include_raw_citations,
                    include_raw_affiliations,
                    tei_coordinates,
                    segment_sentences,
                    flavor,
                    -1,
                    -1)

                results.append(r)

        for r in concurrent.futures.as_completed(results):
            input_file, status, text = r.result()
            filename = self._output_file_name(input_file, input_path, output)

            if status != 200 or text is None:
                self.logger.error(f"Processing of {input_file} failed with error {status}: {text}")
                error_count += 1
                # writing error file with suffixed error code
                try:
                    error_filename = filename.replace(".grobid.tei.xml", f"_{status}.txt")
                    self._write_atomic(error_filename, text if text is not None else "")
                    self.logger.info(f"Error details written to {error_filename}")
                except OSError as e:
                    self.logger.error(f"Failed to write error file {filename}: {str(e)}")
            else:
                processed_count += 1
                # writing TEI file
                try:
                    self._write_atomic(filename, text)
                    self.logger.debug(f"Successfully wrote TEI file: {filename}")
                    
                    # Convert to JSON if requested
                    if json_output:
                        try:
                            converter = TEI2LossyJSONConverter()
                            json_data = converter.convert_tei_file(filename, stream=False)
                            
                            if json_data:
                                json_filename = filename.replace('.grobid.tei.xml', '.json')
                                # Always write JSON file when TEI is written (respects --force behavior)
                                json_filename_expanded = os.path.expanduser(json_filename)
                                self._write_atomic(
                                    json_filename_expanded,
                                    json.dumps(json_data, indent=2, ensure_ascii=False))
                                self.logger.debug(f"Successfully wrote JSON file: {json_filename_expanded}")
                            else:
                                self.logger.warning(f"Failed to convert TEI to JSON for {filename}")
                        except Exception as e:
                            self.logger.error(f"Failed to convert TEI to JSON for {filename}: {str(e)}")
                    
                    # Convert to Markdown if requested
                    if markdown_output:
                        try:
                            from .format.TEI2Markdown import TEI2MarkdownConverter
                            converter = TEI2MarkdownConverter()
                            markdown_data = converter.convert_tei_file(filename)

                            if markdown_data is not None:
                                markdown_filename = filename.replace('.grobid.tei.xml', '.md')
                                # Always write Markdown file when TEI is written (respects --force behavior)
                                markdown_filename_expanded = os.path.expanduser(markdown_filename)
                                self._write_atomic(markdown_filename_expanded, markdown_data)
                                self.logger.debug(f"Successfully wrote Markdown file: {markdown_filename_expanded}")
                            else:
                                self.logger.warning(f"Failed to convert TEI to Markdown for {filename}")
                        except Exception as e:
                            self.logger.error(f"Failed to convert TEI to Markdown for {filename}: {str(e)}")
                            
                except OSError as e:
                    self.logger.error(f"Failed to write TEI XML file {filename}: {str(e)}")

        # Calculate batch statistics
        batch_runtime = time.time() - batch_start_time
        batch_docs_per_second = processed_count / batch_runtime if batch_runtime > 0 else 0
        batch_seconds_per_docs = batch_runtime / processed_count if processed_count > 0 else 0
        
        if verbose:
            self.logger.info(f"⏱️  Runtime: {batch_runtime:.2f} seconds")
            self.logger.info(f"🚀 Speed: {batch_docs_per_second:.2f} documents/second")
            self.logger.info(f" Throughput: {batch_seconds_per_docs:.2f} seconds/document")

        return processed_count, error_count, skipped_count

    def process_pdf(
            self,
            service: str,
            pdf_file: str,
            generate_ids: bool,
            consolidate_header: bool,
            consolidate_citations: bool,
            include_raw_citations: bool,
            include_raw_affiliations: bool,
            tei_coordinates: bool,
            segment_sentences: bool,
            flavor: Optional[str] = None,
            start: int = -1,
            end: int = -1
    ) -> Tuple[str, int, Optional[str]]:
        pdf_handle = None
        try:
            pdf_handle = open(pdf_file, "rb")
            
            files = {
                "input": (
                    pdf_file,
                    pdf_handle,
                    "application/pdf",
                    {"Expires": "0"},
                )
            }

            the_url = self.get_server_url(service)

            # set the GROBID parameters
            the_data = {}
            if generate_ids:
                the_data["generateIDs"] = "1"
            if consolidate_header:
                the_data["consolidateHeader"] = "1"
            if consolidate_citations:
                the_data["consolidateCitations"] = "1"
            if include_raw_citations:
                the_data["includeRawCitations"] = "1"
            if include_raw_affiliations:
                the_data["includeRawAffiliations"] = "1"
            if tei_coordinates:
                the_data["teiCoordinates"] = self.config["coordinates"]
            if segment_sentences:
                the_data["segmentSentences"] = "1"
            if flavor:
                the_data["flavor"] = flavor
            if start and start > 0:
                the_data["start"] = str(start)
            if end and end > 0:
                the_data["end"] = str(end)

            res, status = self.post(
                url=the_url, files=files, data=the_data, headers={"Accept": "text/plain"},
                timeout=self.config['timeout']
            )

            if status == 503:
                return self._handle_server_busy_retry(
                    pdf_file,
                    self.process_pdf,
                    service,
                    pdf_file,
                    generate_ids,
                    consolidate_header,
                    consolidate_citations,
                    include_raw_citations,
                    include_raw_affiliations,
                    tei_coordinates,
                    segment_sentences,
                    flavor,
                    start,
                    end
                )

            return (pdf_file, status, res.text)
        
        except IOError as e:
            self.logger.error(f"Failed to open PDF file {pdf_file}: {str(e)}")
            return (pdf_file, 400, f"Failed to open file: {str(e)}")
        except requests.exceptions.ReadTimeout as e:
            self.logger.error(f"Request timeout for {pdf_file}: {str(e)}")
            return (pdf_file, 408, f"Request timeout: {str(e)}")
        except requests.exceptions.RequestException as e:
            return self._handle_request_error(pdf_file, e)
        except Exception as e:
            return self._handle_unexpected_error(pdf_file, e)
        finally:
            if pdf_handle:
                pdf_handle.close()

    def get_server_url(self, service: str) -> str:
        return self.config['grobid_server'] + "/api/" + service

    def process_txt(
            self,
            service: str,
            txt_file: str,
            generate_ids: bool,
            consolidate_header: bool,
            consolidate_citations: bool,
            include_raw_citations: bool,
            include_raw_affiliations: bool,
            tei_coordinates: bool,
            segment_sentences: bool,
            flavor: Optional[str] = None,
            start_page: int = -1,
            end_page: int = -1
    ) -> Tuple[str, int, Optional[str]]:
        # create request based on file content
        try:
            with open(txt_file, 'r', encoding='utf-8') as f:
                references = [line.rstrip() for line in f]
        except IOError as e:
            self.logger.error(f"Failed to read text file {txt_file}: {str(e)}")
            return (txt_file, 500, f"Failed to read file: {str(e)}")
        except UnicodeDecodeError as e:
            self.logger.error(f"Unicode decode error reading {txt_file}: {str(e)}")
            return (txt_file, 500, f"Unicode decode error: {str(e)}")

        the_url = self.get_server_url(service)

        # set the GROBID parameters
        the_data: dict = {}
        if consolidate_citations:
            the_data["consolidateCitations"] = "1"
        if include_raw_citations:
            the_data["includeRawCitations"] = "1"
        the_data["citations"] = references

        try:
            res, status = self.post(
                url=the_url, data=the_data, headers={"Accept": "application/xml"}
            )

            if status == 503:
                return self._handle_server_busy_retry(
                    txt_file,
                    self.process_txt,
                    service,
                    txt_file,
                    generate_ids,
                    consolidate_header,
                    consolidate_citations,
                    include_raw_citations,
                    include_raw_affiliations,
                    tei_coordinates,
                    segment_sentences
                )
        except requests.exceptions.RequestException as e:
            return self._handle_request_error(txt_file, e)
        except Exception as e:
            return self._handle_unexpected_error(txt_file, e)

        return (txt_file, status, res.text)


def main() -> None:
    # Basic logging setup for initialization only
    # The actual logging configuration will be done by GrobidClient based on config.json
    temp_logger = logging.getLogger(__name__)
    temp_logger.propagate = False  # Prevent propagation to avoid duplicates
    if not temp_logger.handlers:
        temp_handler = logging.StreamHandler()
        temp_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
        temp_logger.addHandler(temp_handler)
        temp_logger.setLevel(logging.INFO)

    valid_services = [
        "processFulltextDocument",
        "processHeaderDocument",
        "processReferences",
        "processCitationList",
        "processCitationPatentST36",
        "processCitationPatentPDF"
    ]

    parser = argparse.ArgumentParser(description="Client for GROBID services")
    parser.add_argument(
        "service",
        choices=valid_services,
        help="Grobid service to be called.",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="input to process: a directory, a file, a .zip/.tar/.tar.gz archive, a glob pattern (e.g. '**/*.pdf', 'paper*.zip'), or an s3:// object/prefix/glob (requires the 's3' extra). Archives are streamed and never fully decompressed."
    )
    parser.add_argument(
        "--input-list",
        default=None,
        help="path to a text file with one input per line (local path, glob or s3:// URI); all are processed together. Lines starting with '#' are ignored."
    )
    parser.add_argument(
        "--output",
        default=None,
        help="path to the directory where to put the results (optional)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to the config file (optional)",
    )
    parser.add_argument(
        "--n",
        default=10,
        help="concurrency for service usage"
    )
    parser.add_argument(
        "--generate_ids", "--generateIDs",
        dest="generate_ids",
        action="store_true",
        help="generate random xml:id to textual XML elements of the result files",
    )
    parser.add_argument(
        "--consolidate_header",
        action="store_true",
        help="call GROBID with consolidation of the metadata extracted from the header",
    )
    parser.add_argument(
        "--consolidate_citations",
        action="store_true",
        help="call GROBID with consolidation of the extracted bibliographical references",
    )
    parser.add_argument(
        "--include_raw_citations",
        action="store_true",
        help="call GROBID requesting the extraction of raw citations",
    )
    parser.add_argument(
        "--include_raw_affiliations",
        action="store_true",
        help="call GROBID requesting the extraction of raw affiliations",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="force re-processing pdf input files when tei output files already exist",
    )
    parser.add_argument(
        "--tei_coordinates", "--teiCoordinates",
        dest="tei_coordinates",
        action="store_true",
        help="add the original PDF coordinates (bounding boxes) to the extracted elements",
    )
    parser.add_argument(
        "--segment_sentences", "--segmentSentences",
        dest="segment_sentences",
        action="store_true",
        help="segment sentences in the text content of the document with additional <s> elements",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable detailed logging (INFO level) - shows file-by-file processing details, server status, and JSON conversion messages. Without this flag, only essential statistics and warnings/errors are shown.",
    )

    parser.add_argument(
        "--flavor",
        default=None,
        help="Define the flavor to be used for the fulltext extraction",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="GROBID server URL override of the config file. If config not provided, default is http://localhost:8070",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Convert TEI output to JSON format using the TEI2LossyJSON converter",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Convert TEI output to Markdown format",
    )

    args = parser.parse_args()

    input_path = args.input
    input_list = args.input_list
    config_path = args.config
    output_path = args.output
    flavor = args.flavor
    json_output = args.json
    markdown_output = args.markdown

    # Initialize n with default value
    n = 10
    if args.n is not None:
        try:
            n = int(args.n)
        except ValueError:
            temp_logger.warning(f"Invalid concurrency parameter n: {args.n}. Using default value n = 10")

    # Initialize GrobidClient which will configure logging based on config.json and verbose flag
    try:
        # Only pass grobid_server if it was explicitly provided (not the default)
        client_kwargs = {'config_path': config_path, 'verbose': args.verbose}
        if args.server is not None:  # Only override if user specified a different server
            client_kwargs['grobid_server'] = args.server

        client = GrobidClient(**client_kwargs)
        # Now use the client's logger for all subsequent logging
        logger = client.logger
    except ServerUnavailableException as e:
        temp_logger.error(f"Server unavailable: {str(e)}")
        exit(1)
    except Exception as e:
        temp_logger.error(f"Failed to initialize GrobidClient: {str(e)}")
        exit(1)

    # if output path does not exist, we create it
    if output_path is not None and not os.path.isdir(output_path):
        try:
            logger.info(f"Output directory does not exist but will be created: {output_path}")
            os.makedirs(output_path)
            logger.info(f"Successfully created the directory {output_path}")
        except OSError as e:
            logger.error(f"Creation of the directory {output_path} failed: {str(e)}")
            exit(1)

    service = args.service
    generate_ids = args.generate_ids
    consolidate_header = args.consolidate_header
    consolidate_citations = args.consolidate_citations
    include_raw_citations = args.include_raw_citations
    include_raw_affiliations = args.include_raw_affiliations
    force = args.force
    tei_coordinates = args.tei_coordinates
    segment_sentences = args.segment_sentences
    verbose = args.verbose

    if service is None or service not in valid_services:
        logger.error(f"Missing or invalid service '{service}', must be one of {valid_services}")
        exit(1)

    # Build the list of inputs from --input and/or --input-list
    inputs = []
    if input_path is not None:
        inputs.append(input_path)
    if input_list is not None:
        try:
            with open(input_list, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        inputs.append(line)
        except OSError as e:
            logger.error(f"Could not read --input-list {input_list}: {str(e)}")
            exit(1)

    if not inputs:
        logger.error("No input provided (use --input and/or --input-list)")
        exit(1)

    start_time = time.time()

    try:
        client.process_paths(
            service,
            inputs,
            output=output_path,
            n=n,
            generate_ids=generate_ids,
            consolidate_header=consolidate_header,
            consolidate_citations=consolidate_citations,
            include_raw_citations=include_raw_citations,
            include_raw_affiliations=include_raw_affiliations,
            tei_coordinates=tei_coordinates,
            segment_sentences=segment_sentences,
            force=force,
            verbose=verbose,
            flavor=flavor,
            json_output=json_output,
            markdown_output=markdown_output
        )
    except Exception as e:
        logger.error(f"Processing failed: {str(e)}")
        exit(1)

    runtime = round(time.time() - start_time, 3)
    print(f"Processing completed in {runtime} seconds")


if __name__ == "__main__":
    main()
