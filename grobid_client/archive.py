"""
Read documents out of an archive without unpacking it.

A corpus usually travels as one zip or tarball, and the documents inside it are
wanted one at a time: unpacking the whole thing first needs as much disk as the
corpus, and every byte written is a byte read back again. So entries are read
straight into memory instead, and named after themselves, which is all that
both consumers need - the client posts a named stream to GROBID, and the format
converters parse one. A zip in an object store is range-streamed: only its
central directory and the entries actually asked for cross the network.

This module holds no state beyond the open archive, so it is shared by the
client (PDFs in, TEI out) and by the converters (TEI in, JSON or Markdown out).
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import tarfile
import zipfile
from typing import Any, BinaryIO, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Archive extensions that can be streamed entry-by-entry instead of being fully
# decompressed first. Order matters: multi-dot suffixes must come before their
# single-dot prefixes when stripping (see archive_stem).
ARCHIVE_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tgz", ".tbz2", ".zip", ".tar")

S3_SCHEME = "s3://"


# ---- S3 support (optional 's3' extra: smart_open + boto3) ----

def is_s3(path: Any) -> bool:
    """Return True if path is an s3:// URI."""
    return isinstance(path, str) and path.startswith(S3_SCHEME)


def split_s3(uri: str) -> Tuple[str, str]:
    """Split an s3://bucket/key URI into (bucket, key)."""
    bucket, _, key = uri[len(S3_SCHEME):].partition("/")
    return bucket, key


def s3_basename(uri: str) -> str:
    """Return the last path component of an s3:// key."""
    return split_s3(uri)[1].rsplit("/", 1)[-1]


def import_smart_open() -> Any:
    try:
        import smart_open  # noqa: F401
        return smart_open
    except ImportError as e:
        raise ImportError(
            "Reading from s3:// requires the optional 's3' extra. "
            "Install it with: pip install grobid-client-python[s3]"
        ) from e


def s3_open(uri: str) -> BinaryIO:
    """Open an S3 object as a seekable binary stream (HTTP range-streamed).

    The returned stream lets zipfile read only the central directory and the
    requested entries, so a remote zip is never fully downloaded.
    """
    return import_smart_open().open(uri, "rb")


# ---- Archive naming ----

def looks_like_archive(path: str) -> bool:
    """Return True if the path/URI name has a known archive extension."""
    lower = path.lower()
    return any(lower.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def is_archive(path: str) -> bool:
    """Return True if path is an existing local zip/tar archive file."""
    return os.path.isfile(path) and looks_like_archive(path)


def archive_stem(path: str) -> str:
    """Strip a known archive extension from path (e.g. docs.tar.gz -> docs)."""
    lower = path.lower()
    for ext in ARCHIVE_EXTENSIONS:
        if lower.endswith(ext):
            return path[:-len(ext)]
    return os.path.splitext(path)[0]


def safe_member_path(dest_dir: str, arcname: str) -> Optional[str]:
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


class ArchiveReader:
    """An open zip or tar archive, read one entry at a time.

    ``names`` lists the regular files it holds (directories are skipped), and
    each of them can be read into memory as a named stream, or extracted to a
    directory for the rare consumer that insists on a path.

    Local zips, local tarballs (including compressed ones) and ``s3://`` zips
    all open the same way. The archive - and the remote stream under it, if any
    - is closed by ``close()`` or by leaving the ``with`` block.
    """

    def __init__(self, path: str, log: Optional[logging.Logger] = None) -> None:
        self.path = path
        # Warnings about entries belong in the caller's log, not in this
        # module's, whenever the caller has one.
        self.logger = log or logger
        self._stream: Optional[BinaryIO] = None
        self.kind, self.archive, self.names = self._open(path)

    def _open(self, path: str) -> Tuple[str, Any, List[str]]:
        """Open the archive and list what it holds.

        A ZipFile and a TarFile share no interface worth using here: which one
        it is, is what ``kind`` is for, and it is that tag - not the type - the
        rest of the class dispatches on.
        """
        archive: Any
        if is_s3(path):
            if not path.lower().endswith(".zip"):
                raise ValueError(
                    f"Only .zip archives can be range-streamed over s3://: {path}"
                )
            # Kept so that close() can release it: ZipFile does not close a file
            # object it was handed.
            self._stream = s3_open(path)
            archive = zipfile.ZipFile(self._stream)
            return "zip", archive, self._zip_names(archive)

        if path.lower().endswith(".zip"):
            archive = zipfile.ZipFile(path)
            return "zip", archive, self._zip_names(archive)

        archive = tarfile.open(path, "r:*")
        return "tar", archive, [m.name for m in archive.getmembers() if m.isfile()]

    @staticmethod
    def _zip_names(archive: zipfile.ZipFile) -> List[str]:
        return [n for n in archive.namelist() if not n.endswith("/")]

    def open_member(self, name: str) -> Optional[BinaryIO]:
        """Open one entry as a binary stream. The caller closes it."""
        if self.kind == "zip":
            return self.archive.open(name)
        return self.archive.extractfile(self.archive.getmember(name))

    def read_member_data(self, name: str) -> Optional[Tuple[str, bytes]]:
        """Read one entry into memory, as its safe name and its bytes.

        The entry never touches the disk. Returns None if the entry is
        unusable - an unsafe name, or a tar member with no content.
        """
        safe_name = safe_member_path("", name)
        if safe_name is None:
            self.logger.warning(f"Skipping archive entry with unsafe path: {name}")
            return None

        source = self.open_member(name)
        if source is None:
            return None

        try:
            return safe_name, source.read()
        finally:
            source.close()

    def read_member(self, name: str) -> Optional[BinaryIO]:
        """Read one entry into memory as a named document.

        The entry goes straight from the archive to whoever consumes named
        streams - ``process_pdf``, or a TEI converter. The name is the entry's
        own (sanitized the way extraction is), and that name is what output
        files are derived from.
        """
        entry = self.read_member_data(name)
        if entry is None:
            return None

        safe_name, data = entry
        document = io.BytesIO(data)
        document.name = safe_name  # type: ignore[attr-defined]
        return document

    def extract_member(self, name: str, dest_dir: str) -> Optional[str]:
        """Stream one entry to dest_dir, preserving its relative path.

        Returns the path of the extracted file, or None if it was skipped. This
        is for consumers that can only read from a path; everything else should
        use ``read_member`` and leave the disk alone.
        """
        target = safe_member_path(dest_dir, name)
        if target is None:
            self.logger.warning(f"Skipping archive entry with unsafe path: {name}")
            return None

        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)

        source = self.open_member(name)
        if source is None:
            return None

        try:
            with open(target, "wb") as out_file:
                shutil.copyfileobj(source, out_file)
        finally:
            source.close()

        return target

    def close(self) -> None:
        try:
            self.archive.close()
        finally:
            if self._stream is not None:
                self._stream.close()
                self._stream = None

    def __enter__(self) -> "ArchiveReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
