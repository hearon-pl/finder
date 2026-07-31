#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HEARON — generator danych dla findera kabli.

Pobiera feed IOF z hearon.pl, wyciąga kable z rozpoznanymi złączami
i zapisuje lekki finder-data.json (kilkadziesiąt–kilkaset KB),
który ładuje finder na stronie sklepu.

Uruchamiany raz na dobę przez GitHub Actions.
"""

import json
import os
import re
import sys
import urllib.request
from collections import Counter, OrderedDict
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------- konfiguracja
FEED_URL = os.environ.get(
    "FEED_URL",
    "https://hearon.pl/data/export/feed10000_1ac49ba8bb7cc68dae4994a0.xml",
)
OUT_FILE = os.environ.get("OUT_FILE", "finder-data.json")
# Zostaw pustą listę = wszystkie marki. Np. ["HEARON"] = tylko HEARON.
ONLY_BRANDS = [b.strip().upper() for b in os.environ.get("ONLY_BRANDS", "HEARON").split(",") if b.strip()]

IOFEXT = "{http://www.iai-shop.com/developers/iof/extensions.phtml}"

# ------------------------------------------------------------- rozpoznawanie złączy
# płeć na końcu wartości — obsługujemy oba nazewnictwa naraz,
# żeby zmiana parametrów w IdoSell mogła iść stopniowo
GEND = re.compile(r"(wtyk|gniazdo|męski|meski|żeński|zenski)(\s+kątow[ya])?\s*$", re.I)
MESKIE = {"wtyk", "męski", "meski"}
# nazewnictwo płci pokazywane klientowi — zmiana tutaj zmienia je wszędzie
ETYKIETA_PLCI = {"M": "męski", "F": "żeński"}
NOISE = (
    "posiadają zabezpiecz",
    "high-speed",
    "niebieski",
    "przekazywanie",
    "wejście zasilania",
)

def kind(v):
    """Zwraca kategorię złącza albo None, jeśli wartość nie jest złączem."""
    if not v:
        return None
    vl = v.lower().strip()
    if vl.startswith("kabel "):
        return None
    if any(x in vl for x in NOISE):
        return None
    if vl in (
        "złącze z drugiej strony",
        "złącze z jednej strony",
        "standard",
        "stabilizator tworzywowy",
        "przejściówka (adapter audio)",
        "zaciśnięte mosiężne tulejki",
    ):
        return None
    if "banan" in vl:
        return "banan"
    if ("powercon" in vl or "zasilania sieciowego" in vl
            or "zasilające sieciowe" in vl or "iec c13" in vl or "iec c14" in vl):
        return "powercon"
    if "rj45" in vl or "ethercon" in vl:
        return "rj45"
    if "speakon" in vl:
        return "speakon"
    if "hdmi" in vl:
        return "hdmi"
    if GEND.search(v.strip()):
        return "audio"
    return None

# złącza typowo symetryczne: jedna wartość = ten sam wtyk na obu końcach
SYMM = {"banan", "powercon", "rj45", "speakon"}

def parse_end(v, k):
    """Rozkłada wartość złącza na typ (do selektora), płeć i etykietę (do wyniku)."""
    s = v.strip()
    if k == "banan":
        cnt = 2 if re.match(r"^\s*2x", s, re.I) else 1
        return {"type": "Wtyk bananowy", "gender": None,
                "label": ("2× " if cnt > 1 else "") + "Wtyk bananowy"}
    if k == "powercon":
        return {"type": "Powercon", "gender": None, "label": "Powercon"}
    if k == "rj45":
        return {"type": "RJ45 / Ethercon", "gender": None, "label": "RJ45 / Ethercon"}
    if k == "speakon":
        g = "F" if re.search(r"gniazdo|żeńsk|zensk", s, re.I) else ("M" if re.search(r"\bwtyk\b|męsk|mesk", s, re.I) else None)
        return {"type": "Speakon", "gender": g, "label": "Speakon"}
    if k == "hdmi":
        g = "F" if re.search(r"gniazdo|żeńsk|zensk", s, re.I) else ("M" if re.search(r"wtyk|męsk|mesk", s, re.I) else None)
        return {"type": "HDMI", "gender": g, "label": ("HDMI żeński" if g == "F" else "HDMI męski")}

    ang = bool(re.search(r"kątow", s, re.I))
    s2 = re.sub(r"\s*kątow[ya]\s*", " ", s, flags=re.I).strip()
    m = re.match(r"^\s*(\d+)x\s*(.*)$", s2)
    count = int(m.group(1)) if m else 1
    body = m.group(2) if m else s2
    gm = GEND.search(body)
    gender = None
    if gm:
        gender = "M" if gm.group(1).lower() in MESKIE else "F"
        body = body[:gm.start()].strip()
    t = re.sub(r"\bTRS stereo\b", "TRS", body)
    t = re.sub(r"\bTS mono\b", "TS", t).strip()
    if re.search(r"mini", t, re.I) and re.search(r"xlr", t, re.I):
        t = "mini XLR 3-pin"
    gw = ETYKIETA_PLCI.get(gender, "")
    label = (f"{count}× " if count > 1 else "") + body + ((" " + gw) if gw else "") + (" kątowy" if ang else "")
    return {"type": t, "gender": gender, "label": label.strip()}

def sig_bucket(s):
    s = (s or "").lower()
    if ("symetryczny" in s and "niesym" not in s) or ("balanced" in s and "unbalanced" not in s):
        return "symetryczny"
    return "niesymetryczny"

def parse_len(params, name):
    for k in ("Długość [m]", "Długość", "Długość kabla [m]"):
        v = params.get(k, "")
        if v:
            vl = v.lower()
            if "cm" in vl:
                m = re.search(r"([\d,\.]+)", vl)
                if m:
                    return round(float(m.group(1).replace(",", ".")) / 100, 3)
            try:
                return float(vl.replace(",", "."))
            except ValueError:
                pass
    m = re.search(r"(\d+(?:[,\.]\d+)?)\s*m\b", name)
    return float(m.group(1).replace(",", ".")) if m else None

def cats_from_menu(paths):
    out = []
    for path in paths:
        parts = [p.strip() for p in re.split(r"\\+", path.strip()) if p.strip()]
        if len(parts) >= 2:
            out.append("Adaptery" if parts[0].lower().startswith("złącza") else parts[1])
    return sorted(set(out))

# luźne złącza (nie kable) — nie należą do findera
LUZNE = re.compile(r"^\s*(hearon\s+)?(wtyk|gniazdo)\b", re.I)
# adapter rozpoznajemy po nazwie — konwencja nazewnicza HEARON
ADAPTER = re.compile(r"adapter|przejściówk|przejsciowk|rozgałęźnik|rozgaleznik|gender\s*changer", re.I)


# wartość złącza opisująca CAŁY kabel (dwa złącza rozdzielone myślnikiem)
# to prawie zawsze pomyłka przy wypełnianiu parametru
RODZINY = r"xlr|jack|rca|cinch|speakon|powercon|hdmi|rj45|banan"
PODEJRZANE = re.compile(rf"({RODZINY}).*\s[-–]\s.*({RODZINY})", re.I)
OSTRZEZENIA = []


def rodzaj_produktu(nazwa):
    return "Adapter" if ADAPTER.search(nazwa or "") else "Kabel"


def famkey(code):
    return re.split(r"[-_ ]", code)[0] if code else code

# ------------------------------------------------------------------- pobranie
def fetch_feed(url, dest="feed.xml"):
    print(f"Pobieram feed: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "hearon-finder-build/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        total = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    print(f"Pobrano {total/1048576:.1f} MB -> {dest}")
    return dest

# -------------------------------------------------------------------- parsing
def parse_products(path):
    """Strumieniowo czyta feed IOF i zwraca listę rekordów produktów."""
    recs = []
    skipped_brand = 0
    for event, elem in ET.iterparse(path, events=("end",)):
        if not elem.tag.endswith("product"):
            continue

        prod = elem
        producer = prod.find("producer")
        brand = (producer.get("name") if producer is not None else "") or ""
        if ONLY_BRANDS and brand.strip().upper() not in ONLY_BRANDS:
            skipped_brand += 1
            prod.clear()
            continue

        # nazwa
        name_el = prod.find("./description/name")
        name = (name_el.text or "").strip() if name_el is not None else ""

        # link do karty
        card = prod.find("card")
        url = (card.get("url") if card is not None else "") or ""
        # feedowy URL -> zwykły link do produktu
        url = re.sub(r"\.feed\d+\.html$", ".html", url)

        # cena brutto
        price = None
        price_el = prod.find("price")
        if price_el is not None:
            try:
                price = float(price_el.get("gross"))
            except (TypeError, ValueError):
                price = None

        # dostępność: bierzemy ze stanu wariantu (<size available="in_stock">)
        dostepny = False
        for sz in prod.findall("./sizes/size"):
            if (sz.get("available") or "").lower() == "in_stock":
                dostepny = True
                break

        # kod produktu
        code = prod.get("code_on_card") or prod.get("id") or ""

        # oficjalny klucz grupowania wariantów (IdoSell): <group id="...">
        grp = prod.find("group")
        group_id = grp.get("id") if grp is not None else None

        # miniatura produktu: najpierw mały wariant ikony (~200x150), potem większe
        img = ""
        icon = prod.find("./images/icons/icon")
        if icon is not None:
            img = icon.get(IOFEXT + "url_small") or icon.get("url") or ""
        if not img:
            big = prod.find("./images/large/image")
            if big is not None:
                img = big.get("url") or ""

        # parametry: nazwa <-> wartość związane w jednym węźle (bez rozjazdu!)
        params = OrderedDict()
        conn_values = []
        for p in prod.findall("./parameters/parameter"):
            pname = (p.get("name") or "").strip()
            vals = [ (v.get("name") or "").strip() for v in p.findall("value") ]
            if not pname:
                continue
            if pname not in params and vals:
                params[pname] = vals[0]
            for v in vals:
                if kind(v):
                    conn_values.append(v)

        # menu (typ / zastosowanie kabla z drzewa sklepu)
        menu_paths = []
        for item in prod.iter():
            if item.tag == IOFEXT + "item" or item.tag.endswith("}item") or item.tag == "item":
                tid = item.get("textId") or item.get("textid")
                if tid:
                    menu_paths.append(tid)

        # wybór dwóch końcówek
        pairs = [(v, kind(v)) for v in conn_values]
        pairs = [(v, k) for v, k in pairs if k]
        if len(pairs) >= 2:
            (va, ka), (vb, kb) = pairs[0], pairs[1]
        elif len(pairs) == 1 and pairs[0][1] in SYMM:
            (va, ka) = pairs[0]
            (vb, kb) = pairs[0]
        else:
            prod.clear()
            continue

        if LUZNE.match(name):        # luźny wtyk/gniazdo, nie kabel
            prod.clear()
            continue

        for wartosc in (va, vb):
            if PODEJRZANE.search(wartosc or ""):
                OSTRZEZENIA.append((code, name, wartosc))

        ea, eb = parse_end(va, ka), parse_end(vb, kb)
        recs.append({
            "code": code, "group_id": group_id, "name": name, "brand": brand.strip(),
            "ea": ea, "eb": eb,
            "sig": sig_bucket(params.get("Typ sygnału", "")),
            "len": parse_len(params, name),
            "price": price, "url": url, "img": img, "ok": dostepny,
            "cats": cats_from_menu(menu_paths),
            "rodzaj": rodzaj_produktu(name),
        })
        prod.clear()
    print(f"Rozpoznane kable: {len(recs)} SKU (pominięte inne marki: {skipped_brand})")
    return recs

# ------------------------------------------------------------------ grupowanie
def build_data(recs):
    groups = OrderedDict()
    for x in recs:
        # grupujemy po oficjalnym group_id z IdoSell; gdy go brak — po prefiksie kodu
        gk = x["group_id"] or famkey(x["code"])
        key = (x["brand"], gk, x["ea"]["label"], x["eb"]["label"], x["sig"])
        g = groups.setdefault(key, {
            "name": x["name"], "brand": x["brand"],
            "aType": x["ea"]["type"], "aGender": x["ea"]["gender"], "aLabel": x["ea"]["label"],
            "bType": x["eb"]["type"], "bGender": x["eb"]["gender"], "bLabel": x["eb"]["label"],
            "sig": x["sig"], "img": x["img"], "rodzaj": x["rodzaj"],
            "lengths": [], "variants": [], "cats": set(),
        })
        if x["len"] is not None and x["len"] not in g["lengths"]:
            g["lengths"].append(x["len"])
        if not g["img"] and x["img"]:
            g["img"] = x["img"]
        g["variants"].append([x["len"], x["price"], x["url"], 1 if x["ok"] else 0])
        g["cats"].update(x["cats"])

    for g in groups.values():
        g["lengths"].sort()
        g["variants"].sort(key=lambda v: (v[0] or 0))
        # ceny liczymy z wariantów dostępnych; gdy nic nie ma — ze wszystkich
        dost = [v for v in g["variants"] if v[3]]
        baza = dost or g["variants"]
        prices = [v[1] for v in baza if v[1]]
        g["price_min"] = min(prices) if prices else None
        g["price_max"] = max(prices) if prices else None
        g["url"] = baza[0][2] if baza else ""
        g["any"] = 1 if dost else 0
        g["name"] = re.sub(r"\s*\d+([,\.]\d+)?\s*m\b", "", g["name"], flags=re.I).strip(" -–")
        g["cats"] = sorted(g["cats"])

    TYPE_ORDER = ["XLR 3-pin", "mini XLR 3-pin", "Jack 6,3 mm TRS", "Jack 6,3 mm TS",
                  "Jack 3,5 mm TRS", "Jack 3,5 mm TRRS", "RCA (cinch)", "Speakon",
                  "Wtyk bananowy", "Powercon", "RJ45 / Ethercon", "HDMI"]
    types = sorted(
        {g["aType"] for g in groups.values()} | {g["bType"] for g in groups.values()},
        key=lambda t: TYPE_ORDER.index(t) if t in TYPE_ORDER else 99,
    )
    lengths = sorted({l for g in groups.values() for l in g["lengths"]})
    cats = Counter(c for g in groups.values() for c in g["cats"])

    # adresy mają wspólny początek — wynosimy go raz, żeby nie powtarzać w każdym wariancie
    PREFIX = "https://hearon.pl/pl/products/"
    IMGPRE = "https://hearon.pl/hpeciai/"
    for g in groups.values():
        if g["url"].startswith(PREFIX):
            g["url"] = g["url"][len(PREFIX):]
        for v in g["variants"]:
            if v[2].startswith(PREFIX):
                v[2] = v[2][len(PREFIX):]
        if g.get("img", "").startswith(IMGPRE):
            g["img"] = g["img"][len(IMGPRE):]

    keep = ("name", "brand", "aType", "aGender", "aLabel", "bType", "bGender",
            "bLabel", "sig", "lengths", "price_min", "price_max", "url", "cats", "img",
            "variants", "any", "rodzaj")
    return {
        "generated": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "url_prefix": PREFIX,
        "img_prefix": IMGPRE,
        "types": types,
        "lengths": lengths,
        "cats": [c for c, _ in cats.most_common()],
        "rodzaje": ["Kabel", "Adapter"],
        "products": [{k: g[k] for k in keep} for g in groups.values()],
    }

# ----------------------------------------------------------------------- main
def main():
    path = fetch_feed(FEED_URL)
    recs = parse_products(path)
    if not recs:
        print("BŁĄD: nie rozpoznano żadnych kabli — nie nadpisuję pliku.", file=sys.stderr)
        sys.exit(1)
    data = build_data(recs)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(OUT_FILE)
    if OSTRZEZENIA:
        print("\n!!! PODEJRZANE WARTOŚCI ZŁĄCZY (do poprawy w IdoSell) !!!")
        for kod, nazwa, wart in OSTRZEZENIA:
            print(f"  [{kod}] {nazwa[:60]}")
            print(f"        wartość: {wart!r}  <- wygląda na opis kabla, nie na złącze")
        print()

    print(f"Zapisano {OUT_FILE}: {len(data['products'])} rodzin, "
          f"{len(data['types'])} typów złączy, {size/1024:.0f} KB")
    try:
        os.remove(path)
    except OSError:
        pass

if __name__ == "__main__":
    main()
