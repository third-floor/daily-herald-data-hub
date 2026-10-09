"""
STEP 1 — Convert the original files (on Google Drive) into size-capped JSON
chunks under data/source/, ready to commit to GitHub.

What it does
------------
* Reads every input file for each named source (JSON or XLSX).
* JSON: accepts a bare list of records, or a wrapper object like
  {"schema_version": ..., "records": [...]} (the wrapper metadata is kept in
  the manifest).
* XLSX: streams the first sheet (or a named one) row by row, and
    - renames repeated column headers (gem_model, gem_model__2, ...) instead of
      silently overwriting or mis-labelling them;
    - decodes cells that contain JSON ([...] / {...}) into real JSON;
      cells that look like JSON but don't parse (e.g. text truncated by
      Excel's cell limit) are kept as strings and counted in the manifest;
    - turns empty / "None" / "nan" cells into null.
* Removes duplicate uids WITHIN a source (later file wins — order your inputs
  oldest → newest) and writes a CSV report of what was dropped.
* Optionally removes unwanted fields: give a source "drop_fields": a list of
  names or wildcard patterns, e.g. ["output - *"] to leave out the old
  Gemini 2.5 transcription columns.
* Adds a small `_src` field to every record: {"file": ..., "row": ...}.
  Nothing else in the record is changed (no text fixing — that happens in
  build.py, so the source layer stays faithful to the originals).
* Writes data/source/<source_name>/part-0001.json ... (each <= max size)
  and data/source/manifest.json.

Usage
-----
From Python / Colab:
    from prepare_sources import prepare
    prepare(SOURCES, out_dir="data/source", max_mb=20)

From the command line:
    python scripts/prepare_sources.py --config config/sources.json
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import datetime as dt
import glob
import hashlib
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chunking import ChunkWriter, clear_parts  # noqa: E402

NULL_STRINGS = {"", "none", "nan", "null", "undefined", "n/a"}


# ──────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────

def natural_key(s: str):
    """Sort data_2 before data_10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def expand_inputs(patterns) -> list[str]:
    files: list[str] = []
    for pat in patterns:
        hits = sorted(glob.glob(pat), key=natural_key)
        if not hits:
            print(f"  ! no files match {pat}")
        files.extend(h for h in hits if h not in files)
    return files


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def detect_schema(rec: dict) -> str:
    if not isinstance(rec, dict):
        return "not_a_record"
    if "gem_controlled_objects" in rec or "gem_text_elements" in rec:
        return "pipeline_xlsx"
    if "te" in rec or "date_info" in rec or "places_dep" in rec:
        return "v6_compact_json"
    if "Transcribed_Text" in rec or any(k.startswith("output - ") for k in rec):
        return "v1_flat_json"
    return "unknown"


def record_uid(rec: dict) -> str:
    for k in ("uid", "object_uid", "collection_uid"):
        v = rec.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


# ──────────────────────────────────────────────────────────────────────────
# readers
# ──────────────────────────────────────────────────────────────────────────

def read_json_file(path: str, info: dict):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        list_key = next((k for k, v in data.items() if isinstance(v, list)), None)
        if list_key is None:
            raise ValueError(f"{path}: JSON object without a list of records")
        info["wrapper"] = {k: v for k, v in data.items() if k != list_key}
        info["wrapper_list_key"] = list_key
        data = data[list_key]
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of records")
    for i, rec in enumerate(data):
        yield i, rec


def clean_cell(value, col: str, stats: dict):
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, str):
        s = value.strip()
        if s.lower() in NULL_STRINGS:
            return None
        if s[:1] in "[{":
            try:
                parsed = json.loads(s)
                stats["json_columns"].add(col)
                return parsed
            except (ValueError, RecursionError):
                stats["unparsed_json"][col] += 1
        return s
    return value


def read_xlsx_file(path: str, info: dict, sheet: str | None = None):
    import openpyxl  # preinstalled on Colab

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    info["sheet"] = ws.title
    rows = ws.iter_rows(values_only=True)
    try:
        header = next(rows)
    except StopIteration:
        wb.close()
        return

    seen: Counter = Counter()
    columns, renamed = [], []
    for i, h in enumerate(header):
        name = str(h).strip() if h not in (None, "") else f"column_{i + 1}"
        seen[name] += 1
        if seen[name] > 1:
            new = f"{name}__{seen[name]}"
            renamed.append({"original": name, "renamed_to": new, "position": i + 1})
            name = new
        columns.append(name)
    info["columns"] = columns
    info["renamed_columns"] = renamed
    stats = {"json_columns": set(), "unparsed_json": Counter()}

    for r_i, row in enumerate(rows, start=2):  # spreadsheet row number
        if row is None or all(v in (None, "") for v in row):
            continue
        rec = OrderedDict()
        for col, val in zip(columns, row):
            rec[col] = clean_cell(val, col, stats)
        yield r_i, rec

    info["json_columns"] = sorted(stats["json_columns"], key=columns.index)
    info["unparsed_json"] = dict(stats["unparsed_json"])
    wb.close()


# ──────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────

def prepare(sources: list[dict], out_dir: str | Path = "data/source",
            max_mb: float = 20, compute_hashes: bool = True) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reports = out_dir / "reports"
    reports.mkdir(exist_ok=True)
    max_bytes = int(max_mb * 1_000_000)

    manifest = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "max_bytes": max_bytes,
        "sources": {},
    }
    uid_sources: dict[str, set] = {}

    for src in sources:
        name, fmt = src["name"], src["format"].lower()
        if not re.fullmatch(r"[a-z0-9_\-]+", name):
            raise ValueError(f"source name '{name}' must be lowercase letters, digits, _ or -")
        files = expand_inputs(src["inputs"])
        print(f"\n▶ {name} ({fmt}) — {len(files)} input file(s)")
        if not files:
            continue

        # pass 1: load everything (keeps memory simple; ~100s of MB is fine on Colab)
        loaded: list[tuple[str, dict]] = []          # (uid, record)
        last_index: dict[str, int] = {}
        dup_rows = []
        no_uid = 0
        drop_patterns = src.get("drop_fields") or []
        dropped_fields: Counter = Counter()
        schema_counts: Counter = Counter()
        file_infos = []
        for path in files:
            info = {"file": Path(path).name, "bytes": Path(path).stat().st_size}
            if compute_hashes:
                info["sha256"] = sha256(path)
            reader = (read_xlsx_file(path, info, src.get("sheet")) if fmt == "xlsx"
                      else read_json_file(path, info))
            n = 0
            for row_no, rec in reader:
                if not isinstance(rec, dict):
                    continue
                if drop_patterns:
                    for k in [k for k in rec if any(fnmatch.fnmatchcase(k, pat) for pat in drop_patterns)]:
                        dropped_fields[k] += 1
                        del rec[k]
                rec["_src"] = {"file": info["file"], "row": row_no}
                uid = record_uid(rec)
                schema_counts[detect_schema(rec)] += 1
                if not uid:
                    no_uid += 1
                    uid = f"__no_uid__{info['file']}__{row_no}"
                if uid in last_index:
                    prev = loaded[last_index[uid]][1]["_src"]
                    dup_rows.append([uid, prev["file"], prev["row"], info["file"], row_no])
                last_index[uid] = len(loaded)
                loaded.append((uid, rec))
                n += 1
            info["records"] = n
            file_infos.append(info)
            print(f"  · {info['file']}: {n:,} records")

        # pass 2: write chunks (later duplicates replace earlier ones)
        folder = out_dir / name
        clear_parts(folder)
        writer = ChunkWriter(folder, max_bytes)
        written = 0
        for i, (uid, rec) in enumerate(loaded):
            if last_index[uid] != i:
                continue
            writer.add(rec, uid)
            written += 1
            uid_sources.setdefault(uid, set()).add(name)
        parts = writer.close()

        if dup_rows:
            with open(reports / f"{name}_duplicates.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["uid", "dropped_file", "dropped_row", "kept_file", "kept_row"])
                w.writerows(dup_rows)

        entry = {
            "format": fmt,
            "role": src.get("role", "records"),
            "inputs": file_infos,
            "records": written,
            "duplicates_removed": len(dup_rows),
            "records_without_uid": no_uid,
            "dropped_fields": dict(dropped_fields),
            "schema_counts": dict(schema_counts),
            "parts": parts,
        }
        manifest["sources"][name] = entry
        biggest = max(p["bytes"] for p in parts) / 1e6
        print(f"  ✓ {written:,} records → {len(parts)} chunk(s), largest {biggest:.1f} MB"
              f"{f'; {len(dup_rows):,} duplicate uids replaced (see reports/)' if dup_rows else ''}")
        if dropped_fields:
            print(f"  · left out fields: {dict(dropped_fields)}")
        if writer.oversized:
            print(f"  ! {len(writer.oversized)} single record(s) exceed the size cap: {writer.oversized[:5]}")
        for info in file_infos:
            if info.get("renamed_columns"):
                print(f"  · {info['file']}: renamed repeated columns → "
                      + ", ".join(r["renamed_to"] for r in info["renamed_columns"]))
            if info.get("unparsed_json"):
                print(f"  · {info['file']}: cells that look like JSON but are truncated/invalid "
                      f"(kept as text): {info['unparsed_json']}")

    # cross-source overlap summary
    combos = Counter(" + ".join(sorted(s)) for s in uid_sources.values())
    manifest["uid_overlap"] = dict(combos.most_common())
    manifest["unique_uids"] = len(uid_sources)

    # Keep the old timestamp if nothing else changed, so an unchanged re-run
    # produces no git changes at all.
    mpath = out_dir / "manifest.json"
    if mpath.exists():
        try:
            old = json.loads(mpath.read_text(encoding="utf-8"))
            if {**old, "generated_at": None} == {**manifest, "generated_at": None}:
                manifest["generated_at"] = old["generated_at"]
        except (ValueError, KeyError):
            pass
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print(f"\n{len(uid_sources):,} unique photographs across all sources. Overlap:")
    for k, v in combos.most_common():
        print(f"  {v:>7,}  {k}")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="JSON file: {\"sources\": [...], \"max_mb\": 20}")
    ap.add_argument("--out", default="data/source")
    ap.add_argument("--no-hash", action="store_true")
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    prepare(cfg["sources"], a.out, cfg.get("max_mb", 20), compute_hashes=not a.no_hash)


if __name__ == "__main__":
    main()
