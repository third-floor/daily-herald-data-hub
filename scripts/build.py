"""
STEP 2 — Build the website data (data/derived) from the source chunks.

Runs automatically in the GitHub Action on every push (and can be run locally
or in Colab to test). Its output is deployed to GitHub Pages but NEVER
committed, so regenerating it doesn't bloat the repository history.

Input : data/source/manifest.json + data/source/<source>/part-*.json
Output: <out>/data/manifest.json
        <out>/data/records/part-*.json   full canonical record per photograph
        <out>/data/search/part-*.json    slim, pre-normalised docs for the search page
        <out>/data/visual/part-*.json    visual-element boxes for the spatial tools

What it does to the data
------------------------
* Joins every source on `uid` → one record per photograph, with optional
  sections: transcription_v1, metadata_v6, visual_elements — plus one
  top-level `transcription` per photograph, taken from the newest source that
  has one (v6 text elements, otherwise the v1 Transcribed_Text).
* Leaves out the old Gemini 2.5 Flash outputs ("output - gemini-2.5…" columns
  of the v1 files); only the newer transcriptions are published.
* Expands the abbreviated keys of the compact v6 JSON export
  (te/leg/conf/rat/...) to the full names used by the pipeline spreadsheet
  (text_elements/legibility/confidence_score/rationale/...), so both look the
  same downstream.
* When a photograph has v6 metadata in BOTH the compact JSON and the pipeline
  spreadsheet, the spreadsheet version wins (it is the richer, unabridged one).
* Repairs mojibake ("Â»", "ÃƒÆ’Ã¢â‚¬Å¡..." → "»") with ftfy.
* Splits the spreadsheet's two pipeline runs (metadata run / visual-elements
  run) using the column order, so repeated columns such as gem_model and
  gem_model__2 end up under the right section.

Usage
-----
    pip install -r requirements.txt
    python scripts/build.py --source data/source --out _site
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chunking import ChunkWriter, clear_parts, read_parts  # noqa: E402

try:
    import ftfy
except ImportError:  # still works without it, just no mojibake repair
    ftfy = None
    print("! ftfy not installed — mojibake will not be repaired (pip install ftfy)")

CATALOGUE_URL = "https://collection.sciencemuseumgroup.org.uk/objects/{uid}"

# First column of the spreadsheet's SECOND pipeline run (visual elements).
# Everything from this column onwards belongs to the visual-elements section.
VISUAL_RUN_MARKERS = ("gem_status", "gem_validation_level", "gem_controlled_objects")

# ──────────────────────────────────────────────────────────────────────────
# small helpers
# ──────────────────────────────────────────────────────────────────────────

_MOJIBAKE = re.compile(r"[ÃÂâ€�]")
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def fix_text(value):
    """Recursively repair mojibake in every string of a JSON-like value."""
    if isinstance(value, str):
        if ftfy and _MOJIBAKE.search(value):
            return ftfy.fix_text(value, normalization="NFC")
        return value
    if isinstance(value, list):
        return [fix_text(v) for v in value]
    if isinstance(value, dict):
        return {k: fix_text(v) for k, v in value.items()}
    return value


def empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def first(*values):
    for v in values:
        if not empty(v):
            return v
    return None


def to_number(v):
    if isinstance(v, str) and _NUMERIC.match(v.strip()):
        return float(v) if "." in v else int(v)
    return v


def as_list(v) -> list:
    if v is None or v == "":
        return []
    return [x for x in v if not empty(x)] if isinstance(v, list) else [v]


def compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if not empty(v)}


def split_links(v) -> list[str]:
    if isinstance(v, list):
        return [str(s).strip() for s in v if str(s).strip()]
    if isinstance(v, str):
        return [s.strip() for s in v.split(";") if s.strip()]
    return []


def clean_date(v):
    if isinstance(v, str):
        return re.sub(r"[T ]00:00:00(\.0+)?$", "", v.strip()) or None
    return v


def detect_schema(rec: dict) -> str:
    if "gem_controlled_objects" in rec or "gem_text_elements" in rec:
        return "pipeline_xlsx"
    if "te" in rec or "date_info" in rec or "places_dep" in rec:
        return "v6_compact_json"
    if "Transcribed_Text" in rec or any(k.startswith("output - ") for k in rec):
        return "v1_flat_json"
    return "unknown"


# ──────────────────────────────────────────────────────────────────────────
# compact v6 JSON  →  full key names
# ──────────────────────────────────────────────────────────────────────────

COMMON = {"conf": "confidence_score", "rat": "rationale", "from": "derived_from",
          "cert": "identification_certainty", "rel": "relationship_label"}
PERSON = {**COMMON, "name": "name_standardised"}
PLACE = {**COMMON, "name": "name_standardised", "type": "place_type"}
RIGHT = {**COMMON, "holder": "copyright_holder", "type": "rights_type"}
TEXT_EL = {"type": "element_type", "text": "transcription", "leg": "legibility"}


def remap_list(items, keymap):
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append({keymap.get(k, k): v for k, v in it.items()})
        elif it:
            out.append(it)
    return out


V6_KNOWN = {"id", "uid", "title", "img", "imgs", "url", "date", "date_info", "activity", "te",
            "photographers", "depicted", "mentioned", "places_dep", "places_men", "subjects",
            "rights", "editorial", "meta", "_src"}
V1_KNOWN = {"uid", "identifier", "title", "description", "date", "maker", "thumbnail_medium_link",
            "thumbnail_large_link", "image_links", "Transcribed_Text", "text_length",
            "Spatial_Prompt", "Spatial_Regions", "_src"}


def other_fields(r: dict, known: set) -> dict:
    """Fields a newer export added that this script doesn't know yet — kept, not lost."""
    return {k: v for k, v in r.items()
            if k not in known and not k.startswith("output - ") and not empty(v)}


def metadata_from_compact(r: dict) -> dict:
    activity = r.get("activity")
    return compact({
        "other_fields": other_fields(r, V6_KNOWN),
        "from_source": "v6_compact_json",
        "text_elements": remap_list(r.get("te"), TEXT_EL),
        "photographers": remap_list(r.get("photographers"), PERSON),
        "people_depicted": remap_list(r.get("depicted"), PERSON),
        "people_mentioned": remap_list(r.get("mentioned"), PERSON),
        "places_depicted": remap_list(r.get("places_dep"), PLACE),
        "places_mentioned": remap_list(r.get("places_men"), PLACE),
        "date_primary": r.get("date_info") or None,
        "date_display": r.get("date"),
        "depicted_activity": {"description": activity} if activity else None,
        "subjects": r.get("subjects"),
        "rights": remap_list(r.get("rights"), RIGHT),
        "editorial": r.get("editorial"),
        "processing": compact(r.get("meta") or {}),
    })


# ──────────────────────────────────────────────────────────────────────────
# pipeline spreadsheet rows  →  sections
# ──────────────────────────────────────────────────────────────────────────

XLSX_CATALOGUE = {"uid", "identifier", "title", "description", "date", "maker",
                  "image_links", "url", "thumbnail_medium_link", "thumbnail_large_link"}
XLSX_METADATA_FIELDS = {
    "gem_text_elements": "text_elements",
    "gem_photographers": "photographers",
    "gem_people_depicted": "people_depicted",
    "gem_people_mentioned": "people_mentioned",
    "gem_places_depicted": "places_depicted",
    "gem_places_mentioned": "places_mentioned",
    "gem_date_primary": "date_primary",
    "gem_dates_associated": "dates_associated",
    "gem_depicted_activity": "depicted_activity",
    "gem_subjects": "subjects",
    "gem_rights": "rights",
    "gem_editorial": "editorial",
}
XLSX_METADATA_FLAT = {"gem_transcription_flat", "gem_maker_flat", "gem_date_flat",
                      "gem_places_flat", "gem_persons_flat"}
XLSX_VISUAL_FIELDS = {
    "gem_controlled_objects": "controlled_objects",
    "gem_other_objects": "other_objects",
    "gem_dropped_objects": "dropped_objects",
    "gem_status": "status",
    "gem_validation_level": "validation_level",
    "gem_validation_notes": "validation_notes",
}
XLSX_VISUAL_IMAGE = {"gem_image_width", "gem_image_height", "gem_sent_width", "gem_sent_height"}
# Dropped from derived output (still kept in data/source):
XLSX_SKIP = {"gem_raw_response",            # truncated copy of the parsed fields
             "gem_controlled_objects_flat", "gem_other_objects_flat", "gem_all_objects_flat"}


def base_name(col: str) -> str:
    return re.sub(r"__\d+$", "", col)


def sections_from_xlsx(r: dict, columns: list[str]):
    """Return (catalogue, metadata_section, visual_section) for one spreadsheet row."""
    cols = columns or list(r.keys())
    boundary = min((cols.index(m) for m in VISUAL_RUN_MARKERS if m in cols), default=len(cols))
    cat, meta, meta_proc, vis, vis_proc, vis_img, flat = {}, {}, {}, {}, {}, {}, {}
    for i, col in enumerate(cols):
        if col not in r or col == "_src":
            continue
        v, b = r[col], base_name(col)
        if b in XLSX_SKIP or empty(v):
            continue
        if col in XLSX_CATALOGUE:
            cat[col] = v
        elif i < boundary:                                   # metadata run
            if b in XLSX_METADATA_FIELDS:
                meta[XLSX_METADATA_FIELDS[b]] = v
            elif b in XLSX_METADATA_FLAT:
                flat.setdefault(b.replace("gem_", ""), v)
            else:
                meta_proc[b.replace("gem_", "", 1)] = to_number(v)
        else:                                                # visual-elements run
            if b in XLSX_VISUAL_FIELDS:
                vis[XLSX_VISUAL_FIELDS[b]] = v
            elif b in XLSX_VISUAL_IMAGE:
                vis_img[b.replace("gem_", "", 1)] = to_number(v)
            else:
                vis_proc[b.replace("gem_", "", 1)] = to_number(v)

    metadata = None
    if meta:
        metadata = compact({"from_source": "pipeline_xlsx", **meta,
                            "flat": flat, "processing": meta_proc})
    visual = None
    if any(k in vis for k in ("controlled_objects", "other_objects")):
        visual = compact({**vis, "image": vis_img, "processing": vis_proc})
    return cat, metadata, visual


# ──────────────────────────────────────────────────────────────────────────
# merge all sources into one record per uid
# ──────────────────────────────────────────────────────────────────────────

def load_sources(src_dir: Path):
    manifest = json.loads((src_dir / "manifest.json").read_text(encoding="utf-8"))
    records: dict[str, dict] = {}
    order: list[str] = []
    stats = Counter()

    def rec_for(uid):
        if uid not in records:
            records[uid] = {"uid": uid, "_cat": {}, "provenance": []}
            order.append(uid)
        return records[uid]

    for name, info in manifest["sources"].items():
        if info.get("role") == "lookup":
            continue
        columns_by_file = {f["file"]: f.get("columns") for f in info["inputs"]}
        for r in read_parts(src_dir / name):
            uid = str(r.get("uid") or "").strip()
            if not uid:
                stats["skipped_no_uid"] += 1
                continue
            kind = detect_schema(r)
            out = rec_for(uid)
            out["provenance"].append(compact({"source": name, "schema": kind, **(r.get("_src") or {})}))
            cat = out["_cat"]

            if kind == "v1_flat_json":
                # The "output - gemini-2.5-…" columns are deliberately not used.
                out["transcription_v1"] = compact({
                    "transcribed_text": r.get("Transcribed_Text"),
                    "spatial_prompt": r.get("Spatial_Prompt"),
                    "spatial_regions": r.get("Spatial_Regions"),
                    "other_fields": other_fields(r, V1_KNOWN),
                })
                for k in ("identifier", "title", "description", "date", "maker",
                          "image_links", "thumbnail_medium_link", "thumbnail_large_link"):
                    cat.setdefault(k, r.get(k))
            elif kind == "v6_compact_json":
                if out.get("metadata_v6", {}).get("from_source") != "pipeline_xlsx":
                    out["metadata_v6"] = metadata_from_compact(r)
                cat.setdefault("identifier", r.get("id"))
                cat.setdefault("title", r.get("title"))
                cat.setdefault("url", r.get("url"))
                cat.setdefault("image_links", r.get("imgs") or ([r["img"]] if r.get("img") else None))
            elif kind == "pipeline_xlsx":
                c, meta, vis = sections_from_xlsx(r, columns_by_file.get((r.get("_src") or {}).get("file")))
                for k, v in c.items():
                    if empty(cat.get(k)):
                        cat[k] = v
                if meta:
                    out["metadata_v6"] = meta          # richer than the compact export
                if vis:
                    out["visual_elements"] = vis
            else:
                stats[f"unknown_schema:{name}"] += 1
            stats[kind] += 1

    return manifest, [records[u] for u in order], stats


def finalise(rec: dict) -> dict:
    cat = rec.pop("_cat")
    uid = rec["uid"]
    meta = rec.get("metadata_v6") or {}
    images = split_links(cat.get("image_links"))
    out = compact({
        "uid": uid,
        "identifier": first(cat.get("identifier"), (meta.get("editorial") or {}).get("accession_number")),
        "title": cat.get("title"),
        "description": cat.get("description"),
        "catalogue_date": clean_date(cat.get("date")),
        "maker": cat.get("maker"),
        "catalogue_url": first(cat.get("url"), CATALOGUE_URL.format(uid=uid)),
        "images": images,
        "thumbnail": first(cat.get("thumbnail_medium_link"), images[0] if images else None),
        "thumbnail_large": cat.get("thumbnail_large_link"),
    })
    # One transcription per photograph, from the newest source that has one.
    v6_text = "\n".join(e.get("transcription", "") for e in meta.get("text_elements") or []
                        if isinstance(e, dict) and e.get("transcription"))
    v6_text = v6_text or (meta.get("flat") or {}).get("transcription_flat")
    v1_text = (rec.get("transcription_v1") or {}).get("transcribed_text")
    if v6_text:
        out["transcription"] = {"text": v6_text, "from": "metadata_v6"}
    elif v1_text:
        out["transcription"] = {"text": v1_text, "from": "transcription_v1"}
    out["sections"] = [s for s in ("transcription_v1", "metadata_v6", "visual_elements") if s in rec]
    for s in out["sections"]:
        out[s] = rec[s]
    out["provenance"] = rec["provenance"]
    return fix_text(out)


# ──────────────────────────────────────────────────────────────────────────
# derived views
# ──────────────────────────────────────────────────────────────────────────

def _people(items, place=False):
    vals = []
    for p in items or []:
        if isinstance(p, str):
            vals.append(p)
            continue
        name = p.get("name_standardised") or p.get("name_as_transcribed") or p.get("name") or ""
        rel = p.get("relationship_label") or (p.get("place_type") if place else "")
        if name:
            vals.append(f"{name} — {rel}" if rel else name)
    return vals


def _rights(items):
    return [" — ".join(x for x in (r.get("copyright_holder"), r.get("rights_type")) if x)
            for r in items or [] if isinstance(r, dict)]


EDITORIAL_LABELS = {"publication_name": "Publication", "story_or_series_title": "Story / series",
                    "caption_reference_codes": "Reference codes",
                    "image_sequence_number": "Image sequence", "accession_number": "Accession number"}


def _objects(vis):
    out = []
    for kind in ("controlled_objects", "other_objects"):
        for o in vis.get(kind) or []:
            if isinstance(o, dict) and o.get("term"):
                t = str(o["term"]).replace("_", " ")
                out.append(f"{t} ({o['detail']})" if o.get("detail") else t)
    return list(dict.fromkeys(out))


def _collect_text(value, deep, rat, path=""):
    if empty(value):
        return
    if isinstance(value, (str, int, float, bool)):
        (rat if "rationale" in path.lower() else deep).append(str(value))
    elif isinstance(value, list):
        for v in value:
            _collect_text(v, deep, rat, path)
    elif isinstance(value, dict):
        for k, v in value.items():
            _collect_text(v, deep, rat, f"{path}.{k}")


def ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def search_doc(rec: dict, records_part: int) -> tuple[dict, dict]:
    """Same fields the existing search worker produced, but precomputed.

    Returns (core, extra): `core` is what every search needs (card fields);
    `extra` holds the hidden-metadata and rationale text, which the search
    page only downloads when someone ticks those options.
    """
    t1 = rec.get("transcription_v1") or {}
    md = rec.get("metadata_v6") or {}
    vis = rec.get("visual_elements") or {}

    transcription = (rec.get("transcription") or {}).get("text", "")

    makers, seen = [], set()
    def add_maker(name, source):
        if name and (name, source) not in seen:
            seen.add((name, source)); makers.append({"name": name, "source": source})
    add_maker(rec.get("maker"), "maker field")
    for p in md.get("photographers") or []:
        if isinstance(p, dict):
            add_maker(p.get("name_standardised") or p.get("name_as_transcribed"), "photographers field")
    for r in md.get("rights") or []:
        if isinstance(r, dict):
            add_maker(r.get("copyright_holder"), "rights field (copyright holder)")

    dp = md.get("date_primary") or {}
    date = first(rec.get("catalogue_date"), md.get("date_display"),
                 " / ".join(str(d) for d in as_list(dp.get("date_standardised"))), dp.get("date_as_transcribed")) or ""
    date_details = [x for x in (
        f"Transcribed: {dp['date_as_transcribed']}" if dp.get("date_as_transcribed") else "",
        f"Relationship: {dp['relationship_label']}" if dp.get("relationship_label") else "",
    ) if x]

    structured = []
    def group(label, values):
        values = [v for v in dict.fromkeys(values) if v]
        if values:
            structured.append({"label": label, "values": values})
    group("Photographers", _people(md.get("photographers")))
    group("People depicted", _people(md.get("people_depicted")))
    group("People mentioned", _people(md.get("people_mentioned")))
    group("Places depicted", _people(md.get("places_depicted"), place=True))
    group("Places mentioned", _people(md.get("places_mentioned"), place=True))
    group("Rights", _rights(md.get("rights")))
    ed = md.get("editorial") or {}
    group("Editorial", [f"{lab}: {ed[k]}" for k, lab in EDITORIAL_LABELS.items() if ed.get(k)])
    group("Visual elements", _objects(vis))

    deep, rat = [], []
    shown = {"uid", "identifier", "title", "description", "catalogue_date", "maker",
             "images", "thumbnail", "thumbnail_large", "catalogue_url", "sections", "provenance",
             "transcription"}
    for k, v in rec.items():
        if k not in shown:
            _collect_text(v, deep, rat, k)

    schema = ("v6 + visual" if vis else "v6") if md else ("v1" if t1 else "catalogue only")
    return compact({
        "uid": rec["uid"],
        "identifier": rec.get("identifier"),
        "title": rec.get("title") or f"Daily Herald object {rec['uid']}",
        "description": first(rec.get("description"), (md.get("depicted_activity") or {}).get("description"),
                             ed.get("story_or_series_title")),
        "makers": makers,
        "date": date,
        "dateDetails": date_details,
        "transcription": transcription,
        "images": rec.get("images"),
        "url": rec.get("catalogue_url"),
        "structured": structured,
        "schema": schema,
        "rp": records_part,
    }), compact({"uid": rec["uid"], "deep": ws(" ".join(deep)), "rat": ws(" ".join(rat))})


def _years(rec):
    md = rec.get("metadata_v6") or {}
    ys = set()
    for s in [rec.get("catalogue_date"), md.get("date_display"),
              *as_list((md.get("date_primary") or {}).get("date_standardised"))]:
        for m in re.findall(r"\b(1[89]\d\d|20\d\d)\b", str(s or "")):
            ys.add(int(m))
    return sorted(ys)


def visual_doc(rec: dict) -> dict | None:
    vis = rec.get("visual_elements")
    if not vis:
        return None
    used = (vis.get("processing") or {}).get("image_used") or ""
    image = next((u for u in rec.get("images", []) if used and u.endswith(used)),
                 (rec.get("images") or [""])[0])
    dets = []
    for kind, short in (("controlled_objects", "ctrl"), ("other_objects", "other")):
        for o in vis.get(kind) or []:
            box = o.get("box_2d") if isinstance(o, dict) else None
            if not o.get("term") or not (isinstance(box, list) and len(box) == 4):
                continue
            dets.append(compact({"term": str(o["term"]).strip().lower(), "kind": short,
                                 "detail": o.get("detail"), "confidence": o.get("confidence_score"),
                                 "rationale": o.get("rationale"), "box_2d": box}))
    md = rec.get("metadata_v6") or {}
    makers = [p.get("name_standardised") or p.get("name_as_transcribed")
              for p in md.get("photographers") or [] if isinstance(p, dict)]
    return compact({
        "uid": rec["uid"], "identifier": rec.get("identifier"), "title": rec.get("title"),
        "url": rec.get("catalogue_url"), "image": image,
        "image_size": vis.get("image"),
        "makers": [m for m in dict.fromkeys(makers + [rec.get("maker")]) if m],
        "years": _years(rec),
        "validation_level": vis.get("validation_level"),
        "detections": dets,
    })


# ──────────────────────────────────────────────────────────────────────────
# widget views — each widget gets its data in the shape it was written for,
# so the widgets need no changes to their analysis code.
# ──────────────────────────────────────────────────────────────────────────

INV_PERSON = {v: k for k, v in PERSON.items()}
INV_PLACE = {v: k for k, v in PLACE.items()}
INV_RIGHT = {v: k for k, v in RIGHT.items()}
INV_TEXT_EL = {v: k for k, v in TEXT_EL.items()}


def _to_compact_list(items, inv):
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        o = {inv.get(k, k): v for k, v in it.items()}
        if "name" in inv.values() and not o.get("name") and it.get("name_as_transcribed"):
            o["name"] = it["name_as_transcribed"]
        out.append(o)
    return out


def _src_file(rec, section_sources):
    for p in rec.get("provenance") or []:
        if p.get("schema") in section_sources and p.get("file"):
            return p["file"]
    return None


def _date_flat(md):
    dp = md.get("date_primary") or {}
    return first(" / ".join(str(d) for d in as_list(dp.get("date_standardised"))), md.get("date_display"))


def _names(items):
    return " ; ".join(dict.fromkeys(
        (p.get("name_standardised") or p.get("name_as_transcribed") or p.get("name"))
        for p in items or [] if isinstance(p, dict)
        and (p.get("name_standardised") or p.get("name_as_transcribed") or p.get("name"))))


def view_compact_v6(rec):
    """The compact v6 JSON shape (as in data_5–10.json) — data_hub, linguistic, explorer."""
    md = rec.get("metadata_v6")
    if not md:
        return None
    return {
        "id": rec.get("identifier"), "uid": rec["uid"], "title": rec.get("title"),
        "imgs": rec.get("images") or [], "url": rec.get("catalogue_url"),
        "date": _date_flat(md) or "",
        "date_info": md.get("date_primary") or {},
        "activity": (md.get("depicted_activity") or {}).get("description", ""),
        "te": _to_compact_list(md.get("text_elements"), INV_TEXT_EL),
        "photographers": _to_compact_list(md.get("photographers"), INV_PERSON),
        "depicted": _to_compact_list(md.get("people_depicted"), INV_PERSON),
        "mentioned": _to_compact_list(md.get("people_mentioned"), INV_PERSON),
        "places_dep": _to_compact_list(md.get("places_depicted"), INV_PLACE),
        "places_men": _to_compact_list(md.get("places_mentioned"), INV_PLACE),
        "subjects": md.get("subjects") or [],
        "rights": _to_compact_list(md.get("rights"), INV_RIGHT),
        "editorial": md.get("editorial") or {},
        "meta": md.get("processing") or {},
        "_src": _src_file(rec, {md.get("from_source")}),
    }


def view_v1(rec):
    """The original flat transcription JSON shape (data_1–4.json) — data_hub."""
    t1 = rec.get("transcription_v1")
    if not t1:
        return None
    out = {
        "uid": rec["uid"], "identifier": rec.get("identifier", ""), "title": rec.get("title", ""),
        "description": rec.get("description", ""), "date": rec.get("catalogue_date", ""),
        "maker": rec.get("maker", ""),
        "thumbnail_medium_link": rec.get("thumbnail", ""),
        "thumbnail_large_link": rec.get("thumbnail_large", ""),
        "image_links": "; ".join(rec.get("images") or []),
        "Transcribed_Text": t1.get("transcribed_text", ""),
        "text_length": t1.get("text_length", ""),
        "Spatial_Prompt": t1.get("spatial_prompt", ""),
        "Spatial_Regions": t1.get("spatial_regions", ""),
        "_src": _src_file(rec, {"v1_flat_json"}),
    }
    return out


def view_dates(rec):
    """Slim spreadsheet-style row for every dated photo — calendar + weather."""
    md = rec.get("metadata_v6") or {}
    date_flat = _date_flat(md)
    if not (date_flat or rec.get("catalogue_date")):
        return None
    return compact({
        "uid": rec["uid"], "identifier": rec.get("identifier"), "title": rec.get("title"),
        "description": rec.get("description"), "date": rec.get("catalogue_date"),
        "maker": rec.get("maker"), "url": rec.get("catalogue_url"),
        "image_links": "; ".join(rec.get("images") or []),
        "gem_date_flat": date_flat,
        "date_relationship": (md.get("date_primary") or {}).get("relationship_label"),
        "activity_description": (md.get("depicted_activity") or {}).get("description"),
        "gem_places_depicted": _names(md.get("places_depicted")),
        "gem_places_mentioned": _names(md.get("places_mentioned")),
        "gem_maker_flat": _names(md.get("photographers")),
    })


def view_visual_table(rec):
    """Pipeline-spreadsheet row (visual-elements run) — workbench + spatial explorer.

    Repeated spreadsheet columns (gem_model, gem_image_used, token counts…) are
    filled from the VISUAL-ELEMENTS run, which is what these tools analyse.
    """
    vis = rec.get("visual_elements")
    if not vis:
        return None
    md = rec.get("metadata_v6") or {}
    t1 = rec.get("transcription_v1") or {}
    row = {
        "uid": rec["uid"], "identifier": rec.get("identifier"), "title": rec.get("title"),
        "description": rec.get("description"), "date": rec.get("catalogue_date"),
        "maker": rec.get("maker"), "url": rec.get("catalogue_url"),
        "image_links": "; ".join(rec.get("images") or []),
        "gem_maker_flat": (md.get("flat") or {}).get("maker_flat") or _names(md.get("photographers")),
        "gem_date_flat": (md.get("flat") or {}).get("date_flat") or _date_flat(md),
        "gem_transcription_flat": (md.get("flat") or {}).get("transcription_flat")
            or (rec.get("transcription") or {}).get("text"),
        "gem_controlled_objects": vis.get("controlled_objects") or [],
        "gem_other_objects": vis.get("other_objects") or [],
        "gem_dropped_objects": vis.get("dropped_objects") or [],
        "gem_status": vis.get("status"),
        "gem_validation_level": vis.get("validation_level"),
        "gem_validation_notes": vis.get("validation_notes"),
    }
    for k, v in (vis.get("image") or {}).items():
        row[f"gem_{k}"] = v
    for k, v in (vis.get("processing") or {}).items():
        row[f"gem_{k}"] = v
    return {k: v for k, v in row.items() if v is not None and v != ""}


WIDGET_VIEWS = {
    "compact_v6": view_compact_v6,
    "v1": view_v1,
    "dates": view_dates,
    "visual_table": view_visual_table,
}


# ──────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────

def build(src_dir="data/source", out="_site", site_dir="site", max_mb: float = 20,
          search_mb: float = 5):
    src_dir, out = Path(src_dir), Path(out)
    max_bytes = int(max_mb * 1_000_000)

    # 1. copy the static site (hub page, tools, search) into the output folder
    if site_dir and Path(site_dir).exists():
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(site_dir, out)
    data_out = out / "data"
    data_out.mkdir(parents=True, exist_ok=True)

    # 2. merge
    src_manifest, merged, stats = load_sources(src_dir)
    print(f"Merged {len(merged):,} photographs from {len(src_manifest['sources'])} source(s)")

    # 3. write chunked views
    # Search files are kept smaller (default 5 MB) so the search page can start
    # answering as soon as the first one arrives, while the rest stream in.
    sizes = {"records": max_bytes, "visual": max_bytes,
             "search": min(max_bytes, int(search_mb * 1_000_000)),
             "search_extra": min(max_bytes, int(search_mb * 1_000_000))}
    sizes.update({f"widgets/{k}": max_bytes for k in WIDGET_VIEWS})
    if (data_out / "widgets").exists():
        shutil.rmtree(data_out / "widgets")
    writers = {k: (clear_parts(data_out / k), ChunkWriter(data_out / k, size))[1]
               for k, size in sizes.items()}
    section_counts, term_counts = Counter(), Counter()
    years = Counter()
    for raw in merged:
        rec = finalise(raw)
        part = writers["records"].add(rec, rec["uid"])
        core, extra = search_doc(rec, part)
        writers["search"].add(core, rec["uid"])
        writers["search_extra"].add(extra, rec["uid"])
        v = visual_doc(rec)
        if v:
            writers["visual"].add(v, rec["uid"])
            term_counts.update({d["term"] for d in v["detections"]})
        for name, fn in WIDGET_VIEWS.items():
            row = fn(rec)
            if row:
                writers[f"widgets/{name}"].add(row, rec["uid"])
        section_counts.update(rec["sections"])
        if rec.get("transcription"):
            section_counts["transcription"] += 1
        for y in _years(rec)[:1]:
            years[y // 10 * 10] += 1

    files = {k: w.close() for k, w in writers.items()}

    # Lookup tables (e.g. a place gazetteer) are passed through unchanged.
    if (data_out / "lookups").exists():
        shutil.rmtree(data_out / "lookups")
    for name, info in src_manifest["sources"].items():
        if info.get("role") != "lookup":
            continue
        w = ChunkWriter(data_out / "lookups" / name, max_bytes)
        for r in read_parts(src_dir / name):
            r.pop("_src", None)
            w.add(fix_text(r))
        files[f"lookups/{name}"] = w.close()
    manifest = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "photographs": len(merged),
        "sections": dict(section_counts),
        "decades": {str(k): v for k, v in sorted(years.items())},
        "top_visual_terms": dict(term_counts.most_common(40)),
        "files": {k: [{**p, "path": f"data/{k}/{p['file']}"} for p in parts] for k, parts in files.items()},
        "sources": {name: {"records": s["records"], "format": s["format"],
                           "inputs": [i["file"] for i in s["inputs"]],
                           "parts": [f"data/source/{name}/{p['file']}" for p in s["parts"]]}
                    for name, s in src_manifest["sources"].items()},
        "build_stats": dict(stats),
    }
    (data_out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    for k, parts in files.items():
        mb = sum(p["bytes"] for p in parts) / 1e6
        print(f"  {k:12s} {len(parts):3d} file(s), {mb:7.1f} MB total, "
              f"largest {max((p['bytes'] for p in parts), default=0) / 1e6:.1f} MB")
    print(f"  sections: {dict(section_counts)}")
    if any(k.startswith("unknown") for k in stats):
        print(f"  ! records with unknown schema: {stats}")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="data/source")
    ap.add_argument("--out", default="_site")
    ap.add_argument("--site", default="site", help="static site folder copied into --out first")
    ap.add_argument("--max-mb", type=float, default=20, help="hard cap per file")
    ap.add_argument("--search-mb", type=float, default=5, help="target size of search files")
    a = ap.parse_args(argv)
    build(a.source, a.out, a.site, a.max_mb, a.search_mb)


if __name__ == "__main__":
    main()
