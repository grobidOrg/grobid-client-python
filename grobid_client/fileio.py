"""
Write output files whole, or not at all.

A run that is killed - by an OOM, a wall-clock limit, a Ctrl-C - must not leave
a half-written result behind, because a truncated file is indistinguishable
from a complete one to everything that decides what still has to be done by
looking at what is already there. Every output this package writes therefore
goes through the same rename-into-place dance.
"""
from __future__ import annotations

import os
import pathlib
import tempfile


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


DEFAULT_FILE_MODE = _default_file_mode()


def unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def write_atomic(filename: str, text: str) -> None:
    """Write text to filename via a temp file in the same directory, then os.replace.

    A killed process must never leave a partial output behind. Processing
    decides a document is already done with os.path.isfile() alone, so a result
    truncated by an OOM kill or a wall-clock timeout is indistinguishable from a
    complete one and is skipped on every subsequent run -- the corruption is
    permanent and silent. Writing to a temp file and renaming means the
    destination either does not exist or is the whole document.

    The temp file goes in the DESTINATION directory, not TMPDIR: os.replace
    is only atomic within a filesystem, and on a cluster TMPDIR is usually a
    different mount. The "." prefix and ".tmp" suffix keep the temp file from
    matching *.grobid.tei.xml or *_[0-9]*.txt, so output counting is
    unaffected while a write is in flight.

    Residual risk: a SIGKILL between mkstemp and replace leaks a temp file.
    That is visible and harmless, unlike a truncated output.
    """
    dest = pathlib.Path(os.path.expanduser(filename))
    dest.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp names are unique, so concurrent writers (threads, or the
    # processes of a conversion pool) cannot collide on the temp path.
    fd, tmp_path = tempfile.mkstemp(dir=str(dest.parent), prefix=".", suffix=".tmp")
    try:
        tmp_file = os.fdopen(fd, "w", encoding="utf8")
    except BaseException:
        # fdopen did not take ownership of fd, so we still have to close it.
        # Past this point the file object owns it and closing it here too
        # could close an unrelated descriptor that reused the number.
        os.close(fd)
        unlink_quietly(tmp_path)
        raise
    try:
        with tmp_file:
            tmp_file.write(text)
        os.chmod(tmp_path, DEFAULT_FILE_MODE)   # mkstemp gives 0600
        os.replace(tmp_path, str(dest))
    except BaseException:
        unlink_quietly(tmp_path)
        raise
