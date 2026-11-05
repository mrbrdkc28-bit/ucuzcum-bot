"""
UCUZCUM — ESLESME ARACI (Claude incelemeli)

Colab'daki eslestir13.py ile ayni tabloyu (eslesmeler.json) doldurur; fark:
adaylari insan yerine Claude inceler, emin olamadiginda urun sayfasina bakar.
Bot bu dosyayi hic degistirmeden okur (tablo_market / *_yok / atla).

AKIS
  1) aday   : eslesmesi eksik urunleri secer, her market icin arar, adaylari
              puanlar -> eslesme_is/aday_<zaman>.json
  2) goster : aday dosyasini satir satir, incelemeye uygun bicimde yazar
  3) (Claude kararlari yazar -> karar.json, bicim asagida)
  4) uygula : kararlari dogrular, yedek alir, eslesmeler.json'a isler
  5) durum  : tablo ve kapsama ozeti

KOMUTLAR (depo klasorunde):
  python eslesme_araci.py aday [--adet 40] [--market migros,ozdilek]
                                [--kaynak A101] [--ara "torku sucuk"]
  python eslesme_araci.py goster eslesme_is/aday_....json
  python eslesme_araci.py uygula eslesme_is/aday_....json karar.json
  python eslesme_araci.py durum

KARAR BICIMI (karar.json):
  {"a101_123": {"migros": 2, "ozdilek": "yok", "macro": null},
   "sok_456":  {"atla": true}}
    sayi  -> aday listesindeki o numarali urun (1'den baslar)
    "yok" -> bu markette bulunamadi (kontrol listesine, tabloya degil)
    null  -> karar yok, sonra yeniden sorulur
    atla  -> urun hicbir yerde eslenemez (kontrol listesine, tabloya degil)
    "neden": "..." -> elenme gerekcesi (istege bagli, kontrol listesine yazilir)

KONTROL LISTESI (eslesme_kontrol.json)
  Claude'un eslediklerini ve eledikleri. Elenenler tabloya "_yok"/"atla"
  olarak YAZILMAZ: Colab araci onlari sormaya devam eder, kullanici sonra
  kontrol eder. Bu arac ise elenenleri bir daha aday olarak getirmez.

GUVENLIK
  * Farkli gramaj secilemez: bot zaten gostermez (elle_esdeger), tabloyu
    kirletmesin. Iki taraftan biri okunamiyorsa izin verilir.
  * Urunun kendi marketiyle eslesme yazilmaz.
  * "onay" isaretli (kullanicinin Colab'da onayladigi) kayitlar degismez.
  * Her uygulamadan once eslesme_is/yedek/ altina yedek alinir.
  * Islenen her karar eslesme_is/gecmis.jsonl dosyasina eklenir.
"""

import argparse
import datetime
import importlib.util
import json
import os
import shutil
import sys
import time
import urllib.request

KLASOR = os.path.dirname(os.path.abspath(__file__))
IS_KLASORU = os.path.join(KLASOR, "eslesme_is")
TABLO = os.path.join(KLASOR, "eslesmeler.json")
URUNLER_URL = "https://mrbrdkc28-bit.github.io/ucuzcum-web/urunler.json"
BEKLEME = 0.3           # market istekleri arasi (saniye)
ADAY_SAYISI = 8         # market basina incelemeye konan aday

# Botun arama/olcu/ad kurallarini aynen kullan: kurallar tek yerde kalsin.
_spec = importlib.util.spec_from_file_location(
    "bot", os.path.join(KLASOR, "bot.py"))
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

# tablo anahtari -> uygulamadaki market adi (normalize: bot.tr_ara)
MARKETLER = {
    "migros": "migros",
    "ozdilek": "ozdilek",
    "macro": "macrocenter",
    "carrefour": "carrefour",
    "sok": "sok",
    "file": "file",
}


def _norm_market(ad):
    return bot.tr_ara(ad or "").strip()


def kendi_marketi(urun, anahtar):
    return _norm_market(urun.get("market")) == MARKETLER[anahtar]


def kars_marketleri(urun):
    """Botun bu urun icin zaten buldugu otomatik eslesmeler (market adlari)."""
    return {_norm_market(m) for m in (urun.get("karsilastirma") or {})}


# ------------------------------------------------------------ veri yukleme

def json_getir(url):
    r = urllib.request.Request(url, headers=bot.BASLIKLAR)
    with urllib.request.urlopen(r, timeout=60) as c:
        return json.loads(c.read().decode("utf-8"))


def urunleri_yukle():
    """Uygulamanin gordugu taze liste (GitHub Pages; Firebase kotasi harcanmaz)."""
    return (json_getir(URUNLER_URL).get("urunler") or {})


def tablo_yukle():
    with open(TABLO, "r", encoding="utf-8") as f:
        return json.load(f)


def carrefour_yukle():
    try:
        with open(os.path.join(KLASOR, "carrefour.json"), encoding="utf-8") as f:
            bot.CARREFOUR_KATALOG = json.load(f).get("katalog") or {}
    except Exception:
        bot.CARREFOUR_KATALOG = {}


def file_yukle():
    """File'in tum katalogu (gunluk onbellek; bot ayni API'yi kullanir)."""
    yol = os.path.join(IS_KLASORU, "file_katalog.json")
    try:
        if time.time() - os.path.getmtime(yol) < 20 * 3600:
            with open(yol, encoding="utf-8") as f:
                bot.FILE_KATALOG = json.load(f)
                return
    except OSError:
        pass
    katalog = {}
    for kat in bot.file_json("categories") or []:
        kid = kat.get("id")
        if not kid:
            continue
        for s in bot.file_json(f"categories/{kid}/subcategories") or []:
            for p in s.get("products") or []:
                pid = str(p.get("id") or p.get("productCode") or "")
                if pid and p.get("productName") and p.get("productPrice"):
                    katalog.setdefault(pid, {"a": p["productName"],
                                             "f": float(p["productPrice"])})
        time.sleep(BEKLEME)
    bot.FILE_KATALOG = katalog
    with open(yol, "w", encoding="utf-8") as f:
        json.dump(katalog, f, ensure_ascii=False)


# ------------------------------------------------------------ market aramalari
# Hepsi ayni bicimde doner: {kod, ad, fiyat, link, stok}

def _mig_tipi(sonuclar, link_fn):
    out = []
    for d in sonuclar:
        sku = d.get("sku") or d.get("id")
        kat = bot.fiyat_katmanlari(d)
        if not sku or not kat["kartli"]:
            continue
        out.append({"kod": str(sku), "ad": d.get("name", ""),
                    "fiyat": bot.tl(kat["kartli"]),
                    "link": link_fn(d), "stok": bot.stokta_var_mi(d)})
    return out


def ara_migros(q):
    return _mig_tipi(bot.migros_katalog_ara(q), bot.migros_link)


def ara_macro(q):
    return _mig_tipi(bot.macro_katalog_ara(q), bot.macro_link)


def ara_ozdilek(q):
    out = []
    for u in bot.ozdilek_katalog_ara(q):
        fiyat = ((u.get("price") or {}).get("value")
                 or (u.get("listPrice") or {}).get("value"))
        if u.get("code") and fiyat:
            out.append({"kod": str(u["code"]), "ad": u.get("name", ""),
                        "fiyat": round(float(fiyat), 2),
                        "link": bot.ozdilek_link(u),
                        "stok": bot.ozdilek_stokta_var_mi(u)})
    return out


def ara_sok(q):
    return [{"kod": str(u["kod"]), "ad": u["ad"], "fiyat": u["normal"],
             "link": u["link"], "stok": True}
            for u in bot.sok_katalog_ara(q)]


def _yerel_ara(katalog, q, link_alani=None):
    kelimeler = [w for w in bot.kars_normalize(q).split() if len(w) >= 3]
    if not kelimeler:
        return []
    puanli = []
    for kod, v in katalog.items():
        parcalar = set(bot.kars_normalize(v.get("a", "")).split())
        ortak = sum(1 for w in kelimeler if w in parcalar)
        if ortak:
            puanli.append((ortak * 10 + (5 if kelimeler[0] in parcalar else 0),
                           kod, v))
    puanli.sort(key=lambda x: -x[0])
    return [{"kod": str(kod), "ad": v.get("a", ""), "fiyat": v.get("f"),
             "link": v.get(link_alani, "") if link_alani else "",
             "stok": True} for _, kod, v in puanli[:12]]


def ara_carrefour(q):
    return _yerel_ara(bot.CARREFOUR_KATALOG or {}, q, "l")


def ara_file(q):
    return _yerel_ara(bot.FILE_KATALOG or {}, q)


ARAMA = {"migros": ara_migros, "ozdilek": ara_ozdilek, "macro": ara_macro,
         "carrefour": ara_carrefour, "sok": ara_sok, "file": ara_file}
AG_ISTEGI = {"migros", "ozdilek", "macro", "sok"}


# ------------------------------------------------------------ puanlama

def anlamli(ad):
    return {w for w in bot.kars_normalize(ad).split()
            if len(w) >= 3 and not any(c.isdigit() for c in w)}


def ozellikler(kaynak_ad, kaynak_fiyat, aday):
    """Adayin kaynak urunle uyumu. Karar Claude'da; bunlar yalniz ipucu."""
    a, b = anlamli(kaynak_ad), anlamli(aday["ad"])
    kel = bot.kars_kelimeler(kaynak_ad, 30)
    m1, m2 = bot.kars_miktar(kaynak_ad), bot.kars_miktar(aday["ad"])
    oz = {
        "marka": bool(kel) and kel[0] in set(bot.kars_normalize(aday["ad"]).split()),
        "miktar": ("ayni" if m1 and m1 == m2 else
                   "okunamadi" if not (m1 and m2) else "farkli"),
        "ek": bot.kars_ek_uyumlu(kaynak_ad, aday["ad"]),
        "bicim": bot.kars_uygulama_bicimi(kaynak_ad)
                 == bot.kars_uygulama_bicimi(aday["ad"]),
        "benzerlik": round(len(a & b) / len(a | b), 2) if (a | b) else 0,
        "eksik": sorted(a - b),
        "fazla": sorted(b - a),
        "oran": (round(aday["fiyat"] / kaynak_fiyat, 2)
                 if kaynak_fiyat and aday.get("fiyat") else None),
    }
    # Kaynakta olup adayda olmayan kelime (salam, light, laktozsuz) urunun
    # turunu/cesidini belirler: fazla kelimeden (market yazim farki) agir.
    # Farkli marka asla ayni urun degildir: listenin dibine itilir.
    puan = (3 * oz["marka"] - 5 * (not oz["marka"]) + 3 * (oz["miktar"] == "ayni")
            + 4 * oz["benzerlik"] + oz["ek"] + oz["bicim"]
            - 1.0 * len(oz["eksik"]) - 0.3 * len(oz["fazla"])
            - 4 * (oz["miktar"] == "farkli") - (not aday.get("stok", True)))
    oz["puan"] = round(puan, 2)
    return oz


def sorgular(ad):
    kel = bot.kars_kelimeler(ad, 30)
    qs = [" ".join(kel[:4]), " ".join(kel[:2])]
    if len(kel) >= 3:
        # marka + ayirt edici kelimeler ("banvit bufe salam")
        qs.append(" ".join([kel[0]] + kel[2:4]))
    if len(kel) > 4:
        qs.append(" ".join([kel[0]] + kel[-2:]))
    gorulen, out = set(), []
    for q in qs:
        if q and q not in gorulen:
            gorulen.add(q)
            out.append(q)
    return out


def adaylari_bul(urun, anahtar):
    ad = urun.get("urun_adi", "")
    fiyat = urun.get("normal_fiyat") or urun.get("gecerli_fiyat")
    toplam = {}
    for q in sorgular(ad):
        try:
            for aday in ARAMA[anahtar](q):
                toplam.setdefault(aday["kod"], aday)
        except Exception as e:
            print(f"    {anahtar} arama hatasi: {type(e).__name__}")
        if anahtar in AG_ISTEGI:
            time.sleep(BEKLEME)
    liste = []
    for aday in toplam.values():
        aday["oz"] = ozellikler(ad, fiyat, aday)
        liste.append(aday)
    liste.sort(key=lambda x: -x["oz"]["puan"])
    return liste[:ADAY_SAYISI]


# ------------------------------------------------------------ komutlar

KONTROL = os.path.join(KLASOR, "eslesme_kontrol.json")


def kontrol_yukle():
    """Kullanicinin sonradan bakacagi liste: Claude'un eslediklerini ve
    eledikleri. Elenenler tabloya yazilmaz (Colab'da sorulmaya devam eder)."""
    try:
        with open(KONTROL, encoding="utf-8") as f:
            k = json.load(f)
    except FileNotFoundError:
        k = {}
    k.setdefault("eslesen", [])
    k.setdefault("elenen", [])
    return k


def kontrol_kaydet(k):
    with open(KONTROL, "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1)


def eksik_marketler(uid, urun, tablo, secili, elenen):
    kayit = tablo.get(uid) or {}
    if kayit.get("atla") or (uid, "hepsi") in elenen:
        return []
    zaten = kars_marketleri(urun)
    return [m for m in secili
            if not kendi_marketi(urun, m)
            and m not in kayit and not kayit.get(m + "_yok")
            and (uid, m) not in elenen
            and MARKETLER[m] not in zaten]


def komut_aday(arg):
    os.makedirs(IS_KLASORU, exist_ok=True)
    secili = [m.strip() for m in arg.market.split(",")] if arg.market \
        else list(MARKETLER)
    urunler, tablo = urunleri_yukle(), tablo_yukle()
    elenen = {(e["id"], e["market"]) for e in kontrol_yukle()["elenen"]}
    if "carrefour" in secili:
        carrefour_yukle()
    if "file" in secili:
        file_yukle()

    hedefler = []
    for uid, u in urunler.items():
        if not isinstance(u, dict):
            continue
        ad = u.get("urun_adi", "")
        if len(bot.kars_kelimeler(ad, 30)) < 2:
            continue
        if arg.kaynak and _norm_market(u.get("market")) != _norm_market(arg.kaynak):
            continue
        if arg.ara and not all(w in bot.kars_normalize(ad).split()
                               for w in bot.kars_normalize(arg.ara).split()):
            continue
        if arg.urun and uid not in arg.urun.split(","):
            continue
        eksik = eksik_marketler(uid, u, tablo, secili, elenen)
        if eksik:
            hedefler.append((uid, u, eksik))
    # Once gramaji okunabilen ve daha cok markette eksik olan urunler:
    # en cok kiyas satiri bunlardan cikiyor.
    # Markasi kac farkli markette satiliyor: cok markette olan markalarin
    # urunleri baska markette de bulunur, eslesme orani yuksek olur.
    marka_market = {}
    for u in urunler.values():
        if isinstance(u, dict):
            kel = bot.kars_kelimeler(u.get("urun_adi", ""), 1)
            if kel:
                marka_market.setdefault(kel[0], set()).add(
                    _norm_market(u.get("market")))

    def yayginlik(u):
        kel = bot.kars_kelimeler(u.get("urun_adi", ""), 1)
        return len(marka_market.get(kel[0], ())) if kel else 0

    hedefler.sort(key=lambda x: (not bot.kars_miktar(x[1].get("urun_adi", "")),
                                 -yayginlik(x[1]), -len(x[2]), x[0]))
    print(f"Eksik eslesmeli urun: {len(hedefler)}  -> {arg.adet} tanesi araniyor")

    is_listesi = []
    for n, (uid, u, eksik) in enumerate(hedefler[:arg.adet], 1):
        print(f"  [{n}/{min(arg.adet, len(hedefler))}] {u.get('urun_adi','')[:60]}")
        kayit = {"id": uid, "ad": u.get("urun_adi", ""),
                 "market": u.get("market", ""), "link": u.get("link", ""),
                 "fiyat": u.get("normal_fiyat") or u.get("gecerli_fiyat"),
                 "miktar": bot.kars_miktar(u.get("urun_adi", "")),
                 "adaylar": {}}
        for m in eksik:
            kayit["adaylar"][m] = adaylari_bul(u, m)
        is_listesi.append(kayit)

    zaman = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    yol = os.path.join(IS_KLASORU, f"aday_{zaman}.json")
    with open(yol, "w", encoding="utf-8") as f:
        json.dump(is_listesi, f, ensure_ascii=False, indent=1)
    print(f"\nYazildi: {yol}")
    return yol


def _miktar_yazi(m):
    return f"{m[0]}{m[1]}" if m else "?"


def komut_goster(arg):
    with open(arg.dosya, encoding="utf-8") as f:
        is_listesi = json.load(f)
    for k in is_listesi:
        print(f"\n## {k['id']} | {k['market']} | {k['ad']} | "
              f"{k['fiyat']} TL | {_miktar_yazi(k['miktar'])}")
        for m, adaylar in k["adaylar"].items():
            if not adaylar:
                print(f"  {m}: (sonuc yok)")
                continue
            print(f"  {m}:")
            gizli = 0
            for i, a in enumerate(adaylar, 1):
                oz = a["oz"]
                # farkli gramaj secilemez (uygula reddeder); kalabalik etmesin
                if oz["miktar"] == "farkli" or oz["puan"] < arg.esik:
                    gizli += 1
                    continue
                isaret = []
                if not oz["marka"]:
                    isaret.append("MARKA?")
                if oz["miktar"] != "ayni":
                    isaret.append("miktar:" + oz["miktar"])
                if not oz["ek"]:
                    isaret.append("ADET/%")
                if not oz["bicim"]:
                    isaret.append("BICIM")
                if not a.get("stok", True):
                    isaret.append("STOKTA YOK")
                fark = ""
                if oz["eksik"] or oz["fazla"]:
                    fark = (f" -[{' '.join(oz['eksik'])}]"
                            f" +[{' '.join(oz['fazla'])}]")
                print(f"    {i}) {a['ad'][:60]} | {a['fiyat']} TL"
                      f" x{oz['oran']} | {oz['puan']}"
                      f" {' '.join(isaret)}{fark}")
            if gizli:
                print(f"    (+{gizli} uygun olmayan aday gizlendi)")


def komut_uygula(arg):
    with open(arg.dosya, encoding="utf-8") as f:
        is_listesi = {k["id"]: k for k in json.load(f)}
    with open(arg.karar, encoding="utf-8") as f:
        kararlar = json.load(f)
    tablo = tablo_yukle()
    kontrol = kontrol_yukle()
    bugun = datetime.date.today().isoformat()

    os.makedirs(os.path.join(IS_KLASORU, "yedek"), exist_ok=True)
    yedek = os.path.join(IS_KLASORU, "yedek",
                         f"eslesmeler_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
    shutil.copy2(TABLO, yedek)

    sayac = {"eslesme": 0, "yok": 0, "atla": 0, "reddedilen": 0}
    gecmis = []
    for uid, karar in kararlar.items():
        k = is_listesi.get(uid)
        if k is None:
            print(f"  !! {uid} bu aday dosyasinda yok, atlandi")
            sayac["reddedilen"] += 1
            continue
        kayit = dict(tablo.get(uid) or {})
        temel = {"id": uid, "ad": k["ad"], "kaynak_market": k["market"],
                 "link": k.get("link", ""), "zaman": bugun}
        if karar.get("atla"):
            # Tabloya "atla" YAZILMAZ: kullanici sonra kontrol eder.
            kontrol["elenen"].append(dict(temel, market="hepsi",
                                          neden=karar.get("neden", "")))
            sayac["atla"] += 1
            gecmis.append({"id": uid, "atla": True, "zaman": bugun})
            continue
        for m, secim in karar.items():
            if secim is None or m == "neden":
                continue
            if m not in k["adaylar"]:
                print(f"  !! {uid} {m}: bu market sorulmamisti, atlandi")
                sayac["reddedilen"] += 1
                continue
            if isinstance(kayit.get(m), dict) and kayit[m].get("onay"):
                print(f"  !! {uid} {m}: kullanici onayli kayit, dokunulmadi")
                continue
            if secim == "yok":
                # Tabloya "_yok" YAZILMAZ (Colab'da sorulmaya devam eder);
                # kontrol listesine eklenir, arac da tekrar sormaz.
                kontrol["elenen"].append(dict(temel, market=m,
                                              neden=karar.get("neden", "")))
                sayac["yok"] += 1
                gecmis.append({"id": uid, "market": m, "yok": True,
                               "zaman": bugun})
                continue
            adaylar = k["adaylar"][m]
            if not (isinstance(secim, int) and 1 <= secim <= len(adaylar)):
                print(f"  !! {uid} {m}: gecersiz secim {secim!r}")
                sayac["reddedilen"] += 1
                continue
            a = adaylar[secim - 1]
            if a["oz"]["miktar"] == "farkli":
                print(f"  !! {uid} {m}: farkli gramaj ({a['ad']}), yazilmadi")
                sayac["reddedilen"] += 1
                continue
            kayit[m] = {"kod": a["kod"], "ad": a["ad"],
                        "kaynak": "claude", "zaman": bugun}
            kayit.pop(m + "_yok", None)
            kontrol["eslesen"].append(dict(temel, market=m, kod=a["kod"],
                                           eslesen_ad=a["ad"],
                                           eslesen_link=a.get("link", "")))
            sayac["eslesme"] += 1
            gecmis.append({"id": uid, "ad": k["ad"], "market": m,
                           "kod": a["kod"], "aday_ad": a["ad"],
                           "zaman": bugun})
        if kayit:
            tablo[uid] = kayit

    with open(TABLO, "w", encoding="utf-8") as f:
        json.dump(tablo, f, ensure_ascii=False, indent=1)
    kontrol_kaydet(kontrol)
    with open(os.path.join(IS_KLASORU, "gecmis.jsonl"), "a",
              encoding="utf-8") as f:
        for g in gecmis:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
    print(f"Islendi: {sayac}   (yedek: {yedek})")


def komut_durum(arg):
    tablo = tablo_yukle()
    urunler = urunleri_yukle()
    say = {m: 0 for m in MARKETLER}
    yok = {m: 0 for m in MARKETLER}
    claude = 0
    for v in tablo.values():
        if not isinstance(v, dict):
            continue
        for m in MARKETLER:
            if isinstance(v.get(m), dict):
                say[m] += 1
                claude += v[m].get("kaynak") == "claude"
            if v.get(m + "_yok"):
                yok[m] += 1
    tabloda = sum(1 for uid in urunler if uid in tablo)
    kiyasli = sum(1 for u in urunler.values() if u.get("karsilastirma"))
    print(f"Tablo: {len(tablo)} kayit | eslesme: {say} | yok: {yok}")
    print(f"Claude'un ekledigi eslesme: {claude}")
    print(f"Uygulamadaki urun: {len(urunler)} | tabloda: {tabloda} | "
          f"kiyasi olan: {kiyasli}")


def main():
    p = argparse.ArgumentParser(description="Ucuzcum eslesme araci")
    alt = p.add_subparsers(dest="komut", required=True)
    a = alt.add_parser("aday")
    a.add_argument("--adet", type=int, default=40)
    a.add_argument("--market", default="")
    a.add_argument("--kaynak", default="", help="yalniz bu marketin urunleri")
    a.add_argument("--ara", default="", help="adinda bu kelimeler gecenler")
    a.add_argument("--urun", default="", help="virgulle urun kimlikleri")
    g = alt.add_parser("goster")
    g.add_argument("dosya")
    g.add_argument("--esik", type=float, default=2.0,
                   help="bu puanin altindaki adaylar gizlenir")
    u = alt.add_parser("uygula")
    u.add_argument("dosya")
    u.add_argument("karar")
    alt.add_parser("durum")
    arg = p.parse_args()
    {"aday": komut_aday, "goster": komut_goster,
     "uygula": komut_uygula, "durum": komut_durum}[arg.komut](arg)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
