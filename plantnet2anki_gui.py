#!/usr/bin/env python3
"""
plantnet2anki_gui.py
====================
PlantNet → Anki with a local web interface.

Run:
    python plantnet2anki_gui.py

Then open http://localhost:7842 in your browser (opened automatically).

Requirements:
    pip install requests beautifulsoup4
"""

import csv
import io
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import requests
    from bs4 import BeautifulSoup
    import genanki
except ImportError as _missing:
    print(f"Missing dependency: {_missing}")
    print("Please run:")
    print("  pip install requests beautifulsoup4 genanki")
    sys.exit(1)

PORT = 7842

# ── Botanical constants ───────────────────────────────────────────────────────
TELA_BASE    = "https://api.tela-botanica.org/service:eflore:0.1"
PFAF_BASE    = "https://pfaf.org/user/plant.aspx"
PLANTNET_API = "https://my-api.plantnet.org/v2/identify/all"
HEADERS      = {"User-Agent": "plantnet2anki-gui/1.0"}

MONTHS_EN = ["January","February","March","April","May","June",
             "July","August","September","October","November","December"]
BIO_TYPE_MAP = {
    "Ph":"Phanerophyte (tree/shrub)", "Ch":"Chamaephyte (dwarf shrub)",
    "H":"Hemicryptophyte (perennial herb)", "G":"Geophyte (bulb/rhizome)",
    "Th":"Therophyte (annual)", "HH":"Helophyte (marsh plant)",
}
ORGAN_LABELS = {
    "en": {"flower":"Flower", "leaf":"Leaf",    "habit":"Habit",          "fruit":"Fruit", "bark":"Bark"},
    "fr": {"flower":"Fleur",  "leaf":"Feuille",  "habit":"Plante entière", "fruit":"Fruit", "bark":"Écorce"},
}

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


def group_by_species(rows):
    if not rows:
        return []
    headers = list(rows[0].keys())
    col_sci    = find_col(headers, "original name", "scientific name", "species", "nom_sci") or headers[min(4,len(headers)-1)]
    col_family = find_col(headers, "family", "famille")
    col_images = find_col(headers, "images", "image")
    col_date   = find_col(headers, "date observed", "date")

    by = {}
    for row in rows:
        sci = row.get(col_sci, "").strip()
        if not sci: continue
        if sci not in by:
            by[sci] = {"scientific": sci,
                       "family":     row.get(col_family, "") if col_family else "",
                       "own_images": [], "observations": 0, "last_date": ""}
        by[sci]["observations"] += 1
        if col_date:
            by[sci]["last_date"] = row.get(col_date, "") or by[sci]["last_date"]
        if col_images and row.get(col_images):
            for u in re.split(r"[\s|]+", row[col_images]):
                u = u.strip()
                if u.startswith("http"):
                    by[sci]["own_images"].append(u)
    return list(by.values())


# ── Tela Botanica ─────────────────────────────────────────────────────────────
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


def bdtfx_search(name, mode="exacte"):
    """Query BDTFX and return first matching taxon dict or None."""
    url = (f"{TELA_BASE}/bdtfx/taxons"
           f"?recherche={mode}&masque.ns={requests.utils.quote(name)}"
           f"&retour.champs=num_nom,nom_sci,nom_vernaculaire,famille"
           f"&navigation.limite=1&retour.format=json")
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    data    = r.json()
    entries = [v for v in data.values() if isinstance(v, dict) and v.get("num_nom")]
    return (entries[0] if entries else None), "ok"


def fetch_tela(sci, options):
    result = {}
    try:
        cleaned = clean_scientific_name(sci)
        log_extra = f" (cleaned: '{cleaned}')" if cleaned != sci else ""

        # 1. Try exact match on cleaned name
        taxon, st = bdtfx_search(cleaned, "exacte")

        # 2. Fallback: fuzzy search on cleaned name
        if taxon is None:
            taxon, st = bdtfx_search(cleaned, "floue")

        # 3. Fallback: fuzzy search on genus only
        if taxon is None:
            genus = cleaned.split()[0]
            taxon, st = bdtfx_search(genus, "floue")
            if taxon:
                log(f"    BDTFX: genus-only match ({genus}) → {taxon.get('nom_sci','?')}", "warn")

        if taxon is None:
            return result, f"not in BDTFX{log_extra}"

        num_nom = taxon["num_nom"]
        num_nom = taxon["num_nom"]
        # Always fetch vernacular name — used on the card regardless of options
        if taxon.get("nom_vernaculaire"):
            result["common_name"] = taxon["nom_vernaculaire"]
        if taxon.get("famille"):
            result["tb_family"] = taxon["famille"]

        r2 = requests.get(f"{TELA_BASE}/baseflor/taxons/{num_nom}?retour.format=json",
                          headers=HEADERS, timeout=10)
        if r2.status_code != 200:
            return result, f"BDTFX OK, Baseflor HTTP {r2.status_code}"
        bf = r2.json()
        if not isinstance(bf, dict):
            return result, "Baseflor: unexpected format"
        if "flowering" in options:
            d  = bf.get("mois_debut_floraison") or bf.get("mois-debut-floraison")
            f_ = bf.get("mois_fin_floraison")   or bf.get("mois-fin-floraison")
            if d and f_:
                try: result["flowering"] = f"{MONTHS_EN[int(d)-1]} – {MONTHS_EN[int(f_)-1]}"
                except: result["flowering"] = f"{d} – {f_}"
        if "perennial" in options:
            tb = bf.get("type_biologique") or bf.get("type-biologique", "")
            if tb:
                code = re.split(r"[,\s/]", tb)[0]
                result["perennial"] = BIO_TYPE_MAP.get(code, tb)
        if "habitat" in options:
            h = bf.get("syntaxon") or bf.get("habitat") or bf.get("milieu", "")
            if h: result["habitat"] = h[:200]
        if "description" in options:
            desc = bf.get("commentaire") or bf.get("description", "")
            if desc: result["description"] = desc[:300]

        found = [k for k in result if k != "tb_family"]
        return result, ("OK: " + ", ".join(found)) if found else f"OK (num_nom={num_nom}) — no Baseflor data"
    except Exception as e:
        return result, f"error: {e}"


# ── PFAF ──────────────────────────────────────────────────────────────────────
def fetch_pfaf(sci, options):
    result = {}
    if not ({"edible","medicinal","toxicity"} & set(options)):
        return result, "skipped"
    try:
        r = requests.get(f"{PFAF_BASE}?LatinName={sci.replace(' ','+')}", headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return result, f"HTTP {r.status_code}"
        if "Plant not found" in r.text or len(r.text) < 500:
            return result, "not in PFAF"
        soup = BeautifulSoup(r.text, "html.parser")
        def extract(label):
            for tag in soup.find_all(["h2","h3","td","th"]):
                if label.lower() in tag.get_text().lower():
                    nxt = tag.find_next_sibling()
                    if nxt: return nxt.get_text(" ", strip=True)[:300]
                    parent = tag.find_parent("tr")
                    if parent:
                        cells = parent.find_all("td")
                        if len(cells) > 1: return cells[-1].get_text(" ", strip=True)[:300]
            return ""
        if "edible"    in options:
            v = extract("Edible Uses");    result["edible"]    = v if len(v)>10 else ""
        if "medicinal" in options:
            v = extract("Medicinal Uses"); result["medicinal"] = v if len(v)>10 else ""
        if "toxicity"  in options:
            v = extract("Known Hazards"); result["toxicity"]  = v if len(v)>5  else ""
        result = {k: v for k, v in result.items() if v}
        found = list(result.keys())
        return result, ("OK: " + ", ".join(found)) if found else "page found but no data extracted"
    except Exception as e:
        return result, f"error: {e}"

def fetch_common_name(sci, api_key, lang="en"):
    """
    Fetch common name from GBIF only.
    Tries requested language first, falls back to English.
    """
    # Map lang codes to GBIF language codes
    lang_map = {
        "en": ("eng", "en"),
        "fr": ("fra", "fre", "fr"),
        "de": ("deu", "ger", "de"),
        "es": ("spa", "es"),
    }
    pref_codes = lang_map.get(lang, ("eng", "en"))
    en_codes   = lang_map["en"]

    try:
        r = requests.get(f"{GBIF_API}/species/match",
                         params={"name": sci, "verbose": "false"},
                         headers=HEADERS, timeout=8)
        if not r.ok:
            return "", "GBIF match failed"
        key = r.json().get("usageKey") or r.json().get("speciesKey")
        if not key:
            return "", "not found in GBIF"

        r2 = requests.get(f"{GBIF_API}/species/{key}/vernacularNames",
                          params={"limit": 100}, headers=HEADERS, timeout=8)
        if not r2.ok:
            return "", f"GBIF vernacular HTTP {r2.status_code}"

        names = r2.json().get("results", [])

        # Pass 1: preferred language
        for n in names:
            if n.get("language", "").lower() in pref_codes:
                return n.get("vernacularName", ""), f"GBIF ({lang})"

        # Pass 2: English fallback
        if lang != "en":
            for n in names:
                if n.get("language", "").lower() in en_codes:
                    return n.get("vernacularName", ""), "GBIF (en fallback)"

        return "", "no vernacular name in GBIF"

    except Exception as e:
        return "", f"GBIF error: {e}"
# ── PlantNet photos ───────────────────────────────────────────────────────────
# ── Photo sources ────────────────────────────────────────────────────────────
#
# Storage model:
#   plant["images"] = {
#       "flower": ["url1", "url2"],   # organ-tagged (from PlantNet)
#       "leaf":   ["url3"],
#       "untagged": ["url4", "url5"], # from GBIF/iNat — no organ info
#   }
#
# PlantNet related images: 1 API call per plant → already organ-tagged.
# GBIF / iNaturalist: free, no organ tags → stored under "untagged".

GBIF_API = "https://api.gbif.org/v1"
INAT_API = "https://api.inaturalist.org/v1"


def collect_plantnet_by_organ(plant, api_key, organs, n_per_organ):
    """
    Submit own image to PlantNet with include-related-images=true.
    Returns {organ: [url, ...]} — already organ-tagged, 1 API call only.
    """
    result = {o: [] for o in organs}
    if not plant.get("own_images") or not api_key:
        return result, "skipped (need own image + API key)"
    try:
        img_r = requests.get(plant["own_images"][0], timeout=15)
        if not img_r.ok:
            return result, f"could not fetch own image HTTP {img_r.status_code}"
        r = requests.post(
            PLANTNET_API,
            files=[("images", ("plant.jpg", img_r.content, "image/jpeg"))],
            data={"organs": ["auto"]},
            params={"include-related-images": "true", "no-reject": "true",
                    "lang": "en", "api-key": api_key},
            headers=HEADERS, timeout=20
        )
        if not r.ok:
            return result, f"API HTTP {r.status_code}: {r.text[:60]}"
        related = r.json().get("results", [{}])[0].get("images", [])
        log(f"    PlantNet: {len(related)} related image(s) returned")
        for img in related:
            organ = img.get("organ")
            if organ not in organs:
                continue
            if len(result[organ]) >= n_per_organ:
                continue
            u = (img.get("url") or {}).get("m") or (img.get("url") or {}).get("s", "")
            if u:
                result[organ].append(u)
        filled   = {o: len(v) for o, v in result.items() if v}
        return result, f"OK — {filled}"
    except Exception as e:
        return result, f"error: {e}"


def collect_gbif_urls(sci, n):
    """Fetch up to n photo URLs from GBIF occurrences (no organ tag)."""
    try:
        r = requests.get(f"{GBIF_API}/species/match",
                         params={"name": sci, "verbose": "false"},
                         headers=HEADERS, timeout=8)
        if not r.ok:
            return [], f"species match HTTP {r.status_code}"
        key = r.json().get("usageKey") or r.json().get("speciesKey")
        if not key:
            return [], "not found in GBIF"
        r2 = requests.get(f"{GBIF_API}/occurrence/search",
                          params={"taxonKey": key, "mediaType": "StillImage", "limit": n * 2},
                          headers=HEADERS, timeout=10)
        if not r2.ok:
            return [], f"occurrences HTTP {r2.status_code}"
        urls = []
        for occ in r2.json().get("results", []):
            for m in occ.get("media", []):
                u = m.get("identifier", "")
                if u and u.startswith("http") and u not in urls:
                    urls.append(u)
        return urls[:n], f"{len(urls)} found"
    except Exception as e:
        return [], f"error: {e}"


def collect_inat_urls(sci, n):
    """Fetch up to n research-grade photo URLs from iNaturalist (no organ tag)."""
    try:
        r = requests.get(f"{INAT_API}/observations",
                         params={"taxon_name": sci, "quality_grade": "research",
                                 "photos": "true", "per_page": n * 2,
                                 "order_by": "votes", "order": "desc"},
                         headers=HEADERS, timeout=12)
        if not r.ok:
            return [], f"HTTP {r.status_code}"
        urls = []
        for obs in r.json().get("results", []):
            for photo in obs.get("photos", []):
                u = photo.get("url", "").replace("square", "medium")
                if u and u not in urls:
                    urls.append(u)
        return urls[:n], f"{len(urls)} found"
    except Exception as e:
        return [], f"error: {e}"


def fetch_photos_for_plant(plant, sci, organs, n_per_organ, api_key, source, max_untagged=None):
    """
    Main photo fetching function.
    Returns plant["images"] dict:
      - organ-tagged photos from PlantNet (if api_key provided)
      - untagged pool from GBIF/iNat for remaining slots

    source: "all" | "plantnet" | "gbif" | "inat" | "none"
    """
    images     = {o: [] for o in organs}
    images["untagged"] = []
    seen       = set()

    if source == "none" or n_per_organ == 0:
        return images, "disabled"

    use_pn    = source in ("all", "plantnet") and api_key and plant.get("own_images")
    use_gbif  = source in ("all", "gbif")
    use_inat  = source in ("all", "inat")
    summary   = []

    # ── 1. PlantNet related images (organ-tagged, 1 call) ─────────────────────
    if use_pn:
        pn_result, pn_status = collect_plantnet_by_organ(plant, api_key, organs, n_per_organ)
        log(f"    PlantNet related: {pn_status}")
        for organ in organs:
            for u in pn_result.get(organ, []):
                if u not in seen:
                    images[organ].append(u)
                    seen.add(u)
        pn_total = sum(len(images[o]) for o in organs)
        summary.append(f"PlantNet:{pn_total}")

    # ── 2. Compute missing slots ──────────────────────────────────────────────
    missing = sum(max(0, n_per_organ - len(images[o])) for o in organs)
    # Cap untagged photos to max_untagged if specified
    untagged_budget = min(missing, max_untagged) if max_untagged is not None else missing

    # ── 3. GBIF — fill untagged pool ─────────────────────────────────────────
    if use_gbif and untagged_budget > 0:
        log("    Collecting from GBIF…")
        gbif_urls, gbif_st = collect_gbif_urls(sci, untagged_budget * 3)
        log(f"    GBIF: {gbif_st}")
        added = 0
        for u in gbif_urls:
            if u not in seen and added < untagged_budget:
                images["untagged"].append(u)
                seen.add(u)
                added += 1
        summary.append(f"GBIF:{added}")
        untagged_budget -= added

    # ── 4. iNaturalist — fill remaining untagged pool ─────────────────────────
    if use_inat and untagged_budget > 0:
        log("    Collecting from iNaturalist…")
        inat_urls, inat_st = collect_inat_urls(sci, untagged_budget * 3)
        log(f"    iNat: {inat_st}")
        added = 0
        for u in inat_urls:
            if u not in seen and added < untagged_budget:
                images["untagged"].append(u)
                seen.add(u)
                added += 1
        summary.append(f"iNat:{added}")

    # ── Summary ───────────────────────────────────────────────────────────────
    tagged   = {o: len(images[o]) for o in organs if images[o]}
    untagged = len(images["untagged"])
    status   = " | ".join(summary) + f" → tagged:{tagged} untagged:{untagged}"
    return images, status

# ── Anki builder (genanki → Basic + JS random photo) ─────────────────────────
#
# Architecture:
#   - One NOTE per species → one CARD per species
#   - Field PhotosJSON: JSON array [{url, organ}, ...] of all available photos
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

  var organ = imgs[idx].getAttribute('data-organ') || '';
  var organEl = document.getElementById('pn-organ');
  if (organEl) organEl.textContent = organ;
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
    """Return list of {url, organ} dicts for all available photos."""
    labels = ORGAN_LABELS.get(lang, ORGAN_LABELS["en"])
    slots = []
    order = list(ORGAN_LABELS["en"].keys()) + ["own", "untagged"]
    for key in order:
        for url in images.get(key, []):
            if len(slots) >= MAX_PHOTO_SLOTS:
                return slots
            slots.append({"url": url, "organ": labels.get(key, "")})
    return slots


def build_info_html(plant, lang="en"):
    i = plant.get("info", {})
    rows = []
    fam = plant.get("family") or i.get("tb_family", "")
    if lang == "fr":
        if fam:               rows.append(f"<b>Famille</b> : {fam}")
        if i.get("flowering"):    rows.append(f"<b>Floraison</b> : {i['flowering']} <small style='color:#aaa'>(Tela Botanica)</small>")
        if i.get("perennial"):    rows.append(f"<b>Type bio.</b> : {i['perennial']} <small style='color:#aaa'>(Tela Botanica)</small>")
        if i.get("habitat"):      rows.append(f"<b>Habitat</b> : {i['habitat']} <small style='color:#aaa'>(Tela Botanica)</small>")
        if i.get("edible"):       rows.append(f"<b>Comestible</b> : {i['edible']} <small style='color:#aaa'>(PFAF)</small>")
        if i.get("medicinal"):    rows.append(f"<b>Médicinal</b> : {i['medicinal']} <small style='color:#aaa'>(PFAF)</small>")
        if i.get("toxicity"):     rows.append(f"<b>Toxicité</b> : {i['toxicity']} <small style='color:#aaa'>(PFAF)</small>")
    else:
        if fam:               rows.append(f"<b>Family</b>: {fam}")
        if i.get("flowering"):    rows.append(f"<b>Flowering</b>: {i['flowering']} <small style='color:#aaa'>(Tela Botanica)</small>")
        if i.get("perennial"):    rows.append(f"<b>Bio. type</b>: {i['perennial']} <small style='color:#aaa'>(Tela Botanica)</small>")
        if i.get("habitat"):      rows.append(f"<b>Habitat</b>: {i['habitat']} <small style='color:#aaa'>(Tela Botanica)</small>")
        if i.get("edible"):       rows.append(f"<b>Edible</b>: {i['edible']} <small style='color:#aaa'>(PFAF)</small>")
        if i.get("medicinal"):    rows.append(f"<b>Medicinal</b>: {i['medicinal']} <small style='color:#aaa'>(PFAF)</small>")
        if i.get("toxicity"):     rows.append(f"<b>Toxicity</b>: {i['toxicity']} <small style='color:#aaa'>(PFAF)</small>")
    return "<br>".join(rows)


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
            f'<img src="{s["url"]}" class="pn-img" data-organ="{s["organ"]}"'
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
    n_per_organ  = int(config.get("n_photos", 2))
    max_untagged = config.get("max_untagged")
    max_untagged = int(max_untagged) if max_untagged is not None else None
    organs       = config.get("organs", ["flower", "leaf", "habit"])
    photo_source = config.get("photo_source", "all")
    include_own  = config.get("include_own", True)

    # Reset per-plant data so previous runs don't bleed through
    for p in plants:
        p.pop("info",    None)
        p.pop("images",  None)
        p.pop("_slots",  None)

    log(f"Starting — {len(plants)} species | organs: {organs} | {n_per_organ}/organ | own photos: {include_own} | source: {photo_source}")

    for i, plant in enumerate(plants):
        if should_stop():
            break
        sci = plant["scientific"]
        pct = int(5 + (i / len(plants)) * 85)
        set_progress(pct)
        log(f"[{i+1}/{len(plants)}] {sci}")
        plant["info"]   = {}
        plant["images"] = {}

        # Own photos (only if include_own is enabled)
        if include_own and plant["own_images"]:
            plant["images"]["own"] = plant["own_images"][:4]
            log(f"  📷 {len(plant['images']['own'])} own photo(s)", "ok")

        # Organ-tagged + untagged photos from external sources
        if n_per_organ > 0:
            extra_images, status = fetch_photos_for_plant(
                plant, sci, organs, n_per_organ, api_key, photo_source,
                max_untagged=max_untagged
            )
            log(f"  Photos: {status}", "ok" if any(extra_images.values()) else "warn")
            plant["images"].update(extra_images)

        # Common name: PlantNet API (fr→en) then Wikipedia (fr→en)
        lang      = config.get("lang", "en")
        log(f"  Lang: {lang}", "ok")
        cn, cn_status = fetch_common_name(sci, api_key, lang=lang)
        if cn:
            plant["info"]["common_name"] = cn
            log(f"  Common name: {cn} ({cn_status})", "ok")
        else:
            log(f"  Common name: {cn_status}", "warn")

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
                log(f"  ✓ {idx+1}/{total_imgs}  {size_kb:.0f} KB  {slot['organ'] or 'untagged'}", "ok")
            except Exception as ex:
                log(f"  ⚠ {url[:50]}: {ex}", "warn")
                url_to_fname[url] = None

        total_mb = downloaded_bytes / (1024 * 1024)
        log(f"Downloaded {len(media_files)}/{total_imgs} images — {total_mb:.1f} MB total", "ok")

        # Rewrite each plant's slots using local filenames
        for p in plants:
            slots = collect_photo_slots(p.get("images", {}))
            p["_slots"] = [
                {"url": url_to_fname[s["url"]] or s["url"], "organ": s["organ"]}
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
                    "new_logs":       new_logs,
                    "all_logs":       all_logs,
                }
            self.send_json(data)

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

        elif path == "/setup_notetype":
            ok, msg = create_notetype_via_ankiconnect()
            self.send_json({"ok": ok, "message": msg})

        elif path == "/stop":
            with state_lock:
                state["stopped"] = True
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