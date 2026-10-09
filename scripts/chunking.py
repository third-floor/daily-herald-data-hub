"""
Size-capped JSON chunk writer shared by prepare_sources.py and build.py.

Each chunk is a valid JSON array written with ONE RECORD PER LINE:

    [
    {...record...},
    {...record...}
    ]

That keeps every file loadable with a single `JSON.parse` / `json.load`, while
letting git store and diff the files line by line (much smaller history than
one giant minified line).

Chunks are cut by *byte size*, not record count, so they never exceed
`max_bytes` (default 20 MB) regardless of how rich the records are.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_MAX_BYTES = 20_000_000  # 20 MB (decimal), the hard cap per file


def dumps_line(record) -> bytes:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ChunkWriter:
    """Streams records into part-0001.json, part-0002.json, ... under `folder`."""

    def __init__(self, folder: str | Path, max_bytes: int = DEFAULT_MAX_BYTES,
                 prefix: str = "part"):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.prefix = prefix
        self.parts: list[dict] = []      # [{path, bytes, records}]
        self.oversized: list[str] = []   # ids of records larger than max_bytes
        self._lines: list[bytes] = []
        self._size = 4                   # "[\n" + "\n]"
        self._count = 0

    @property
    def current_part_index(self) -> int:
        """0-based index of the part the NEXT record will land in (if it fits)."""
        return len(self.parts)

    def would_overflow(self, line: bytes) -> bool:
        extra = len(line) + (2 if self._lines else 0)  # ",\n" separator
        return bool(self._lines) and self._size + extra > self.max_bytes

    def add(self, record, record_id: str = "") -> int:
        """Add a record; returns the 0-based part index it was written to."""
        line = dumps_line(record)
        if self.would_overflow(line):
            self.flush()
        if len(line) + 4 > self.max_bytes:
            self.oversized.append(record_id or "?")
        self._size += len(line) + (2 if self._lines else 0)
        self._lines.append(line)
        self._count += 1
        return len(self.parts)

    def flush(self):
        if not self._lines:
            return
        name = f"{self.prefix}-{len(self.parts) + 1:04d}.json"
        path = self.folder / name
        data = b"[\n" + b",\n".join(self._lines) + b"\n]"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self.parts.append({"file": name, "bytes": len(data), "records": self._count})
        self._lines, self._size, self._count = [], 4, 0

    def close(self) -> list[dict]:
        self.flush()
        return self.parts


def clear_parts(folder: str | Path, prefix: str = "part"):
    """Delete old chunk files so a re-run never leaves stale parts behind."""
    folder = Path(folder)
    if folder.exists():
        for p in folder.glob(f"{prefix}-*.json"):
            p.unlink()


def read_parts(folder: str | Path, prefix: str = "part"):
    """Yield records from every chunk in a folder, in order."""
    for p in sorted(Path(folder).glob(f"{prefix}-*.json")):
        with open(p, encoding="utf-8") as fh:
            for rec in json.load(fh):
                yield rec
