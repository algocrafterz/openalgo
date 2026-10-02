"""Write-once run folders: built in a temp sibling directory and renamed into place.

A run is either written completely or not at all. Files are hashed into the manifest,
the manifest is written last, every file is made read-only, and the folder is then moved
to its final name in one atomic rename. Nothing is ever replaced.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = "manifest.json"


def _encode(content: bytes | str | Mapping | list) -> bytes:
    """Serialise one artifact: bytes as-is, str as UTF-8, JSON containers pretty-printed."""
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, (Mapping, list)):
        return (json.dumps(content, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
            "utf-8"
        )
    raise TypeError(f"unsupported artifact content type: {type(content).__name__}")


def _clean_relpath(relpath: str) -> str:
    """Validate and normalise a relative posix path, raising ValueError when unsafe."""
    if not isinstance(relpath, str) or not relpath.strip():
        raise ValueError("artifact path must be a non-empty string")
    if relpath.startswith("/") or relpath.startswith("\\") or PurePosixPath(relpath).is_absolute():
        raise ValueError(f"artifact path must be relative: {relpath!r}")
    parts = relpath.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"artifact path has an empty or dot segment: {relpath!r}")
    return "/".join(parts)


class RunFolder:
    """Collects a run's files in memory, then commits them atomically."""

    def __init__(self, runs_dir: Path, run_id: str) -> None:
        """Create a builder for `runs_dir/run_id`. Nothing touches disk until commit."""
        self._runs_dir = Path(runs_dir)
        self._run_id = run_id
        self._files: dict[str, bytes] = {}

    def add(self, relpath: str, content: bytes | str | Mapping | list) -> None:
        """Queue one file.

        Raises:
            ValueError: Unsafe or duplicate path, or the reserved name manifest.json.
        """
        rel = _clean_relpath(relpath)
        if rel == MANIFEST_NAME:
            raise ValueError("manifest.json is written by commit()")
        if rel in self._files:
            raise ValueError(f"duplicate artifact path: {rel}")
        self._files[rel] = _encode(content)

    def commit(self, manifest_builder: Callable[[dict[str, str]], dict[str, Any]]) -> Path:
        """Write the folder and return its final path.

        Raises:
            FileExistsError: The run id already exists.
            ValueError: The manifest's "artifacts" does not equal the computed hash map.
        """
        target = self._runs_dir / self._run_id
        if target.exists():
            raise FileExistsError(str(target))
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=self._runs_dir, prefix=f".{self._run_id}.tmp-"))
        try:
            artifacts: dict[str, str] = {}
            for rel in sorted(self._files):
                path = tmp / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self._files[rel])
                artifacts[rel] = hashlib.sha256(self._files[rel]).hexdigest()
            manifest = manifest_builder(dict(artifacts))
            if not isinstance(manifest, Mapping) or manifest.get("artifacts") != artifacts:
                raise ValueError("manifest['artifacts'] must equal the computed artifact hashes")
            (tmp / MANIFEST_NAME).write_bytes(_encode(manifest))
            for path in tmp.rglob("*"):
                if path.is_file():
                    path.chmod(0o444)
            if target.exists():
                raise FileExistsError(str(target))
            try:
                os.rename(tmp, target)
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    raise FileExistsError(str(target)) from exc
                raise
        except BaseException:
            _remove_tree(tmp)
            raise
        return target


def _remove_tree(path: Path) -> None:
    """Delete a temp tree, tolerating the read-only files this module creates."""
    if not path.exists():
        return
    for p in path.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
    shutil.rmtree(path, ignore_errors=True)
