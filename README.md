# Daily Herald Data Hub

One place for the AI-generated data about the Daily Herald photographic archive (Science Museum Group), and for the tools that search, explore and visualise it.

* **Site:** `https://<owner>.github.io/<repo>/`. It is built and deployed automatically by GitHub Actions.
* **Data:** `data/source/` holds the original files, converted to JSON chunks of at most 20 MB.

## How it fits together

```
Google Drive (originals: .json, .xlsx)
      │  colab/01_initial_setup.ipynb  → scripts/prepare_sources.py
      ▼
data/source/<source>/part-0001.json …     ← committed to git (≤ 20 MB each)
data/source/manifest.json                     inputs, checksums, counts, renamed columns
data/source/reports/*_duplicates.csv          uids that appeared twice
      │  GitHub Action → scripts/build.py (on every push)
      ▼
_site/                                     ← deployed to Pages, never committed
  index.html, tools/*.html                    copied from site/
  data/manifest.json                          counts + list of every data file
  data/records/part-*.json                    full record per photograph (all sections)
  data/search/part-*.json                     card fields for the search page (~5 MB files)
  data/search_extra/part-*.json               hidden metadata + rationale text (loaded on demand)
  data/visual/part-*.json                     bounding boxes per photograph (compact)
  data/widgets/<view>/part-*.json             data shaped for the widgets (see below)
  data/lookups/<name>/part-*.json             optional lookup tables (e.g. a place gazetteer)
```

Only `data/source/` is versioned, so the repository grows only when the source data actually changes. Everything in `_site/` is rebuilt from it in about 15 seconds.

## First-time set-up (about 15 minutes)

### 1. Create the repository
1. On GitHub: **New repository**. Give it a name (e.g. `daily-herald-data-hub`) and make it **Public**, because free accounts only get Pages for public repos. Do **not** add a README, licence or .gitignore; it must be empty.
2. In the new repo: **Settings → Pages → Build and deployment → Source: _GitHub Actions_**.

### 2. Create an access token for Colab
1. GitHub → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Repository access:** *Only select repositories* → your new repo.
3. **Permissions → Repository permissions:**
   * **Contents:** Read and write
   * **Workflows:** Read and write. This is needed to push `.github/workflows/deploy.yml`.
4. Copy the token. In Colab, open the 🔑 **Secrets** panel, add `GITHUB_TOKEN` and switch on *Notebook access*.

### 3. Put the files on Drive
1. Upload `dh-data-hub.zip` (this starter repo) to Drive, e.g. `MyDrive/DH_Data_Hub/dh-data-hub.zip`.
2. Note where your original data files are on Drive.

### 4. Run the notebook
Open `colab/01_initial_setup.ipynb` in Colab (**File → Upload notebook**). Edit **cell 1** (repo name, Drive paths, the `SOURCES` list), then run the cells in order:

| Cell | What it does |
|---|---|
| 2 | Mounts Drive and lists every input file it found, with its size |
| 3 | Clones the repo; on the first run it unpacks the starter files into it |
| 4 | Converts the originals into `data/source/` chunks of at most 20 MB, and prints counts, duplicates and repeated column names |
| 5 | Optional: runs the website build exactly as GitHub will, and lets you preview the hub |
| 6 | Commits and pushes |

Then open the repo's **Actions** tab. When *Build and deploy hub* turns green, the site is live.

## How the widgets get their data

Every tool in `site/tools/` includes `site/assets/hub-data.js`. When it opens, it loads its data from the hub automatically: it shows a progress banner, then hands the rows to the widget's existing loading code. Nothing needs to be uploaded. Each tool still has its own "load a file" option for exploring a different export, and that is also what it falls back to when opened straight from disk.

`build.py` writes each view in exactly the shape the widgets were originally written for:

| View | Shape | Used by |
|---|---|---|
| `widgets/compact_v6` | compact v6 JSON (as in `data_5–10.json`); metadata from the spreadsheet is converted to it | AI Transcription Dashboard, Photograph Archive Explorer, Textual Analysis |
| `widgets/v1` | flat transcription JSON (as in `data_1–4.json`) | AI Transcription Dashboard |
| `widgets/dates` | slim spreadsheet-style row per dated photo (`gem_date_flat`, `date`, places, activity…) | Date Explorer, Archive Weather |
| `widgets/visual_table` | pipeline-spreadsheet row per photo with visual elements | Visual Elements Workbench, Spatial Explorer |

In `visual_table`, the repeated spreadsheet columns (`gem_image_used`, `gem_model`, token counts…) come from the **visual-elements run**. Previously, loading the raw spreadsheet gave the tools the metadata run's values, so boxes were drawn on the back of the print.

**Archive Weather map.** The map needs coordinates, which the AI data doesn't have. Add your gazetteer as a *lookup* source in the notebook (there's a commented example in cell 1):

```python
{"name": "place_gazetteer", "format": "xlsx", "role": "lookup", "sheet": "place_gazetteer",
 "inputs": [f"{DRIVE}/Daily Herald/archive_weather_data.xlsx"]},
{"name": "category_map", "format": "xlsx", "role": "lookup", "sheet": "category_map",
 "inputs": [f"{DRIVE}/Daily Herald/archive_weather_data.xlsx"]},
```

The gazetteer's `raw_place` values should match the standardised place names, e.g. `London, Greater London, England, United Kingdom`. The rest of the forecast works without it.

## Updating the code
When you get an updated `dh-data-hub.zip`, put it on Drive in place of the old one. Use the latest notebook, copy your settings into cell 1, keep `UPDATE_CODE_FROM_ZIP = True`, and run all the cells. The code files are replaced; `data/source/` is never touched.

## Updating the data later
Add or replace files on Drive, update `SOURCES` if needed, and re-run the notebook. Unchanged chunks produce no git changes.

To rebuild the site without new data (for example after editing `site/`), use **Actions → Build and deploy hub → Run workflow**.

## Sources and the record model

| Source name | Format | Example | Becomes section |
|---|---|---|---|
| `transcriptions_v1` | flat JSON | `data_1–4.json` | `transcription_v1` |
| `metadata_v6` | compact JSON (`te`, `conf`, `rat`…) | `data_5–10.json` | `metadata_v6` |
| `pipeline_xlsx` | pipeline spreadsheet | `*_huggingface_games.xlsx` | `metadata_v6` (preferred over the compact JSON) + `visual_elements` |

`build.py` merges all sources on `uid`. Each record looks like this:

```jsonc
{
  "uid": "co8798550", "identifier": "1983-5236/41317", "title": "…", "description": "…",
  "catalogue_date": "1931-11-17", "maker": "…", "catalogue_url": "…", "images": ["…1001_.jpg", "…1002_.jpg"],
  "sections": ["metadata_v6", "visual_elements"],
  "metadata_v6": { "text_elements": […], "photographers": […], "people_depicted": […], "places_depicted": […],
                   "date_primary": {…}, "rights": […], "editorial": {…}, "processing": {…} },
  "visual_elements": { "controlled_objects": [{ "term", "box_2d", "confidence_score", "rationale" }], "other_objects": […],
                       "image": { "image_width", … }, "processing": { "image_used", "model", … } },
  "provenance": [{ "source": "pipeline_xlsx", "file": "…xlsx", "row": 2 }]
}
```

Notes:
* **Transcriptions:** every record has one top-level `transcription` (`{"text", "from"}`), taken from the newest source that has one: the v6 text elements, otherwise the v1 `Transcribed_Text`. The old Gemini 2.5 Flash columns (`output - gemini-2.5…`) are left out. The notebook drops them from `data/source/` with `"drop_fields": ["output - *"]`, and `build.py` ignores them in any case.
* **Compact keys are expanded** to the spreadsheet's names, for example `te→text_elements`, `leg→legibility`, `conf→confidence_score`, `rat→rationale`, `from→derived_from`, `cert→identification_certainty`, `rel→relationship_label`.
* **Repeated spreadsheet columns** (`gem_model`, `gem_image_used`, the token counts…) belong to two different pipeline runs. `prepare_sources.py` renames the second copy to `…__2`. `build.py` assigns everything from `gem_status` onwards to the visual-elements run. Change `VISUAL_RUN_MARKERS` in `build.py` if your column order differs.
* **Text repair:** mojibake such as `Â»` or `ÃƒÆ’Ã¢â‚¬Å¡…` is repaired to `»` in the built data only. The files in `data/source/` stay exactly as exported.
* **Left out of the built data:** `gem_raw_response` (truncated to 4,000 characters in the spreadsheet) and the `*_objects_flat` columns. Both remain in `data/source/`.

## Running locally
```bash
pip install -r requirements.txt
python scripts/prepare_sources.py --config config/sources.example.json   # paths to your originals
python scripts/build.py                                                   # → _site/
python -m http.server -d _site 8000                                       # open http://localhost:8000
```

## Limits to keep in mind
* GitHub blocks files over 100 MB. Every file here is capped at 20 MB, and the workflow fails if one is larger.
* GitHub recommends keeping a repository under about 1 GB. Only `data/source/` counts towards this.
* A published Pages site can be up to 1 GB, with a soft limit of 100 GB of bandwidth per month.
* Git LFS doesn't work with Pages, so it isn't used.
