#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robot de collecte des cours BRVM -> cours.json

Objectif : produire un fichier cours.json contenant, pour chaque action cotee,
le dernier cours connu + le cours de la veille + la date du cours.

Strategie en cascade (du plus frais au plus fiable) :
  1. Site officiel BRVM (www.brvm.org) : scraping HTML generique.
     -> si JavaScript bloque la lecture, cette etape ne ramene rien : ce n'est
        pas grave, on passe a l'etape 2.
  2. Depot public communautaire (raw.githubusercontent / Fredysessie) :
     fichiers CSV par ticker. Donne le dernier cours de cloture DATE.

Chaque cours est estampille avec sa source et sa date, pour que l'appli affiche
clairement si une donnee est fraiche ou perimee. Le fichier contient aussi un
bloc "_meta" de diagnostic (ce qui a marche / echoue), visible en cas de
probleme.

Aucune cle API, aucun service payant. Tourne dans GitHub Actions.
"""

import json
import sys
import datetime
import re

try:
    import requests
except ImportError:
    print("Le module 'requests' est requis (pip install requests).")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})

# Liste des tickers BRVM connus (pour la source de secours CSV).
# Si un ticker n'existe pas dans la source, il est simplement ignore.
TICKERS = [
    "ABJC", "BICC", "BICB", "BNBC", "BOAB", "BOABF", "BOAC", "BOAM", "BOAN",
    "BOAS", "CABC", "CBIBF", "CFAC", "CIEC", "ECOC", "ETIT", "FTSC", "NEIC",
    "NSBC", "NTLC", "ONTBF", "ORAC", "ORGT", "PALC", "PRSC", "SAFC", "SCRC",
    "SDCC", "SDSC", "SEMC", "SGBC", "SHEC", "SIBC", "SICC", "SIVC", "SLBC",
    "SMBC", "SNTS", "SOGC", "SPHC", "STAC", "STBC", "SVOC", "TTLC", "TTLS",
    "UNLC", "UNXC",
]

# URLs candidates du site officiel (on essaie chacune jusqu'a en trouver une qui
# renvoie un tableau de cours exploitable).
BRVM_URLS = [
    "https://www.brvm.org/fr/cours-actions/0",
    "https://www.brvm.org/fr/cours-actions/liste",
    "https://www.brvm.org/en/cours-actions/0",
]

FREDY_BASE = ("https://raw.githubusercontent.com/Fredysessie/"
              "brvm-data-public/main/data")


def to_number_fr(text):
    """Format francais d'un tableau web : '16 000,00' -> 16000, '1.234' -> 1234,
    '12,5' -> 12.5. (virgule = decimale, espace/point = milliers)."""
    if text is None:
        return None
    t = str(text).replace("\xa0", " ").strip().replace(" ", "")
    if t in ("", "-", "ND", "N/D", "n/a"):
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")   # 16.000,00 -> 16000.00
    else:
        t = t.replace(".", "")                      # 16.000 -> 16000 (milliers)
    t = re.sub(r"[^0-9.\-]", "", t)
    try:
        return float(t)
    except ValueError:
        return None


def to_number_csv(text):
    """Format machine d'un CSV : '16000.0' -> 16000.0 (point = decimale)."""
    if text is None:
        return None
    t = str(text).strip()
    if t in ("", "-", "ND"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Source 1 : site officiel BRVM (scraping HTML generique)
# --------------------------------------------------------------------------
def scrape_brvm_official():
    if not HAS_BS4:
        return {}, "bs4 absent"
    last_err = "aucune URL exploitable"
    for url in BRVM_URLS:
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code != 200 or not r.text:
                last_err = f"{url} -> HTTP {r.status_code}"
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            data = parse_any_price_table(soup)
            if len(data) >= 5:
                return data, f"OK ({url}, {len(data)} titres)"
            last_err = f"{url} -> tableau de cours introuvable (JS ?)"
        except Exception as e:  # noqa
            last_err = f"{url} -> {e}"
    return {}, last_err


def parse_any_price_table(soup):
    """Parcourt toutes les tables, repere celle des cours via ses en-tetes,
    et en extrait {symbole: {actuel, veille, date, source}}."""
    today = datetime.date.today().isoformat()
    best = {}
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower()
                   for th in table.find_all("th")]
        if not headers:
            first = table.find("tr")
            if first:
                headers = [td.get_text(" ", strip=True).lower()
                           for td in first.find_all(["td", "th"])]
        htxt = " ".join(headers)
        if not any(k in htxt for k in ("cours", "clôture", "cloture", "veille",
                                       "précédent", "precedent")):
            continue

        def col(*keys):
            # priorite a l'ORDRE des mots-cles (le 1er trouve gagne)
            for k in keys:
                for i, h in enumerate(headers):
                    if k in h:
                        return i
            return None

        i_sym = col("symbole", "ticker", "code")
        i_cur = col("clôture", "cloture", "cours du jour", "dernier cours",
                    "dernier", "cours")
        i_prev = col("veille", "précédent", "precedent", "ouverture")
        if i_prev == i_cur:
            i_prev = None

        rows = table.find_all("tr")
        local = {}
        for tr in rows:
            cells = [td.get_text(" ", strip=True)
                     for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            sym = None
            if i_sym is not None and i_sym < len(cells):
                sym = cells[i_sym].strip().upper()
            if not sym or not re.fullmatch(r"[A-Z0-9]{3,6}", sym):
                # tente de detecter un ticker connu dans la 1re cellule
                cand = cells[0].strip().upper()
                sym = cand if cand in TICKERS else None
            if not sym:
                continue
            cur = to_number_fr(cells[i_cur]) if (i_cur is not None
                                                 and i_cur < len(cells)) else None
            prev = to_number_fr(cells[i_prev]) if (i_prev is not None
                                                   and i_prev < len(cells)) else None
            if cur and cur > 0:
                local[sym] = {
                    "actuel": cur,
                    "veille": prev if (prev and prev > 0) else cur,
                    "date": today,
                    "source": "brvm.org",
                }
        if len(local) > len(best):
            best = local
    return best


# --------------------------------------------------------------------------
# Source 2 : depot public communautaire (CSV par ticker) - secours
# --------------------------------------------------------------------------
def fetch_fredy_csv(ticker):
    url = f"{FREDY_BASE}/{ticker}/{ticker}.daily.csv"
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code != 200 or not r.text:
            return None
        lines = [l for l in r.text.strip().splitlines()
                 if l and not l.lower().startswith("date")]
        if not lines:
            return None
        last = lines[-1].split(",")
        prev = lines[-2].split(",") if len(lines) >= 2 else last
        # colonnes : Date, Open, High, Low, Close, Volume
        close = to_number_csv(last[4]) if len(last) > 4 else None
        pclose = to_number_csv(prev[4]) if len(prev) > 4 else close
        date = last[0].strip() if last else ""
        if close and close > 0:
            return {"actuel": close,
                    "veille": pclose if (pclose and pclose > 0) else close,
                    "date": date,
                    "source": "github/Fredysessie"}
    except Exception:
        return None
    return None


def scrape_fredy():
    data = {}
    for t in TICKERS:
        row = fetch_fredy_csv(t)
        if row:
            data[t] = row
    return data


# --------------------------------------------------------------------------
# Programme principal : fusion des sources, ecriture de cours.json
# --------------------------------------------------------------------------
def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    diag = {}

    official, msg1 = scrape_brvm_official()
    diag["brvm_officiel"] = msg1

    fredy = {}
    if len(official) < 5:
        fredy = scrape_fredy()
        diag["github_secours"] = f"{len(fredy)} titres"
    else:
        diag["github_secours"] = "non utilise (officiel suffisant)"

    # Fusion : on prefere la donnee la plus recente par ticker.
    merged = {}
    for src in (fredy, official):  # official ecrase fredy si present (plus frais)
        for sym, row in src.items():
            merged[sym] = row

    if not merged:
        diag["resultat"] = "AUCUNE donnee recuperee - sources indisponibles"
    else:
        dates = sorted({r.get("date", "") for r in merged.values() if r.get("date")})
        diag["resultat"] = f"{len(merged)} titres"
        diag["dates_couvertes"] = (dates[0] + " -> " + dates[-1]) if dates else "?"

    human = (f"{now.day:02d}/{now.month:02d}/{now.year} "
             f"{now.hour:02d}:{now.minute:02d} UTC")
    out = {"_meta": {
        "updated": now.isoformat(timespec="seconds"),
        "updated_human": human,
        "count": len(merged),
        "diagnostic": diag,
    }}
    out.update(merged)

    with open("cours.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("=== Robot cours BRVM ===")
    for k, v in diag.items():
        print(f"  {k}: {v}")
    print(f"  -> cours.json ecrit ({len(merged)} titres)")


if __name__ == "__main__":
    main()
