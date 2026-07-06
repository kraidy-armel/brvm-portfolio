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
        i_var = col("variation")
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
            # La colonne « Cours veille » du site est PARFOIS fausse (elle repete
            # le cours du jour), alors que la colonne « Variation (%) » est fiable.
            # On reconstruit donc la veille a partir de la variation officielle :
            # veille = cloture / (1 + variation/100). Repli : colonne veille.
            prev = None
            if cur and cur > 0 and i_var is not None and i_var < len(cells):
                v = to_number_fr(cells[i_var])
                if v is not None and abs(v) < 50 and (1 + v / 100.0) > 0:
                    prev = round(cur / (1 + v / 100.0))
            if not prev or prev <= 0:
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
# Indice BRVM Composite (pour comparer le portefeuille au marche)
# --------------------------------------------------------------------------
INDICE_URLS = [
    "https://www.brvm.org/fr",
    "https://www.brvm.org/fr/indices/0",
    "https://www.brvm.org/en",
]


ANNONCE_PAGES = [
    ("https://www.brvm.org/fr/emetteurs/type-annonces/convocations-assemblees-generales", "ago"),
    ("https://www.brvm.org/fr/emetteurs/type-annonces/communiques", "communique"),
]


def scrape_annonces():
    """Lit les dernieres annonces emetteurs (AG, dividendes, resultats) sur
    brvm.org. L'appli les transforme en evenements d'agenda pour les titres
    que l'utilisateur suit. Renvoie une liste (max 40)."""
    out = []
    if not HAS_BS4:
        return out
    for url, typ in ANNONCE_PAGES:
        try:
            r = http_get(url, timeout=30)
            if r.status_code != 200 or not r.text:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for table in soup.find_all("table"):
                heads = " ".join(th.get_text(" ", strip=True).lower()
                                 for th in table.find_all("th"))
                if "annonce" not in heads:
                    continue
                for tr in table.find_all("tr"):
                    tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                    if len(tds) < 3:
                        continue
                    dm = re.match(r"(\d{2})/(\d{2})/(\d{4})", tds[0].strip())
                    if not dm:
                        continue
                    date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
                    societe, titre = tds[1].strip(), tds[2].strip()
                    t = typ
                    low = titre.lower()
                    if typ == "communique":
                        if "dividende" in low:
                            t = "dividende"
                        elif "sultat" in low:      # resultat(s), avec ou sans accent
                            t = "resultats"
                        else:
                            continue
                    out.append({"date": date, "societe": societe,
                                "titre": titre[:120], "type": t})
        except Exception:
            pass
    return out[:40]


MOIS_FR = {"janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
           "mai": 5, "juin": 6, "juillet": 7, "aout": 8, "août": 8,
           "septembre": 9, "octobre": 10, "novembre": 11,
           "decembre": 12, "décembre": 12}


def parse_date_fr(s):
    """'30 septembre 2026' ou '30/09/2026' -> '2026-09-30' (sinon None)."""
    s = (s or "").strip().lower()
    m = re.match(r"(\d{1,2})(?:er)?\s+([a-zéûà]+)\s+(\d{4})", s)
    if m and m.group(2) in MOIS_FR:
        return f"{m.group(3)}-{MOIS_FR[m.group(2)]:02d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def scrape_dividendes():
    """Page officielle « Paiement de dividendes » : emetteur, date ex-dividende,
    date de paiement, montant net. C'est LA source fiable pour l'agenda."""
    out = []
    if not HAS_BS4:
        return out
    try:
        r = http_get("https://www.brvm.org/fr/esv/paiement-de-dividendes", timeout=30)
        if r.status_code != 200 or not r.text:
            return out
        soup = BeautifulSoup(r.text, "html.parser")
        for table in soup.find_all("table"):
            ths = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
            heads = " ".join(ths)
            if "dividende" not in heads or "emetteur" not in heads:
                continue

            def col(k):
                for i, h in enumerate(ths):
                    if k in h:
                        return i
                return None

            i_em, i_pay = col("emetteur"), col("paiement")
            i_ex, i_mt = col("ex-dividende"), col("montant")
            for tr in table.find_all("tr"):
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) < 5:
                    continue
                em = tds[i_em] if (i_em is not None and i_em < len(tds)) else ""
                if not em:
                    continue
                dex = parse_date_fr(tds[i_ex]) if (i_ex is not None and i_ex < len(tds)) else None
                dpay = parse_date_fr(tds[i_pay]) if (i_pay is not None and i_pay < len(tds)) else None
                d = dex or dpay
                if not d:
                    continue
                mt = tds[i_mt] if (i_mt is not None and i_mt < len(tds)) else ""

                def jm(x):
                    return (x[8:10] + "/" + x[5:7]) if x else "?"
                # dates volontairement en JJ/MM (sans annee) dans le titre :
                # l'appli garde ainsi la date ex-dividende comme date d'evenement
                titre = f"Dividende net {mt}/action - detachement {jm(dex)}, paiement {jm(dpay)}"
                out.append({"date": d, "societe": em,
                            "titre": titre[:120], "type": "dividende"})
    except Exception:
        pass
    return out[:20]


def scrape_indice():
    """Cherche la valeur de l'indice BRVM Composite sur le site officiel.
    Renvoie (valeur, source) ou (None, message)."""
    last = "introuvable"
    for url in INDICE_URLS:
        try:
            r = http_get(url, timeout=30)
            if r.status_code != 200 or not r.text:
                last = f"{url} -> HTTP {r.status_code}"
                continue
            # texte brut sans balises, pour un motif robuste
            txt = re.sub(r"<[^>]+>", " ", r.text)
            m = re.search(
                r"BRVM[\s\-]*C(?:omposite|OMPOSITE)\D{0,40}?(\d{2,3}(?:[.,]\d{1,2})?)",
                txt)
            if m:
                val = to_number_fr(m.group(1))
                if val and 50 < val < 2000:   # garde-fou de vraisemblance
                    return val, url
            last = f"{url} -> motif non trouve"
        except Exception as e:  # noqa
            last = f"{url} -> {e}"
    return None, last


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
    #    -> jamais les samedis/dimanches : marche ferme, ce serait un point plat
    for sym, row in merged.items():
        cur = row.get("actuel")
        if not cur or cur <= 0:
            continue
        d = (row.get("date") or today)[:10]
        try:
            if datetime.date.fromisoformat(d).weekday() >= 5:
                continue
        except ValueError:
            pass
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

    # Indice BRVM Composite (l'appli le recupere automatiquement a la synchro)
    idx, idx_src = scrape_indice()
    if idx:
        out["_indice"] = {"composite": idx,
                          "date": datetime.date.today().isoformat(),
                          "source": idx_src}
        diag["indice_composite"] = f"{idx} ({idx_src})"
    else:
        # site injoignable : on ressert la derniere valeur connue (marquee du jour
        # de sa collecte) plutot que de perdre l'info
        if isinstance(ancien_histo, dict) and ancien_histo.get("_indice_prec"):
            out["_indice"] = ancien_histo["_indice_prec"]
        diag["indice_composite"] = f"non recupere ({idx_src})"
    if out.get("_indice"):
        histo["_indice_prec"] = out["_indice"]

    # Annonces emetteurs (AG, resultats) + calendrier officiel des dividendes
    ann = (scrape_dividendes() + scrape_annonces())[:50]
    if ann:
        out["_annonces"] = ann
        diag["annonces"] = f"{len(ann)} annonce(s)"
    else:
        if isinstance(ancien_histo, dict) and ancien_histo.get("_annonces_prec"):
            out["_annonces"] = ancien_histo["_annonces_prec"]
        diag["annonces"] = "non recuperees"
    if out.get("_annonces"):
        histo["_annonces_prec"] = out["_annonces"]

    with open("cours.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("=== Robot cours BRVM ===")
    for k, v in diag.items():
        print(f"  {k}: {v}")
    print(f"  -> cours.json ecrit ({len(merged)} titres)")


if __name__ == "__main__":
    main()
