"""
UCUZCUM BOTU v7 - UC MARKET
- Migros (Money'e ozel)  + A101 (herkese acik)  + BIM (herkese acik)
- Migros/A101: JSON API   |   BIM: HTML kazima (kirilgan, yapi degisirse bozulur)
- Her urune: market, kaynak, indirim_turu, fiyat_notu, bitis_tarihi
- Onceki fiyati saklar -> bildirim icin dususu tespit eder
"""

import json
import os
import re
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
    {"kaynak": "Migros Hemen",
     "liste": "https://www.migros.com.tr/rest/hemen/search/screens/money-indirimli-market-urunleri-dt-5",
     "detay": "https://www.migros.com.tr/rest/hemen/products/screens/{sku}"},
    {"kaynak": "Sanal Market",
     "liste": "https://www.migros.com.tr/rest/search/screens/migroskop-urunleri-dt-3",
     "detay": "https://www.migros.com.tr/rest/products/screens/{sku}"},
]

A101_PROMOSYONLAR = [
    {"kod": "Z110", "ad": "Aldin Aldin"},
    {"kod": "Z100", "ad": "Haftanin Yildizlari"},
]

# BIM: kac guncel aktuel sayfasi gezilsin
BIM_AKTUEL_SAYISI = 3


def tarih_cevir(ms):
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")
    except Exception:
        return ""


def istek_json(url):
    r = urllib.request.Request(url, headers=BASLIKLAR)
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return json.loads(c.read().decode("utf-8"))
    except Exception:
        print("    [HATA] json istek")
        return None


def istek_html(url):
    r = urllib.request.Request(url, headers=BASLIKLAR)
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return c.read().decode("utf-8", errors="ignore")
    except Exception:
        print("    [HATA] html istek")
        return ""


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

def migros_calis(dusenler):
    yazilan = 0
    for kaynak in MIGROS_KAYNAKLARI:
        print(f"\n--- {kaynak['kaynak']} ---")
        veri = istek_json(kaynak["liste"])
        adaylar = []
        if veri:
            try:
                adaylar = veri["data"]["searchInfo"]["storeProductInfos"]
            except (KeyError, TypeError):
                pass
        print(f"Listeden {len(adaylar)} urun")

        for aday in adaylar:
            if not aday.get("discountRate"):
                continue
            sku = aday.get("sku", "")
            uid = str(aday.get("id", ""))
            if not sku or not uid:
                continue

            time.sleep(BEKLEME)
            dveri = istek_json(kaynak["detay"].format(sku=sku.lstrip("0")))
            dto = dveri.get("data", {}).get("storeProductInfoDTO") if dveri else None
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

def a101_calis(dusenler):
    yazilan = 0
    for promo in A101_PROMOSYONLAR:
        print(f"\n--- A101: {promo['ad']} ---")
        sorgu = {"channel": "SLOT",
                 "filters": [{"field": "promotionCode", "value": promo["kod"]}],
                 "from": 0, "limit": 60}
        b64 = base64.b64encode(json.dumps(sorgu).encode()).decode()
        url = ("https://rio.a101.com.tr/dbmk89vnr/CALL/Store/search/VS032"
               f"?__culture=tr-TR&__platform=web&data={urllib.parse.quote(b64)}&__isbase64=true")
        veri = istek_json(url)
        urunler = veri.get("results", []) if veri else []
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


# ============ BIM (HTML kazima) ============

def bim_urunleri_ayikla(html):
    urunler = []
    for blok in html.split('class="product col-xl-3')[1:]:
        ad_m = re.search(r'class="title">([^<]+)</h2>', blok)
        yeni_m = re.search(
            r'quantify">(\d+),?\s*</div>\s*<div class="kusurArea"><span class="number">(\d{2})',
            blok, re.DOTALL)
        if not (ad_m and yeni_m):
            continue
        marka_m = re.search(r'subTitle">([^<]+)</h2>', blok)
        eski_m = re.search(r'strikethrough.*?quantify">([\d.,]+)', blok, re.DOTALL)
        link_m = re.search(r'href="(/aktuel-urunler/[^"]+)"', blok)
        gorsel_m = re.search(r'<img src="(https://cdn[^"]+)"', blok)

        marka = marka_m.group(1).strip() if marka_m else ""
        ad = ad_m.group(1).strip()
        tam_ad = f"{marka} {ad}".strip()
        yeni = float(yeni_m.group(1) + "." + yeni_m.group(2))
        eski = float(eski_m.group(1).replace(".", "").replace(",", ".")) if eski_m else 0.0
        if not (eski and yeni and yeni < eski):
            continue

        # kimlik: urun link'inden (sabit) uret
        kimlik = re.sub(r'\W+', '', link_m.group(1)) if link_m else re.sub(r'\W+', '', tam_ad)
        urunler.append({
            "kimlik": kimlik,
            "ad": tam_ad,
            "eski": eski,
            "yeni": yeni,
            "gorsel": gorsel_m.group(1) if gorsel_m else "",
        })
    return urunler


def bim_calis(dusenler):
    yazilan = 0
    print("\n--- BIM ---")
    ana = istek_html("https://www.bim.com.tr/Categories/100/aktuel-urunler.aspx")
    kodlar = list(dict.fromkeys(re.findall(r'Bim_AktuelTarihKey=(\d+)', ana)))[:BIM_AKTUEL_SAYISI]
    print(f"{len(kodlar)} aktuel sayfasi gezilecek")

    gorulen = set()
    for kod in kodlar:
        url = f"https://www.bim.com.tr/Categories/100/aktuel-urunler.aspx?Bim_AktuelTarihKey={kod}"
        html = istek_html(url)
        urunler = bim_urunleri_ayikla(html)
        print(f"  aktuel {kod}: {len(urunler)} urun")

        for u in urunler:
            if u["kimlik"] in gorulen:
                continue
            gorulen.add(u["kimlik"])

            oran = round((1 - u["yeni"] / u["eski"]) * 100) if u["eski"] else 0
            urun = {
                "urun_adi": u["ad"],
                "normal_fiyat": u["eski"],
                "herkese_fiyat": u["yeni"],
                "money_fiyat": u["yeni"],
                "gecerli_fiyat": u["yeni"],
                "indirim_orani": oran,
                "indirim_turu": "herkese",
                "market": "BIM",
                "kaynak": "Aktuel",
                "gorsel": u["gorsel"],
                "fiyat_notu": "magaza fiyati",
                "bitis_tarihi": "",
                "guncelleme": int(time.time()),
            }
            if kaydet(f"bim_{u['kimlik']}", urun, dusenler):
                yazilan += 1
    return yazilan


# ============ ANA AKIS ============

if __name__ == "__main__":
    if not FIREBASE_URL:
        print("HATA: FIREBASE_URL yok")
        raise SystemExit(1)

    print("Ucuzcum Botu v7 (uc market) basladi.")
    dusenler = []

    print("\n==== MIGROS ====")
    m = migros_calis(dusenler)
    print("\n==== A101 ====")
    a = a101_calis(dusenler)
    print("\n==== BIM ====")
    b = bim_calis(dusenler)

    print("\n" + "=" * 50)
    print(f"Bitti. Migros:{m}  A101:{a}  BIM:{b}  Toplam:{m + a + b}")

    if dusenler:
        print(f"\n{len(dusenler)} URUNDE FIYAT DUSUSU:")
        for ad_, e, y, mk in dusenler:
            print(f"  [{mk}] {ad_}: {e} -> {y} TL")
    else:
        print("Bu turda fiyat dususu yok.")
