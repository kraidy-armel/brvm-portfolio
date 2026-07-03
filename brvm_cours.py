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

NOUVEAU : le robot envoie aussi une notification push sur ton telephone
(via ntfy.sh) quand un titre bouge fortement - voir le bloc NOTIFICATIONS
plus bas. Maximum une notification par jour.

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

# Le serveur brvm.org sert une chaine de certificat incomplete : requests refuse
# alors la connexion ("CERTIFICATE_VERIFY_FAILED"). Pour lire des cours publics,
# on s'autorise un repli sans verification du certificat (lecture seule, aucune
# donnee envoyee). On coupe juste l'avertissement correspondant.
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass


def http_get(url, timeout=30):
    """GET tolerant au certificat mal configure de brvm.org."""
    try:
        return SESSION.get(url, timeout=timeout)
    except requests.exceptions.SSLError:
        return SESSION.get(url, timeout=timeout, verify=False)

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
            r = http_get(url, timeout=30)
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


# ==========================================================================
# NOTIFICATIONS PUSH (ntfy.sh)
# ==========================================================================
# Le robot garde un petit historique des cours dans cours.json (bloc "_histo",
# ignore par l'appli) et t'envoie une notification sur ton telephone quand un
# titre sort de l'ordinaire :
#   - variation de +/- SEUIL_7J % sur 7 jours (tendance), ou
#   - variation de +/- SEUIL_1J % sur la journee (gros mouvement).
# Maximum UNE notification par jour (un "digest"), pour ne pas te spammer.
#
# Pour recevoir les notifications : installe l'appli gratuite "ntfy" sur ton
# telephone et abonne-toi au sujet NTFY_TOPIC ci-dessous (voir le guide).
# Pour couper les notifications : mets NTFY_TOPIC = ""  (chaine vide).

NTFY_TOPIC = "brvm-kraidy-sxoyc6rst1"   # ton canal secret - ne le partage pas
SEUIL_7J = 5.0                           # seuil en % sur 7 jours
SEUIL_1J = 4.0                           # seuil en % sur 1 jour
HISTO_MAX_POINTS = 20                    # points de cours gardes par titre


def notifier(merged, ancien_histo):
    """Historise les cours et envoie au besoin la notification du jour.
    Renvoie le bloc _histo a ecrire dans cours.json."""
    today = datetime.date.today().isoformat()
    state = {"points": {}, "derniere_notif": ""}
    if isinstance(ancien_histo, dict):
        state["points"] = ancien_histo.get("points", {}) or {}
        state["derniere_notif"] = ancien_histo.get("derniere_notif", "") or ""
    points = state["points"]

    # 1) Historise le cours du jour (1 point par date de cours)
    for sym, row in merged.items():
        cur = row.get("actuel")
        if not cur or cur <= 0:
            continue
        d = (row.get("date") or today)[:10]
        h = points.setdefault(sym, {})
        h[d] = cur
        for old in sorted(h)[:-HISTO_MAX_POINTS]:
            del h[old]

    if not NTFY_TOPIC:
        return state

    # 2) Detection des mouvements
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    hits7, hits1 = [], []
    for sym, row in merged.items():
        cur = row.get("actuel") or 0
        if not cur or cur <= 0:
            continue
        prev = row.get("veille") or cur
        if prev > 0:
            v1 = (cur - prev) / prev * 100
            if abs(v1) >= SEUIL_1J:
                hits1.append((sym, v1, cur))
        h = points.get(sym, {})
        refs = sorted(d for d in h if d <= cutoff)
        if refs:
            ref = h[refs[-1]]
            if ref and ref > 0:
                v7 = (cur - ref) / ref * 100
                if abs(v7) >= SEUIL_7J:
                    hits7.append((sym, v7, cur))

    # 3) Une seule notification par jour maximum
    if state["derniere_notif"] == today:
        return state

    def fcfa(x):
        return f"{x:,.0f}".replace(",", " ")

    lines, vus = [], set()
    for sym, v, cur in sorted(hits7, key=lambda x: -abs(x[1])):
        vus.add(sym)
        ico = "\U0001F4C8" if v > 0 else "\U0001F4C9"
        lines.append(f"{ico} {sym} {v:+.1f}% sur 7 jours ({fcfa(cur)} F)")
    for sym, v, cur in sorted(hits1, key=lambda x: -abs(x[1])):
        if sym in vus:
            continue
        ico = "▲" if v > 0 else "▼"
        lines.append(f"{ico} {sym} {v:+.1f}% aujourd'hui ({fcfa(cur)} F)")

    if not lines:
        return state

    msg = "\n".join(lines[:8])
    if len(lines) > 8:
        msg += f"\n... et {len(lines) - 8} autre(s) titre(s)"
    msg += "\n\nOuvre l'appli pour voir et agir. A toi de decider."
    try:
        r = requests.post(
            "https://ntfy.sh/" + NTFY_TOPIC,
            data=msg.encode("utf-8"),
            headers={"Title": "BRVM - mouvements a surveiller",
                     "Priority": "default",
                     "Tags": "bell"},
            timeout=20)
        if r.status_code == 200:
            state["derniere_notif"] = today
            print(f"  notification ntfy envoyee ({min(len(lines), 8)} titre(s))")
        else:
            print(f"  ntfy a repondu HTTP {r.status_code} (on reessaiera)")
    except Exception as e:
        print(f"  notification ntfy impossible : {e}")
    return state


# --------------------------------------------------------------------------
# Programme principal : fusion des sources, ecriture de cours.json
# --------------------------------------------------------------------------
def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    diag = {}

    # Historique precedent (garde dans cours.json, bloc "_histo")
    ancien_histo = {}
    try:
        with open("cours.json", encoding="utf-8") as f:
            ancien_histo = (json.load(f) or {}).get("_histo", {})
    except Exception:
        pass

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

    # Notifications + historique (avant l'ajout des codes alternatifs,
    # pour ne pas signaler deux fois le meme titre)
    histo = notifier(merged, ancien_histo)

    # Codes alternatifs : certains portefeuilles utilisent un code different du
    # code officiel. On duplique l'entree sous l'autre code pour que l'appli
    # retrouve le titre quoi qu'il arrive. (cle = code alternatif, valeur = code officiel)
    ALIASES = {"SGBCI": "SGBC"}
    for alt, official_code in ALIASES.items():
        if official_code in merged and alt not in merged:
            merged[alt] = dict(merged[official_code])

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
    out["_histo"] = histo

    with open("cours.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("=== Robot cours BRVM ===")
    for k, v in diag.items():
        print(f"  {k}: {v}")
    print(f"  -> cours.json ecrit ({len(merged)} titres)")


if __name__ == "__main__":
    main()
