#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HEARON — przygotowanie ikon złączy do findera.

Bierze surowe rendery (duże PNG z przezroczystością) i robi z nich
spójny komplet ikon: przycięcie w miejscu, gdzie kabel wchodzi w złącze,
kwadratowy kadr, jednakowy margines, skala wg realnej wielkości złącza,
eksport do WebP (lekkie) i PNG (zapas).

Użycie:
    python3 przygotuj-ikony.py plik1.png plik2.png ...
albo bez argumentów — weźmie wszystko z KATALOG_WEJSCIOWY.

Nazwa pliku wyjściowego bierze się z MAPA_NAZW (dopasowanie po fragmencie
nazwy pliku wejściowego). Jeśli nic nie pasuje, użyta zostanie nazwa pliku.
"""

import io
import os
import sys
import glob
import numpy as np
from PIL import Image

KATALOG_WEJSCIOWY = "/mnt/user-data/uploads"
KATALOG_WYJSCIOWY = "/mnt/user-data/outputs/ikony"

ROZMIAR = 280          # px, plik źródłowy (2,7x względem wyświetlanych 104 px)
MARGINES = 0.09        # margines wokół obiektu
JAKOSC_WEBP = 85

# rozpoznanie typu po fragmencie nazwy pliku (małe litery)
# UWAGA: wzorce bardziej szczegółowe MUSZĄ być wyżej niż ogólne,
# bo dopasowanie kończy się na pierwszym trafieniu.
# Sufiks "-male-s" = wersja TS/mono (jeden pierścień), bez niego TRS/stereo.
MAPA_NAZW = [
    ("jack_3_5_trrs",  "jack-35-trrs"),
    ("jack_3_5_trs",   "jack-35-trs"),
    ("jack_3_5_ts",    "jack-35-ts"),
    ("jack_6_3_trs",   "jack-63-trs"),
    ("jack_6_3_ts",    "jack-63-ts"),
    ("din",            "din-5pin"),
    ("optyczn",        "toslink"),
    ("toslink",        "toslink"),
    ("wtyk_banan",     "banan"),
    ("usb-c",          "usb-c"),
    ("usbc",           "usb-c"),
    # --- warianty mono (TS) — przed ogólnymi wzorcami rozmiaru ---
    ("65mm-001-male-s", "jack-63-ts"),
    ("63mm-001-male-s", "jack-63-ts"),
    ("35mm-001-male-s", "jack-35-ts"),
    ("ts-mono",         "jack-63-ts"),
    # --- warianty 4-stykowe ---
    ("trrs",           "jack-35-trrs"),
    # --- reszta ---
    ("xlr-001",        "xlr-3pin"),
    ("mini",           "mini-xlr-3pin"),
    ("xlr",            "xlr-3pin"),
    ("35mm",           "jack-35-trs"),
    ("3.5",            "jack-35-trs"),
    ("65mm",           "jack-63-trs"),
    ("63mm",           "jack-63-trs"),
    ("6.3",            "jack-63-trs"),
    ("usb-c",          "usb-c"),
    ("usbc",           "usb-c"),
    ("type-c",         "usb-c"),
    ("typec",          "usb-c"),
    ("micro-usb",      "micro-usb"),
    ("mini-usb",       "mini-usb"),
    ("rca",            "rca"),
    ("speakon",        "powercon"),   # niebieski render to powerCON
    ("powercon",       "powercon"),
    ("powercon",       "powercon"),
    ("banan",          "banan"),
    ("rj45",           "rj45"),
    ("ethercon",       "rj45"),
    ("hdmi",           "hdmi"),
]

# REGUŁA WIELKOŚCI — złagodzona proporcja realnych średnic złączy.
# 1.00 = największe (XLR, Speakon, Powercon). Wartości to pierwiastek
# z rzeczywistej proporcji, żeby małe złącza były widocznie mniejsze,
# ale nie znikały w siatce.
SKALA = {
    "xlr-3pin":       1.00,   # ~19 mm
    "speakon":        1.00,   # ~22 mm
    "powercon":       1.00,   # ~22 mm
    "hdmi":           0.86,
    "jack-63-trs":    0.79,   # ~12 mm
    "jack-63-ts":     0.79,
    "mini-xlr-3pin":  0.79,
    "rj45":           0.79,
    "rca":            0.76,   # ~11 mm
    "banan":          0.72,
    "din-5pin":       0.86,
    "toslink":        0.80,
    "usb-c":          0.62,
    "micro-usb":      0.55,
    "mini-usb":       0.58,
    "usb-a":          0.72,
    "jack-35-trs":    0.65,   # ~8 mm
    "jack-35-trrs":   0.65,
}


def nazwa_wyjsciowa(sciezka):
    baza = os.path.basename(sciezka).lower()
    for fragment, nazwa in MAPA_NAZW:
        if fragment in baza:
            return nazwa
    return os.path.splitext(os.path.basename(sciezka))[0].lower()


def punkt_ciecia(im):
    """Znajduje kolumnę, w której kabel przechodzi w złącze.

    Najpierw sprawdza, CZY kabel w ogóle jest: kabel to długi odcinek
    o stałej, cienkiej grubości na początku obiektu. Przy renderach
    samych wtyków (np. banan) kabla nie ma i wtedy nic nie przycinamy.
    """
    a = np.array(im.getchannel("A"))
    op = a > 16
    kol = op.sum(axis=0)
    nz = np.nonzero(kol)[0]
    if len(nz) == 0:
        return 0
    x0, x1 = nz.min(), nz.max()
    rozpietosc = x1 - x0
    if rozpietosc < 10:
        return 0

    maks = kol.max()
    w5 = kol[min(x0 + int(rozpietosc * 0.05), x1)]
    w20 = kol[min(x0 + int(rozpietosc * 0.20), x1)]

    # kabel: oba punkty cienkie względem najgrubszego miejsca
    # i zbliżone do siebie (stała grubość)
    cienki = w5 < maks * 0.6 and w20 < maks * 0.6
    stala = w5 > 0 and abs(w20 - w5) <= max(w5 * 0.25, 8)
    if not (cienki and stala):
        return 0          # brak kabla — nie tniemy

    baza = np.median(kol[x0:x0 + max(int(rozpietosc * 0.12), 5)])
    for x in range(x0, x1):
        if kol[x] > baza * 1.35:
            return max(x - int(rozpietosc * 0.02), 0)
    return x0


def przytnij(im):
    bb = im.getchannel("A").point(lambda p: 255 if p > 8 else 0).getbbox()
    return im.crop(bb) if bb else im


def przygotuj(sciezka):
    nazwa = nazwa_wyjsciowa(sciezka)
    skala = SKALA.get(nazwa, 1.0)

    im = Image.open(sciezka).convert("RGBA")
    cut = punkt_ciecia(im)
    im = przytnij(im.crop((cut, 0, im.size[0], im.size[1])))

    # kwadrat + margines
    w, h = im.size
    s = max(w, h)
    plotno = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    plotno.paste(im, ((s - w) // 2, (s - h) // 2), im)
    m = int(s * MARGINES)
    plotno2 = Image.new("RGBA", (s + 2 * m, s + 2 * m), (0, 0, 0, 0))
    plotno2.paste(plotno, (m, m), plotno)

    # skala wg reguły wielkości
    docelowy = int(ROZMIAR * skala)
    zmniejszony = plotno2.resize((docelowy, docelowy), Image.LANCZOS)
    finalny = Image.new("RGBA", (ROZMIAR, ROZMIAR), (0, 0, 0, 0))
    przesun = (ROZMIAR - docelowy) // 2
    finalny.paste(zmniejszony, (przesun, przesun), zmniejszony)

    os.makedirs(KATALOG_WYJSCIOWY, exist_ok=True)
    p_webp = os.path.join(KATALOG_WYJSCIOWY, nazwa + ".webp")
    p_png = os.path.join(KATALOG_WYJSCIOWY, nazwa + ".png")
    finalny.save(p_webp, "WEBP", quality=JAKOSC_WEBP, method=6)
    finalny.save(p_png, "PNG", optimize=True)
    return nazwa, skala, os.path.getsize(p_webp) / 1024


def main():
    pliki = sys.argv[1:]
    if not pliki:
        pliki = sorted(glob.glob(os.path.join(KATALOG_WEJSCIOWY, "*.png")))
    if not pliki:
        print("Brak plików wejściowych.")
        return
    print(f"{'plik wyjściowy':18} | {'skala':>6} | {'WebP':>8}")
    print("-" * 40)
    for p in pliki:
        try:
            nazwa, skala, kb = przygotuj(p)
            print(f"{nazwa:18} | {skala:6.2f} | {kb:6.1f} KB")
        except Exception as e:
            print(f"BŁĄD {os.path.basename(p)}: {e}")


if __name__ == "__main__":
    main()
