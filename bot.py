"""
UCUZCUM BOTU v6 - COK MARKET
- Migros (Money'e ozel indirimler) + A101 (herkese acik indirimler)
- Her urune: market, kaynak, indirim_turu, fiyat_notu (online), bitis_tarihi
- Onceki fiyati saklar -> bildirim icin dususu tespit eder
- Karsilastirma YOK (barkod yok) - ayri listeler
"""

import json
import os
import time
import base64
import datetime
import urllib.parse
import urllib.request
import urllib.error

FIREBASE_URL = os.environ.get("FIREBASE_URL", "")

BASLIKLAR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}

BEKLEME = 0.4

MIGROS_KAYNAKLARI = [
    {
        "kaynak": "Migros Hemen",
        "liste": "https://www.migros.com.tr/rest/hemen/search/screens/money-indirimli-market-urunleri-dt-5",
        "detay": "https://www.migros.com.tr/rest/hemen/products/screens/{sku}",
    },
    {
        "kaynak": "Sanal Market",
        "liste": "https://www.migros.com.tr/rest/search/screens/migroskop-urunleri-dt-3",
        "detay": "https://www.migros.com.tr/rest/products/screens/{sku}",
    },
]

A101_PROMOSYONLAR = [
    {"kod": "Z110", "ad": "Aldin Aldin"},
    {"kod": "Z100", "ad": "Haftanin Yildizlari"},
]


def tarih_cevir(ms):
    """epoch milisaniyeyi okunabilir tarihe cevirir: 1784321999000 -> '23.07.2026'"""
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")
    except Exception:
        return ""


def istek(url):
    r = urllib.request.Request(url, headers=BASLIKLAR)
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return json.loads(c.read().decode("utf-8"))
    except Exception as e:
        print(f"    [HATA] {type(e).__name__}")
        return None


def tl(kurus):
    return round(kurus / 100, 2) if kurus else 0.0


def onceki_fiyati_al(urun_id):
    try:
        r = urllib.request.Request(f"{FIREBASE_URL}/urunler/{urun_id}.json")
        with urllib.request.urlopen(r, timeout=15) as c:
            eski = json.loads(c.read().decode("utf-8"))
            if eski:
                return eski.get("gecerli_fiyat")
    except Exception:
        pass
    return None


def firebase_yaz(yol, veri):
    r = urllib.request.Request(
        f"{FIREBASE_URL}/{yol}.json",
        data=json.dumps(veri).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return c.status == 200
    except Exception:
        print("    [FIREBASE HATA]")
        return False


def kaydet(urun_id, urun, dusenler):
    eski = onceki_fiyati_al(urun_id)
    urun["onceki_fiyat"] = eski
    if eski and urun["gecerli_fiyat"] < eski:
        dusenler.append((urun["urun_adi"], eski, urun["gecerli_fiyat"], urun["market"]))
        urun["dustu"] = True
    else:
        urun["dustu"] = False
    return firebase_yaz(f"urunler/{urun_id}", urun)


# ============ MIGROS ============

def migros_liste(url):
    veri = istek(url)
    if not veri:
        return []
    try:
        return veri["data"]["searchInfo"]["storeProductInfos"]
    except (KeyError, TypeError):
        return []


def migros_detay(detay_kalibi, sku):
    veri = istek(detay_kalibi.format(sku=sku.lstrip("0")))
    if not veri:
        return None
    return veri.get("data", {}).get("storeProductInfoDTO")


def migros_calis(dusenler):
    yazilan = 0
    for kaynak in MIGROS_KAYNAKLARI:
        print(f"\n--- {kaynak['kaynak']} ---")
        adaylar = migros_liste(kaynak["liste"])
        print(f"Listeden {len(adaylar)} urun")

        for aday in adaylar:
            if not aday.get("discountRate"):
                continue
            sku = aday.get("sku", "")
            uid = str(aday.get("id", ""))
            if not sku or not uid:
                continue

            time.sleep(BEKLEME)
            dto = migros_detay(kaynak["detay"], sku)
            if not dto:
                continue

            reg = dto.get("regularPrice")
            sale = dto.get("salePrice")
            loy = dto.get("loyaltyPrice")
            herkese = bool(sale and reg and sale < reg)
            money = bool(loy and sale and loy < sale)
            if not (herkese or money):
                continue

            gecerli = tl(loy) if money else tl(sale)
            urun = {
                "urun_adi": dto.get("name", "?"),
                "normal_fiyat": tl(reg),
                "herkese_fiyat": tl(sale),
                "money_fiyat": tl(loy),
                "gecerli_fiyat": gecerli,
                "indirim_orani": dto.get("discountRate", 0),
                "indirim_turu": "herkese" if herkese else "money",
                "market": "Migros",
                "kaynak": kaynak["kaynak"],
                "gorsel": (dto.get("images") or [{}])[0].get("urls", {}).get("PRODUCT_LIST", ""),
                "fiyat_notu": "online fiyat",
                "bitis_tarihi": "",
                "guncelleme": int(time.time()),
            }
            if kaydet(f"migros_{uid}", urun, dusenler):
                yazilan += 1
    return yazilan


# ============ A101 ============

def a101_cek(promo_kodu):
    sorgu = {
        "channel": "SLOT",
        "filters": [{"field": "promotionCode", "value": promo_kodu}],
        "from": 0,
        "limit": 60,
    }
    b64 = base64.b64encode(json.dumps(sorgu).encode()).decode()
    url = ("https://rio.a101.com.tr/dbmk89vnr/CALL/Store/search/VS032"
           f"?__culture=tr-TR&__platform=web&data={urllib.parse.quote(b64)}&__isbase64=true")
    veri = istek(url)
    if not veri:
        return []
    return veri.get("results", [])


def a101_calis(dusenler):
    yazilan = 0
    for promo in A101_PROMOSYONLAR:
        print(f"\n--- A101: {promo['ad']} ---")
        urunler = a101_cek(promo["kod"])
        print(f"{len(urunler)} urun")

        for u in urunler:
            p = u.get("price", {})
            normal = p.get("normal")
            indirimli = p.get("discounted")
            if not (normal and indirimli and indirimli < normal):
                continue

            uid = str(u.get("id", ""))
            attrs = u.get("attributes", {})
            promo_bilgi = u.get("promotion") or {}
            urun = {
                "urun_adi": attrs.get("name", "?"),
                "normal_fiyat": tl(normal),
                "herkese_fiyat": tl(indirimli),
                "money_fiyat": tl(indirimli),
                "gecerli_fiyat": tl(indirimli),
                "indirim_orani": p.get("discountRate", 0),
                "indirim_turu": "herkese",
                "market": "A101",
                "kaynak": promo["ad"],
                "gorsel": (u.get("images") or [{}])[-1].get("url", ""),
                "fiyat_notu": "online / kapida fiyat",
                "bitis_tarihi": tarih_cevir(promo_bilgi.get("endDate")),
                "guncelleme": int(time.time()),
            }
            if kaydet(f"a101_{uid}", urun, dusenler):
                yazilan += 1
    return yazilan


# ============ ANA AKIS ============

if __name__ == "__main__":
    if not FIREBASE_URL:
        print("HATA: FIREBASE_URL yok")
        raise SystemExit(1)

    print("Ucuzcum Botu v6 (cok market) basladi.")
    dusenler = []

    print("\n" + "=" * 50)
    print("MIGROS")
    print("=" * 50)
    m = migros_calis(dusenler)

    print("\n" + "=" * 50)
    print("A101")
    print("=" * 50)
    a = a101_calis(dusenler)

    print("\n" + "=" * 50)
    print(f"Bitti. Migros: {m}  |  A101: {a}  |  Toplam: {m + a}")

    if dusenler:
        print(f"\n{len(dusenler)} URUNDE FIYAT DUSUSU:")
        for ad_, e, y, mk in dusenler:
            print(f"  [{mk}] {ad_}: {e} -> {y} TL")
    else:
        print("Bu turda fiyat dususu yok.")
