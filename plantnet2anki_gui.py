#!/usr/bin/env python3
"""
plantnet2anki_gui.py
====================
PlantNet → Anki with a local web interface.

Run:
    python plantnet2anki_gui.py

Then open http://localhost:7842 in your browser (opened automatically).

Requirements:
    pip install requests genanki
"""

import csv
import io
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import requests
    import genanki
except ImportError as _missing:
    print(f"Missing dependency: {_missing}")
    print("Please run:")
    print("  pip install requests genanki")
    sys.exit(1)

PORT = 7842

# ── Botanical constants ───────────────────────────────────────────────────────
PLANTNET_API = "https://my-api.plantnet.org/v2/identify/all"
HEADERS      = {"User-Agent": "plantnet2anki-gui/1.0"}

# ── Global state ──────────────────────────────────────────────────────────────
state = {
    "plants":    [],       # list of dicts after CSV parsing
    "log":       [],       # list of log lines sent to frontend via SSE
    "progress":  0,
    "progress_label": "",        # 0-100
    "running":   False,
    "done":      False,
    "stopped":   False,
    "gen_id":    0,
    "deck_pkg":  None,     # final Anki .txt content
    "deck_name": "PlantNet – Botany",
}
state_lock = threading.Lock()
review_event = threading.Event()   # set by /review_finish to resume a paused generation


def log(msg, level="info"):
    with state_lock:
        state["log"].append({"msg": msg, "level": level})


def set_progress(pct, label=None):
    with state_lock:
        state["progress"] = pct
        if label is not None:
            state["progress_label"] = label


# ── CSV parsing ───────────────────────────────────────────────────────────────
def detect_sep(text):
    first = text.split("\n")[0]
    if "\t" in first: return "\t"
    if ";" in first:  return ";"
    return ","


def parse_csv(text):
    sep  = detect_sep(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=sep)
    rows = [{k.strip(): v.strip() for k, v in row.items()} for row in reader]
    return rows, sep


def find_col(headers, *candidates):
    for h in headers:
        for c in candidates:
            if c.lower() in h.lower():
                return h
    return None


def extract_csv_from_multipart(body, content_type):
    """Extract the raw CSV text out of a multipart/form-data POST body.
    Returns the decoded text, or None if no file part could be found."""
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[9:].strip()
            break
    if not boundary:
        return None
    boundary_bytes = ("--" + boundary).encode()
    for part in body.split(boundary_bytes):
        if b"filename=" in part and b"\r\n\r\n" in part:
            _, content = part.split(b"\r\n\r\n", 1)
            content = content.rstrip(b"\r\n--")
            return content.decode("utf-8", errors="replace")
    return None


def group_by_species(rows):
    if not rows:
        return []
    headers = list(rows[0].keys())
    col_sci    = find_col(headers, "original name", "scientific name", "species", "nom_sci") or headers[min(4,len(headers)-1)]
    col_family = find_col(headers, "family", "famille")
    col_images = find_col(headers, "images", "image")
    col_date   = find_col(headers, "date observed", "date")
    col_common = find_col(headers, "current common name", "common name", "nom vernaculaire")
    col_note   = find_col(headers, "personal note", "note personnelle")

    by = {}
    for row in rows:
        sci = row.get(col_sci, "").strip()
        if not sci: continue
        if sci not in by:
            by[sci] = {"scientific": sci,
                       "family":     row.get(col_family, "") if col_family else "",
                       "own_images": [], "observations": 0, "last_date": "",
                       "csv_common_name": "", "personal_notes": []}
        by[sci]["observations"] += 1
        if col_date:
            by[sci]["last_date"] = row.get(col_date, "") or by[sci]["last_date"]
        if col_images and row.get(col_images):
            for u in re.split(r"[\s|]+", row[col_images]):
                u = u.strip()
                if u.startswith("http"):
                    by[sci]["own_images"].append(u)
        if col_common and not by[sci]["csv_common_name"]:
            val = (row.get(col_common) or "").strip()
            if val:
                by[sci]["csv_common_name"] = val
        if col_note:
            val = (row.get(col_note) or "").strip()
            if val and val not in by[sci]["personal_notes"]:
                by[sci]["personal_notes"].append(val)
    return list(by.values())


# ── Name utilities ────────────────────────────────────────────────────────────
def clean_scientific_name(name):
    """
    Strip author citation from scientific name.
    e.g. 'Quercus robur L.' → 'Quercus robur'
         'Lavandula angustifolia Mill.' → 'Lavandula angustifolia'
    Keeps only the first two words (genus + species epithet).
    Handles subspecies/variety: 'Rosa canina subsp. canina' → kept as-is.
    """
    name = name.strip()
    # Remove anything in parentheses (author citations)
    name = re.sub(r"\s*\([^)]*\)", "", name)
    # Split into tokens
    tokens = name.split()
    if len(tokens) <= 2:
        return name
    # If 3rd token is subsp./var./f./ssp. keep up to 4 tokens
    ranks = {"subsp.", "var.", "f.", "ssp.", "subvar.", "forma"}
    if len(tokens) >= 3 and tokens[2].lower() in ranks and len(tokens) >= 4:
        return " ".join(tokens[:4])
    # Otherwise keep only genus + species epithet
    return " ".join(tokens[:2])

def _norm_name(s):
    """Normalize a name for comparison: lowercase, no accents, no extra spaces."""
    s = unicodedata.normalize("NFKD", s.strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _cn_plantnet(sci, api_key, lang="en"):
    """Common name via PlantNet species search (genus prefix — no image needed)."""
    if not api_key:
        return ""
    cleaned = clean_scientific_name(sci).lower()
    genus = sci.split()[0]
    try:
        url = (f"https://my-api.plantnet.org/v2/projects/k-world-flora/species"
               f"?prefix={requests.utils.quote(genus)}&lang={lang}&api-key={api_key}")
        r = requests.get(url, headers=HEADERS, timeout=8)
        if not r.ok:
            return ""
        data = r.json() if isinstance(r.json(), list) else []
        for sp in data:
            if sp.get("scientificNameWithoutAuthor", "").lower() == cleaned:
                names = sp.get("commonNames", [])
                if names:
                    return names[0]
        for sp in data:
            sp_name = sp.get("scientificNameWithoutAuthor", "").lower()
            if cleaned in sp_name or sp_name in cleaned:
                names = sp.get("commonNames", [])
                if names:
                    return names[0]
    except Exception:
        pass
    return ""


def _cn_gbif(sci, lang="en"):
    """Common name via GBIF vernacular names. Tries requested language only —
    PlantNet/iNaturalist cover the fallback languages in the cascade."""
    lang_map = {
        "en": ("eng", "en"), "fr": ("fra", "fre", "fr"),
        "de": ("deu", "ger", "de"), "es": ("spa", "es"),
    }
    pref_codes = lang_map.get(lang, ("eng", "en"))
    try:
        r = requests.get(f"{GBIF_API}/species/match",
                         params={"name": sci, "verbose": "false"},
                         headers=HEADERS, timeout=8)
        if not r.ok:
            return ""
        key = r.json().get("usageKey") or r.json().get("speciesKey")
        if not key:
            return ""
        r2 = requests.get(f"{GBIF_API}/species/{key}/vernacularNames",
                          params={"limit": 100}, headers=HEADERS, timeout=8)
        if not r2.ok:
            return ""
        names = r2.json().get("results", [])
        for n in names:
            if n.get("language", "").lower() in pref_codes:
                return n.get("vernacularName", "")
    except Exception:
        pass
    return ""


def _cn_inat(sci, lang="en"):
    """Common name via iNaturalist taxa search."""
    try:
        r = requests.get(f"{INAT_API}/taxa",
                         params={"q": sci, "per_page": 5}, headers=HEADERS, timeout=8)
        if not r.ok:
            return ""
        results = r.json().get("results", [])
        if not results:
            return ""
        cleaned = clean_scientific_name(sci).lower()
        target = next((res for res in results if res.get("name", "").lower() == cleaned), results[0])
        if lang == "fr":
            for tn in target.get("taxon_names", []):
                if tn.get("locale") == "fr" and tn.get("name"):
                    return tn["name"]
        return target.get("preferred_common_name", "") or ""
    except Exception:
        return ""


def fetch_common_names_all(sci, api_key, lang="en"):
    """
    Query PlantNet, GBIF and iNaturalist for a common name — always all three,
    never stopping early.
    - If every source that found something agrees (accent/case-insensitive),
      a single name is returned.
    - If they disagree, all distinct names are returned, each annotated with
      the source(s) that produced it, e.g.
      "Lavande officinale (PlantNet/GBIF) ; Lavande vraie (iNaturalist)".
    Returns (display_string, status).
    """
    found = []
    pn = _cn_plantnet(sci, api_key, lang=lang)
    if pn:
        found.append((pn, "PlantNet"))
    gb = _cn_gbif(sci, lang=lang)
    if gb:
        found.append((gb, "GBIF"))
    it = _cn_inat(sci, lang=lang)
    if it:
        found.append((it, "iNaturalist"))

    if not found:
        return "", "not found"

    groups, order = {}, []
    for name, src in found:
        key = _norm_name(name)
        if key not in groups:
            groups[key] = {"name": name, "sources": []}
            order.append(key)
        groups[key]["sources"].append(src)

    if len(order) == 1:
        g = groups[order[0]]
        return g["name"], "+".join(g["sources"])

    parts = [f'{groups[k]["name"]} ({"/".join(groups[k]["sources"])})' for k in order]
    return " ; ".join(parts), "multiple sources disagree"
# ── PlantNet photos ───────────────────────────────────────────────────────────
# ── Photo sources ────────────────────────────────────────────────────────────
#
# Storage model:
#   plant["images"] = {
#       "own":      [{"url": "...", "source": "own"}, ...],
#       "untagged": [{"url": "...", "source": "gbif"|"plantnet"}, ...],
#   }
#
# Fetch order for "untagged": GBIF → PlantNet, stopping once the requested
# photo count is reached. (iNaturalist was removed as a photo source; it's
# still used for vernacular names, see fetch_common_names_all.)
# PlantNet related images: 1 API call per reference photo submitted. During
# review, a rejected photo can itself become the new reference photo for a
# fresh PlantNet lookup — works with any photo of the plant, not just the
# CSV's own photo.
# GBIF: free, independent random draws.

GBIF_API = "https://api.gbif.org/v1"
INAT_API = "https://api.inaturalist.org/v1"


def collect_plantnet_related(image_url, api_key, target_sci, n, exclude=None):
    """
    Submit ONE reference photo — any photo of the plant, whether it's the
    CSV's own photo, a GBIF photo, or even a photo PlantNet returned earlier
    — to PlantNet with include-related-images=true, and return up to n
    visually-similar photo URLs FOR THE TARGET SPECIES specifically.

    PlantNet returns several candidate species per identification, each with
    its own set of related images. Rather than trusting the top guess blindly
    (which can be wrong, especially from a low-quality reference photo), this
    scans every candidate for one whose name matches target_sci, and only
    uses that candidate's images.

    Returns (urls, reserve, status, identified, matched):
      urls       -> up to n URLs to use right away
      reserve    -> any extra URLs beyond n, kept for photo-review replacements
      identified -> {"name": ..., "score": 0-100} for whichever candidate was
                     used (the matching one), or None if nothing usable
      matched    -> True if target_sci was found among PlantNet's candidates;
                     False means the caller should fall back to another
                     source instead of trusting unrelated images
    """
    exclude = exclude or set()
    if not image_url or not api_key:
        return [], [], "skipped (need a reference photo + API key)", None, False
    try:
        img_r = requests.get(image_url, timeout=15)
        if not img_r.ok:
            return [], [], f"could not fetch reference photo HTTP {img_r.status_code}", None, False
        r = requests.post(
            PLANTNET_API,
            files=[("images", ("plant.jpg", img_r.content, "image/jpeg"))],
            data={"organs": ["auto"]},
            params={"include-related-images": "true", "no-reject": "true",
                    "nb-results": 10, "lang": "en", "api-key": api_key},
            headers=HEADERS, timeout=20
        )
        if not r.ok:
            return [], [], f"API HTTP {r.status_code}: {r.text[:60]}", None, False
        results_list = r.json().get("results", [])
        target_norm = clean_scientific_name(target_sci).lower()
        match = None
        for res in results_list:
            name = res.get("species", {}).get("scientificNameWithoutAuthor", "")
            if clean_scientific_name(name).lower() == target_norm:
                match = res
                break
        if not match:
            top_names = ", ".join(
                res.get("species", {}).get("scientificNameWithoutAuthor", "?")
                for res in results_list[:3]
            ) or "nothing"
            return [], [], f"target species not among PlantNet's candidates (got: {top_names})", None, False

        identified = {"name": match.get("species", {}).get("scientificNameWithoutAuthor", ""),
                      "score": round(match.get("score", 0) * 100)}
        related = match.get("images", [])
        random.shuffle(related)  # avoid always keeping the API's own default order
        all_urls = []
        for img in related:
            u = (img.get("url") or {}).get("m") or (img.get("url") or {}).get("s", "")
            if u and u not in all_urls and u not in exclude:
                all_urls.append(u)
        urls, reserve = all_urls[:n], all_urls[n:]
        status = f"OK — matched {identified['name']} ({identified['score']}%), {len(urls)} used, {len(reserve)} in reserve"
        return urls, reserve, status, identified, True
    except Exception as e:
        return [], [], f"error: {e}", None, False


def collect_gbif_urls(sci, n, seen=None):
    """
    Fetch up to n photo URLs from GBIF occurrences , each drawn
    from an INDEPENDENT random position across the full set of image-bearing
    occurrences — not a single random window (which would still clump
    results from the same import batch/collector together).
    `seen` is a set of URLs to skip (already used elsewhere).
    Returns (urls, status).
    """
    seen = seen or set()
    try:
        r = requests.get(f"{GBIF_API}/species/match",
                         params={"name": sci, "verbose": "false"},
                         headers=HEADERS, timeout=8)
        if not r.ok:
            return [], f"species match HTTP {r.status_code}"
        key = r.json().get("usageKey") or r.json().get("speciesKey")
        if not key:
            return [], "not found in GBIF"

        rc = requests.get(f"{GBIF_API}/occurrence/search",
                          params={"taxonKey": key, "mediaType": "StillImage", "limit": 0},
                          headers=HEADERS, timeout=8)
        total = rc.json().get("count", 0) if rc.ok else 0
        if total == 0:
            return [], "no images in GBIF"

        picked, tried = [], set()
        max_attempts = min(total, n * 5 + 10)
        attempts = 0
        while len(picked) < n and attempts < max_attempts and len(tried) < total:
            offset = random.randint(0, total - 1)
            if offset in tried:
                continue
            tried.add(offset)
            attempts += 1
            r2 = requests.get(f"{GBIF_API}/occurrence/search",
                              params={"taxonKey": key, "mediaType": "StillImage",
                                      "limit": 1, "offset": offset},
                              headers=HEADERS, timeout=8)
            if not r2.ok:
                continue
            results = r2.json().get("results", [])
            if not results:
                continue
            for m in results[0].get("media", []):
                u = m.get("identifier", "")
                if u and u.startswith("http") and u not in seen and u not in picked:
                    picked.append(u)
                    break
        return picked, f"{len(picked)}/{total} (random draws, {attempts} tries)"
    except Exception as e:
        return [], f"error: {e}"


def fetch_photos_for_plant(plant, sci, n_photos, api_key, source):
    """
    Main photo fetching function. Fills plant["images"]["untagged"] with up
    to n_photos photos (each {"url":..., "source":...}), pulling from the
    sources in this order: GBIF → PlantNet, stopping as soon as n_photos is
    reached. (iNaturalist was removed as a photo source — no usable photos
    were being found there; it's still used for vernacular names.)

    Also populates plant["_fetch_state"] (seen-urls tracking) and
    plant["_reserve"] (PlantNet overflow) so the photo-review step can fetch
    a same-source replacement for any photo the user rejects.

    source: "all" | "plantnet" | "gbif" | "none"
    """
    images = {"untagged": []}

    fetch_state = plant["_fetch_state"]
    reserve     = plant["_reserve"]
    seen        = set(fetch_state["seen_urls"])

    if source == "none" or n_photos == 0:
        return images, "disabled"

    use_gbif = source in ("all", "gbif")
    use_pn   = source in ("all", "plantnet") and api_key and plant.get("own_images")
    summary  = []
    budget   = n_photos

    # ── 1. GBIF ────────────────────────────────────────────────────────────────
    if use_gbif and budget > 0:
        log("    Collecting from GBIF…")
        gbif_urls, gbif_st = collect_gbif_urls(sci, budget, seen=seen)
        log(f"    GBIF: {gbif_st}")
        added = 0
        for u in gbif_urls:
            if u not in seen and added < budget:
                images["untagged"].append({"url": u, "source": "gbif"})
                seen.add(u)
                added += 1
        summary.append(f"GBIF:{added}")
        budget -= added

    # ── 2. PlantNet ────────────────────────────────────────────────────────────
    if use_pn and budget > 0:
        seed_url = plant["own_images"][0]
        pn_urls, pn_reserve, pn_status, pn_id, matched = collect_plantnet_related(
            seed_url, api_key, sci, budget, exclude=seen
        )
        log(f"    PlantNet related: {pn_status}")
        if pn_id:
            log(f"    PlantNet identified reference photo as: {pn_id['name']} ({pn_id['score']}%)", "ok")
        added = 0
        if matched:
            for u in pn_urls:
                if u not in seen and added < budget:
                    images["untagged"].append({"url": u, "source": "plantnet"})
                    seen.add(u)
                    added += 1
            reserve["plantnet"] = pn_reserve
        else:
            log("    PlantNet didn't recognize this species — falling back to GBIF for these slots.", "warn")
            fb_urls, fb_st = collect_gbif_urls(sci, budget, seen=seen)
            log(f"    GBIF fallback: {fb_st}")
            for u in fb_urls:
                if u not in seen and added < budget:
                    images["untagged"].append({"url": u, "source": "gbif"})
                    seen.add(u)
                    added += 1
        summary.append(f"PlantNet:{added}")
        budget -= added

    fetch_state["seen_urls"] = seen

    # ── Summary ───────────────────────────────────────────────────────────────
    status   = " | ".join(summary) + f" → {len(images['untagged'])}/{n_photos}"
    return images, status


def locate_slot(images, flat_index):
    """Map a flat photo-slot index (as produced by collect_photo_slots) back
    to (key, position) inside the raw `images` dict."""
    order = ["own", "untagged"]
    counter = 0
    for key in order:
        lst = images.get(key, [])
        for pos in range(len(lst)):
            if counter == flat_index:
                return key, pos
            counter += 1
    return None, None


def replace_rejected_photo(plant, sci, key, pos, api_key="", try_replace=True, target_source=None, good_refs=None):
    """
    Remove images[key][pos] (a rejected photo). If try_replace is True, also
    try to fetch exactly one replacement — from `target_source` if given
    (lets the user pick a different origin, e.g. a GBIF photo replaced by a
    PlantNet one), otherwise from the SAME source it came from.

    When switching to PlantNet, the reference photo submitted is one of the
    GOOD photos the user kept (`good_refs`) — never the photo being rejected,
    since a bad photo tends to produce a bad/ambiguous identification and
    pull in images of the wrong species. If PlantNet's candidates don't
    include the target species at all, this falls back to GBIF instead of
    trusting an unrelated species' photos.

    Returns (note, new_url, plantnet_id):
      - on success: (None or an info note, new_url, plantnet_id)
      - if try_replace is False, or no replacement was available:
        (note_or_None, None, plantnet_id) — the photo is simply dropped and
        the species ends up with one fewer photo.
      plantnet_id is {"name":..., "score":...} when a fresh PlantNet call was
      made this time (so the caller can show what PlantNet identified the
      reference photo as) — None otherwise.
    """
    images = plant["images"]
    if key not in images or pos >= len(images[key]):
        return None, None, None
    item   = images[key].pop(pos)
    source = item.get("source", "")

    fetch_state = plant["_fetch_state"]
    seen = fetch_state["seen_urls"]
    seen.add(item["url"])

    if not try_replace:
        return None, None, None

    use_source = target_source or source

    new_url = None
    plantnet_id = None
    note = None
    if use_source == "own":
        own  = plant.get("own_images", [])
        used = fetch_state.get("own_used", 0)
        if used < len(own):
            new_url = own[used]
            fetch_state["own_used"] = used + 1
    elif use_source == "plantnet":
        pool = plant["_reserve"]["plantnet"]
        while pool:
            candidate = pool.pop(random.randrange(len(pool)))
            if candidate not in seen:
                new_url = candidate
                break
        # Reserve empty (or never populated): submit one of the GOOD photos
        # still kept for this species as the new PlantNet reference — not
        # the photo being rejected, which is presumably why it's a bad seed.
        if new_url is None and api_key:
            refs = [u for u in (good_refs or []) if u not in seen]
            if refs:
                ref_url = random.choice(refs)
                pn_urls, pn_reserve, _pn_status, plantnet_id, matched = collect_plantnet_related(
                    ref_url, api_key, sci, 3, exclude=seen
                )
                if matched:
                    candidates = pn_urls + pn_reserve
                    if candidates:
                        new_url = candidates[0]
                        plant["_reserve"]["plantnet"].extend(candidates[1:])
                else:
                    note = (f"{sci}: PlantNet didn't recognize this species from the "
                            f"reference photo — falling back to GBIF.")
                    use_source = "gbif"
                    urls, _ = collect_gbif_urls(sci, 3, seen=seen)
                    new_url = next((u for u in urls if u not in seen), None)
            else:
                note = f"{sci}: no other photo available to use as a PlantNet reference."
    elif use_source == "gbif":
        urls, _ = collect_gbif_urls(sci, 3, seen=seen)
        new_url = next((u for u in urls if u not in seen), None)

    if new_url:
        images[key].append({"url": new_url, "source": use_source})
        seen.add(new_url)
        return note, new_url, plantnet_id

    label = {"own": "your photos", "plantnet": "PlantNet",
             "gbif": "GBIF", "inat": "iNaturalist"}.get(use_source, use_source or "?")
    fail_msg = f"{sci}: no more photos available from {label} — one fewer photo for this species."
    combined = f"{note} {fail_msg}" if note else fail_msg
    return combined, None, plantnet_id

# ── Anki builder (genanki → Basic + JS random photo) ─────────────────────────
#
# Architecture:
#   - One NOTE per species → one CARD per species
#   - Field PhotosHTML: pre-rendered <img> tags for all available photos
#   - Front template: JS picks a random photo each review
#   - Back template: {{FrontSide}} + species name + common name + info
#
# This gives: one card per species, different photo shown on every review.
# Supported by Anki Desktop, AnkiDroid, AnkiMobile.

import hashlib, json as _json

MAX_PHOTO_SLOTS = 12   # kept for collect_photo_slots compatibility
NOTETYPE_NAME   = "PlantNet"
MODEL_ID = int(hashlib.md5(b"PlantNet_JS_v2").hexdigest()[:8], 16)
DECK_ID  = int(hashlib.md5(b"PlantNet_deck_v1").hexdigest()[:8], 16)

FRONT_TEMPLATE = """
{{PhotosHTML}}
<div style="text-align:center;color:#888;font-size:13px;margin-top:8px;font-family:sans-serif">What plant is this?</div>
<script>
(function init() {
  var imgs = document.querySelectorAll('.pn-img');
  if (!imgs || imgs.length === 0) { setTimeout(init, 80); return; }

  // Pick a new random photo every time the card is shown
  // On the back side ({{FrontSide}}), the imgs are already visible — skip
  var alreadyShown = false;
  for (var i = 0; i < imgs.length; i++) {
    if (imgs[i].style.display !== 'none') { alreadyShown = true; break; }
  }
  if (alreadyShown) return;

  var idx = Math.floor(Math.random() * imgs.length);
  for (var i = 0; i < imgs.length; i++) {
    imgs[i].style.display = (i === idx) ? 'block' : 'none';
  }
})();
</script>
""".strip()


BACK_TEMPLATE = """
{{FrontSide}}
<hr style="border:none;border-top:1px solid #eee;margin:12px 0">
<div style="font-family:sans-serif;font-size:14px;line-height:1.9;padding:4px">
  <div style="font-style:italic;font-size:1.2em;color:#085041;margin-bottom:2px">{{ScientificName}}</div>
  {{#CommonName}}<div style="color:#1D9E75;font-weight:500;margin-bottom:8px">{{CommonName}}</div>{{/CommonName}}
  {{Info}}
</div>
""".strip()

CSS = (
    ".card { font-family: sans-serif; font-size: 14px; background: #FAFAF8; }"
    " img { max-width: 100%; border-radius: 8px; }"
    " hr { border: none; border-top: 1px solid #eee; }"
)


def make_genanki_model(front_template=None):
    fields = [
        {"name": "PhotosHTML"},
        {"name": "ScientificName"},
        {"name": "CommonName"},
        {"name": "Info"},
    ]
    templates = [{"name": "Card 1", "qfmt": front_template or FRONT_TEMPLATE, "afmt": BACK_TEMPLATE}]
    model = genanki.Model(MODEL_ID, NOTETYPE_NAME, css=CSS)
    model.set_fields(fields)
    model.set_templates(templates)
    return model


def collect_photo_slots(images, lang="en"):
    """Return list of {url, source, key} dicts for all available photos.
    `key` is the raw images-dict key ('own' or 'untagged') and `source` is
    where the photo came from — used by the photo-review step to know how
    to fetch a same-source replacement."""
    slots = []
    order = ["own", "untagged"]
    for key in order:
        for item in images.get(key, []):
            if len(slots) >= MAX_PHOTO_SLOTS:
                return slots
            slots.append({
                "url": item["url"],
                "source": item.get("source", ""),
                "key": key,
            })
    return slots


def build_info_html(plant, lang="en"):
    i = plant.get("info", {})
    rows = []
    fam = plant.get("family", "")
    if fam:
        label = "Famille" if lang == "fr" else "Family"
        rows.append(f"<b>{label}</b> : {fam}" if lang == "fr" else f"<b>{label}</b>: {fam}")

    extra_name = i.get("extra_common_name", "")
    extra_lang = i.get("extra_lang", "")
    if extra_name:
        label = f"Nom ({extra_lang.upper()})" if lang == "fr" else f"Name ({extra_lang.upper()})"
        rows.append(f"<b>{label}</b> : {extra_name}")

    html = "<br>".join(rows)

    note = i.get("personal_note", "")
    if note:
        label = "Note personnelle" if lang == "fr" else "Personal note"
        note_html = (f'<div style="margin-top:10px;padding:6px 10px;background:#FAEEDA;'
                     f'border-radius:6px;font-style:italic">📝 <b>{label}</b> : {note}</div>')
        html = html + note_html if html else note_html
    return html


def build_anki_pkg(plants, deck_name, media_files=None, lang="en"):
    """
    Build .apkg from enriched plant data.
    media_files: list of local file paths to embed (for offline mode).
                 The corresponding slots already use local filenames.
    """
    import io

    question = "Quelle est cette plante ?" if lang == "fr" else "What plant is this?"
    front    = FRONT_TEMPLATE.replace("What plant is this?", question)
    model    = make_genanki_model(front_template=front)
    deck  = genanki.Deck(DECK_ID, deck_name)

    for p in plants:
        sci    = p["scientific"]
        info   = p.get("info", {})
        common = info.get("common_name", "")
        slots  = p.get("_slots", collect_photo_slots(p.get("images", {}), lang=lang))

        # Build PhotosHTML: all images hidden, JS will show one randomly
        img_style = 'max-width:300px;max-height:240px;border-radius:8px;display:none;margin:0 auto'
        photos_html = "".join(
            f'<img src="{s["url"]}" class="pn-img"'
            f' style="{img_style}">'
            for s in slots
        )
        fields = [photos_html, sci, common, build_info_html(p, lang=lang)]
        note   = genanki.Note(model=model, fields=fields,
                              guid=genanki.guid_for(sci))
        deck.add_note(note)

    pkg = genanki.Package(deck)
    pkg.media_files = media_files or []
    buf = io.BytesIO()
    pkg.write_to_file(buf)
    return buf.getvalue()

# ── Generation worker ─────────────────────────────────────────────────────────
def run_generation(config, my_gen_id=None):
    def should_stop():
        with state_lock:
            return state["stopped"] or (my_gen_id is not None and state["gen_id"] != my_gen_id)

    with state_lock:
        state["running"]  = True
        state["done"]     = False
        state["stopped"]  = False
        state["log"]      = []
        state["progress"] = 0
        state["deck_pkg"] = None

    # Build the list of selected species from the config sent by the browser.
    # This ensures a fresh start even after Stop, with the current selection.
    selected_names = {p["scientific"] for p in config.get("plants", [])}
    with state_lock:
        plants = [p for p in state["plants"] if p["scientific"] in selected_names]

    api_key      = config.get("api_key", "")
    n_photos     = int(config.get("n_photos", 4))
    photo_source = config.get("photo_source", "all")
    include_own  = config.get("include_own", True)
    review_photos = config.get("review_photos", False)
    lang         = config.get("lang", "en")
    extra_lang_enabled = config.get("extra_lang_enabled", False)
    extra_lang         = config.get("extra_lang", "")

    # Made available to the /review_data, /review_reject HTTP handlers
    with state_lock:
        state["gen_lang"]    = lang
        state["gen_api_key"] = api_key
        state["review_pending"] = False

    # Reset per-plant data so previous runs don't bleed through
    for p in plants:
        p.pop("info",         None)
        p.pop("images",       None)
        p.pop("_slots",       None)
        p.pop("_fetch_state", None)
        p.pop("_reserve",     None)

    log(f"Starting — {len(plants)} species | {n_photos} photo(s)/species | own photos: {include_own} | source: {photo_source}")

    for i, plant in enumerate(plants):
        if should_stop():
            break
        sci = plant["scientific"]
        pct = int(5 + (i / len(plants)) * 80)
        set_progress(pct)
        log(f"[{i+1}/{len(plants)}] {sci}")
        plant["info"]         = {}
        plant["images"]       = {}
        plant["_fetch_state"] = {"own_used": 0, "seen_urls": set()}
        plant["_reserve"]     = {"plantnet": []}

        # Own photos (only if include_own is enabled)
        if include_own and plant["own_images"]:
            n_own = min(4, len(plant["own_images"]))
            plant["images"]["own"] = [{"url": u, "source": "own"} for u in plant["own_images"][:n_own]]
            plant["_fetch_state"]["own_used"] = n_own
            log(f"  📷 {n_own} own photo(s)", "ok")

        # Photos from external sources (GBIF → PlantNet)
        if n_photos > 0:
            extra_images, status = fetch_photos_for_plant(
                plant, sci, n_photos, api_key, photo_source
            )
            log(f"  Photos: {status}", "ok" if any(extra_images.values()) else "warn")
            plant["images"].update(extra_images)

        # Common name: use the CSV's "current common name" if present —
        # no PlantNet/GBIF/iNaturalist lookup needed in that case.
        csv_common = plant.get("csv_common_name", "")
        if csv_common:
            plant["info"]["common_name"] = csv_common
            log(f"  Common name: {csv_common} (from CSV)", "ok")
        else:
            cn, cn_status = fetch_common_names_all(sci, api_key, lang=lang)
            if cn:
                plant["info"]["common_name"] = cn
                log(f"  Common name: {cn} ({cn_status})", "ok")
            else:
                log(f"  Common name: {cn_status}", "warn")

        # Optional extra name in another language, on top of the CSV/card-lang name
        if extra_lang_enabled and extra_lang:
            # Skip if we'd just be repeating the lookup we already did above
            already_have = (not csv_common) and (extra_lang == lang)
            if not already_have:
                extra_name, extra_status = fetch_common_names_all(sci, api_key, lang=extra_lang)
                if extra_name:
                    plant["info"]["extra_common_name"] = extra_name
                    plant["info"]["extra_lang"] = extra_lang
                    log(f"  Name ({extra_lang}): {extra_name} ({extra_status})", "ok")
                else:
                    log(f"  Name ({extra_lang}): {extra_status}", "warn")

        # Personal note from the CSV, added to the card if not empty
        notes = plant.get("personal_notes", [])
        if notes:
            plant["info"]["personal_note"] = " ; ".join(notes)
            log(f"  Personal note: {plant['info']['personal_note'][:60]}", "ok")

    # ── Photo review (optional) ────────────────────────────────────────────
    if review_photos and n_photos > 0 and not should_stop():
        log("Waiting for photo review…", "ok")
        set_progress(88, "En attente de validation des photos…")
        with state_lock:
            state["review_pending"] = True
        global review_event
        review_event = threading.Event()
        while not review_event.is_set():
            if should_stop():
                break
            review_event.wait(timeout=0.5)
        with state_lock:
            state["review_pending"] = False
        if not should_stop():
            log("Photo review complete.", "ok")

    set_progress(93)
    deck_name = config.get("deck_name", "PlantNet – Botany")
    embed     = config.get("embed_images", False)

    media_files = []

    if embed:
        import tempfile, os as _os
        # Collect all unique URLs across all plants
        all_slots = []
        for p in plants:
            for slot in collect_photo_slots(p.get("images", {})):
                if not any(s["url"] == slot["url"] for s in all_slots):
                    all_slots.append(slot)

        total_imgs = len(all_slots)
        log(f"Downloading {total_imgs} image(s) for offline embedding…")
        tmpdir = tempfile.mkdtemp()
        downloaded_bytes = 0
        url_to_fname = {}

        for idx, slot in enumerate(all_slots):
            if should_stop():
                log("  Download interrupted.", "warn")
                break
            url = slot["url"]
            pct = 93 + int((idx / max(total_imgs, 1)) * 5)
            set_progress(pct, f"Downloading image {idx+1}/{total_imgs}…")
            try:
                r = requests.get(url, timeout=15, headers=HEADERS)
                if not r.ok:
                    log(f"  ⚠ HTTP {r.status_code}: {url[:55]}", "warn")
                    url_to_fname[url] = None
                    continue
                size_kb = len(r.content) / 1024
                downloaded_bytes += len(r.content)
                ct  = r.headers.get("content-type", "")
                ext = ".png" if ("png" in ct or url.lower().endswith(".png"))                       else ".webp" if ("webp" in ct or url.lower().endswith(".webp"))                       else ".jpg"
                fname = hashlib.md5(url.encode()).hexdigest() + ext
                fpath = _os.path.join(tmpdir, fname)
                with open(fpath, "wb") as f:
                    f.write(r.content)
                media_files.append(fpath)
                url_to_fname[url] = fname
                log(f"  ✓ {idx+1}/{total_imgs}  {size_kb:.0f} KB  {slot['source'] or 'untagged'}", "ok")
            except Exception as ex:
                log(f"  ⚠ {url[:50]}: {ex}", "warn")
                url_to_fname[url] = None

        total_mb = downloaded_bytes / (1024 * 1024)
        log(f"Downloaded {len(media_files)}/{total_imgs} images — {total_mb:.1f} MB total", "ok")

        # Rewrite each plant's slots using local filenames
        for p in plants:
            slots = collect_photo_slots(p.get("images", {}))
            p["_slots"] = [
                {"url": url_to_fname[s["url"]] or s["url"]}
                for s in slots
            ]

    set_progress(98)
    log("Building .apkg…")
    pkg_bytes = build_anki_pkg(plants, deck_name, media_files=media_files, lang=lang)

    total_cards  = len(plants)  # 1 card per species (JS random photo)
    total_photos = sum(sum(len(v) for v in p.get("images",{}).values()) for p in plants)

    stopped = should_stop()
    with state_lock:
        state["deck_pkg"]  = pkg_bytes if not stopped else None
        state["deck_name"] = deck_name
        state["running"]   = False
        state["done"]      = not stopped
        state["progress"]  = 100 if not stopped else pct

    if stopped:
        log("Generation stopped.", "warn")
    else:
        log(f"Done — {len(plants)} species, {total_cards} cards, {total_photos} photos", "ok")


# ── HTTP request handler ──────────────────────────────────────────────────────
log_cursor = 0   # tracks how many log lines the client has already seen


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence default HTTP logs

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        script_dir = Path(__file__).parent

        if path == "/" or path == "/index.html":
            html_path = script_dir / "index.html"
            if html_path.exists():
                with open(html_path, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"error": "index.html not found"}, 404)

        elif path == "/style.css":
            css_path = script_dir / "style.css"
            if css_path.exists():
                with open(css_path, "r", encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/css; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"error": "style.css not found"}, 404)

        elif path == "/status":
            global log_cursor
            with state_lock:
                all_logs  = state["log"]
                new_logs  = all_logs[log_cursor:]
                log_cursor = len(all_logs)
                data = {
                    "progress":       state["progress"],
                    "progress_label": state.get("progress_label", ""),
                    "running":        state["running"],
                    "stopped":        state.get("stopped", False),
                    "done":           state["done"],
                    "review_pending": state.get("review_pending", False),
                    "new_logs":       new_logs,
                    "all_logs":       all_logs,
                }
            self.send_json(data)

        elif path == "/review_data":
            with state_lock:
                lang = state.get("gen_lang", "en")
                result = []
                for idx, p in enumerate(state["plants"]):
                    if not p.get("selected"):
                        continue
                    slots = collect_photo_slots(p.get("images", {}), lang=lang)
                    result.append({
                        "plant_index": idx,
                        "scientific":  p["scientific"],
                        "common_name": p.get("info", {}).get("common_name", ""),
                        "slots":       slots,
                    })
            self.send_json({"plants": result})

        elif path == "/download":
            with state_lock:
                pkg = state.get("deck_pkg")
                name = state.get("deck_name", "PlantNet_Botany")
            if not pkg:
                self.send_json({"error": "No deck generated yet"}, 404)
                return
            safe_name = re.sub(r"[^\w\-]", "_", name) + ".apkg"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
            self.send_header("Content-Length", len(pkg))
            self.end_headers()
            self.wfile.write(pkg)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global log_cursor
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        if path == "/upload":
            try:
                # Parse multipart form data manually
                content_type = self.headers.get("Content-Type", "")
                boundary = None
                for part in content_type.split(";"):
                    part = part.strip()
                    if part.startswith("boundary="):
                        boundary = part[9:].strip()
                        break
                if not boundary:
                    self.send_json({"error": "No boundary in multipart"}, 400)
                    return

                # Extract CSV content from multipart body
                csv_content = None
                boundary_bytes = ("--" + boundary).encode()
                parts = body.split(boundary_bytes)
                for part in parts:
                    if b"filename=" in part and b"\r\n\r\n" in part:
                        _, content = part.split(b"\r\n\r\n", 1)
                        content = content.rstrip(b"\r\n--")
                        csv_content = content.decode("utf-8", errors="replace")
                        break

                if csv_content is None:
                    self.send_json({"error": "Could not extract CSV from upload"}, 400)
                    return

                rows, sep = parse_csv(csv_content)
                if not rows:
                    self.send_json({"error": "CSV is empty or could not be parsed"}, 400)
                    return

                plants_data = group_by_species(rows)
                for p in plants_data:
                    p["selected"] = True

                with state_lock:
                    state["plants"] = plants_data

                self.send_json({
                    "plants":    plants_data,
                    "total_obs": len(rows),
                    "separator": "TAB" if sep == "\t" else sep,
                })

            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/load_test_csv":
            try:
                test_file_path = "sample-test.csv"
                if not os.path.exists(test_file_path):
                    self.send_json({"error": "sample-test.csv file not found in directory"}, 404)
                    return
                with open(test_file_path, "r", encoding="utf-8", errors="replace") as f:
                    csv_content = f.read()

                rows, sep = parse_csv(csv_content)
                if not rows:
                    self.send_json({"error": "Test CSV is empty or could not be parsed"}, 400)
                    return

                plants_data = group_by_species(rows)
                for p in plants_data:
                    p["selected"] = True

                with state_lock:
                    state["plants"] = plants_data

                self.send_json({
                    "plants":    plants_data,
                    "total_obs": len(rows),
                    "separator": "TAB" if sep == "\t" else sep,
                })
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/diff_upload":
            try:
                content_type = self.headers.get("Content-Type", "")
                csv_content  = extract_csv_from_multipart(body, content_type)
                if csv_content is None:
                    self.send_json({"error": "Could not extract CSV from upload"}, 400)
                    return
                rows, _sep = parse_csv(csv_content)
                if not rows:
                    self.send_json({"error": "CSV is empty or could not be parsed"}, 400)
                    return
                prev_plants = group_by_species(rows)
                prev_names  = {clean_scientific_name(p["scientific"]).lower() for p in prev_plants}
                with state_lock:
                    matched = [
                        p["scientific"] for p in state["plants"]
                        if clean_scientific_name(p["scientific"]).lower() in prev_names
                    ]
                self.send_json({"matched": matched, "total_prev_species": len(prev_names)})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/setup_notetype":
            ok, msg = create_notetype_via_ankiconnect()
            self.send_json({"ok": ok, "message": msg})

        elif path == "/stop":
            with state_lock:
                state["stopped"] = True
            self.send_json({"ok": True})

        elif path == "/review_reject":
            try:
                data     = json.loads(body)
                idx      = data.get("plant_index")
                rejected    = data.get("rejected", [])
                try_replace = data.get("replace", True)
                with state_lock:
                    plant = state["plants"][idx]
                    lang    = state.get("gen_lang", "en")
                    api_key = state.get("gen_api_key", "")
                    sci   = plant["scientific"]

                    # Each rejected entry is either a plain flat index (old
                    # format, "same source") or {"idx":..., "source":...} to
                    # request a different origin for the replacement.
                    resolved = []
                    for entry in rejected:
                        if isinstance(entry, dict):
                            flat_idx    = entry.get("idx")
                            want_source = entry.get("source") or "same"
                        else:
                            flat_idx, want_source = entry, "same"
                        resolved.append((flat_idx, None if want_source == "same" else want_source))

                    # Resolve all (key, pos) pairs BEFORE mutating anything,
                    # then process each key's removals highest-position-first
                    # so earlier positions in the same key stay valid.
                    targets = []
                    for flat_idx, want_source in resolved:
                        key, pos = locate_slot(plant.get("images", {}), flat_idx)
                        if key is not None:
                            targets.append((key, pos, want_source))
                    targets.sort(key=lambda kp: (kp[0], -kp[1]))

                    # Photos NOT being rejected in this batch — used as PlantNet
                    # reference photos instead of the (presumably bad) one
                    # being replaced, computed before any pops happen.
                    rejected_flat_idx = {flat_idx for flat_idx, _ in resolved}
                    all_slots_before  = collect_photo_slots(plant.get("images", {}), lang=lang)
                    good_refs = [s["url"] for i, s in enumerate(all_slots_before)
                                 if i not in rejected_flat_idx]

                    warnings = []
                    new_urls = []
                    pn_notes = []
                    for key, pos, want_source in targets:
                        w, new_url, pn_id = replace_rejected_photo(plant, sci, key, pos,
                                                             api_key=api_key,
                                                             try_replace=try_replace,
                                                             target_source=want_source,
                                                             good_refs=good_refs)
                        if w:
                            warnings.append(w)
                        if new_url:
                            new_urls.append(new_url)
                        if pn_id:
                            pn_notes.append(f"{sci}: PlantNet identified the reference photo as "
                                             f"{pn_id['name']} ({pn_id['score']}%)")

                    slots = collect_photo_slots(plant.get("images", {}), lang=lang)
                    # Indices of the freshly-added replacement photos within
                    # `slots` — the frontend uses these to show only the new
                    # photos on subsequent review passes.
                    replaced_indices = [i for i, s in enumerate(slots) if s["url"] in new_urls]
                self.send_json({"ok": True, "slots": slots, "warnings": warnings,
                                "replaced_indices": replaced_indices, "plantnet_id_notes": pn_notes})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/review_finish":
            global review_event
            with state_lock:
                state["review_pending"] = False
            review_event.set()
            self.send_json({"ok": True})

        elif path == "/generate":
            try:
                config = json.loads(body)
                # Update plant selection in state
                selected_names = {p["scientific"] for p in config["plants"]}
                with state_lock:
                    for p in state["plants"]:
                        p["selected"] = p["scientific"] in selected_names
                    plants_to_process = [p for p in state["plants"] if p["selected"]]

                log_cursor = 0  # reset log cursor for new run
                with state_lock:
                    state["stopped"]        = True  # stop any running thread
                    state["gen_id"]        += 1     # invalidate old thread
                    state["progress_label"] = ""    # clear stale label immediately
                    my_id = state["gen_id"]
                t = threading.Thread(
                    target=run_generation,
                    args=(config, my_id),
                    daemon=True
                )
                t.start()
                self.send_json({"ok": True, "count": len(plants_to_process)})

            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    print("=" * 52)
    print("  🌿 PlantNet → Anki  (local web interface)")
    print("=" * 52)
    print(f"\n  Server : http://localhost:{PORT}")
    print(f"  Stop   : Ctrl+C\n")

    server = HTTPServer(("127.0.0.1", PORT), Handler)

    # Open browser after a short delay
    def open_browser():
        time.sleep(0.8)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()