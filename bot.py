"""
UCUZCUM BOTU v8 - UC MARKET + BILDIRIM
- Migros + A101 + BIM
- Fiyat dususu tespit edince, kullanici tercihine gore FCM bildirimi gonderir
- Kullanici modlari: "tumu" | "esik" (esik_oran) | "takip" (takip_listesi)

Firebase yapisi (uygulama yazacak, bot okuyacak):
  kullanicilar/{token}/mod           -> "tumu" | "esik" | "takip"
  kullanicilar/{token}/esik_oran     -> int (orn 30)
  kullanicilar/{token}/takip/{urun_id} -> true
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
SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
PROJECT_ID = "ucuzum-4e82f"

BASLIKLAR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
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
BIM_AKTUEL_SAYISI = 3


# ==================== FCM BILDIRIM ====================

def fcm_access_token():
    """Servis hesabiyla FCM icin gecici erisim jetonu uretir."""
    if not SERVICE_ACCOUNT_JSON:
        print("[FCM] Servis hesabi yok, bildirim gonderilemeyecek.")
        return None
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        bilgi = json.loads(SERVICE_ACCOUNT_JSON)
        kimlik = service_account.Credentials.from_service_account_info(
            bilgi, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        kimlik.refresh(Request())
        return kimlik.token
    except Exception as e:
        print(f"[FCM] Jeton uretilemedi: {type(e).__name__} - {e}")
        return None


# ---- Veritabani icin yetkili erisim jetonu (guvenlik kurallari icin) ----
_DB_TOKEN = {"deger": None, "zaman": 0}


def db_access_token():
    """Servis hesabiyla Realtime Database icin yetkili jeton uretir (55 dk onbellek)."""
    if not SERVICE_ACCOUNT_JSON:
        return None
    simdi = time.time()
    if _DB_TOKEN["deger"] and simdi - _DB_TOKEN["zaman"] < 3300:
        return _DB_TOKEN["deger"]
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        bilgi = json.loads(SERVICE_ACCOUNT_JSON)
        kimlik = service_account.Credentials.from_service_account_info(
            bilgi,
            scopes=[
                "https://www.googleapis.com/auth/firebase.database",
                "https://www.googleapis.com/auth/userinfo.email",
            ])
        kimlik.refresh(Request())
        _DB_TOKEN["deger"] = kimlik.token
        _DB_TOKEN["zaman"] = simdi
        return kimlik.token
    except Exception as e:
        print(f"[DB] Jeton uretilemedi: {type(e).__name__} - {e}")
        return None


def db_url(yol):
    """Yetkili veritabani adresi uretir."""
    jeton = db_access_token()
    temel = f"{FIREBASE_URL}/{yol}.json"
    return f"{temel}?access_token={jeton}" if jeton else temel


def fcm_gonder(access_token, cihaz_token, baslik, govde):
    """Tek bir cihaza bildirim gonderir."""
    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"
    veri = {
        "message": {
            "token": cihaz_token,
            "notification": {"title": baslik, "body": govde},
            "android": {
                "priority": "high",
                "notification": {
                    "channel_id": "ucuzcum_indirimler_v2",
                    "notification_priority": "PRIORITY_HIGH",
                }
            }
        }
    }
    r = urllib.request.Request(
        url, data=json.dumps(veri).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return c.status == 200
    except urllib.error.HTTPError as e:
        # 404 = token artik gecersiz (uygulama silinmis olabilir)
        print(f"    [FCM HATA] HTTP {e.code}")
        return False
    except Exception as e:
        print(f"    [FCM HATA] {type(e).__name__}")
        return False


def kullanicilari_al():
    """Firebase'den kayitli kullanicilari (token + tercih) okur."""
    try:
        r = urllib.request.Request(db_url("kullanicilar"))
        with urllib.request.urlopen(r, timeout=15) as c:
            veri = json.loads(c.read().decode("utf-8"))
            return veri if isinstance(veri, dict) else {}
    except Exception:
        return {}


def bildirim_gonderilsin_mi(kullanici, urun, urun_id):
    """Kullanicinin moduna gore bu dususte bildirim almali mi?"""
    mod = kullanici.get("mod", "tumu")
    if mod == "kapali":
        return False
    if mod == "tumu":
        return True
    if mod == "esik":
        esik = kullanici.get("esik_oran", 30)
        return urun.get("indirim_orani", 0) >= esik
    if mod == "takip":
        takip = kullanici.get("takip", {})
        return urun_id in takip
    return False


# ==================== ORTAK ====================

def istek_json(url):
    r = urllib.request.Request(url, headers=BASLIKLAR)
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return json.loads(c.read().decode("utf-8"))
    except Exception:
        return None


def istek_html(url):
    r = urllib.request.Request(url, headers=BASLIKLAR)
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return c.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def tl(kurus):
    return round(kurus / 100, 2) if kurus else 0.0


def onceki_kayit_al(urun_id):
    """Urunun Firebase'deki onceki tam kaydini dondurur (yoksa None)."""
    try:
        r = urllib.request.Request(db_url(f"urunler/{urun_id}"))
        with urllib.request.urlopen(r, timeout=15) as c:
            return json.loads(c.read().decode("utf-8"))
    except Exception:
        return None


def onceki_fiyati_al(urun_id):
    eski = onceki_kayit_al(urun_id)
    return eski.get("gecerli_fiyat") if isinstance(eski, dict) else None


GUN = 86400
GECMIS_PENCERE = 30 * GUN
EN_DUSUK_ICIN_ASGARI_KAYIT = 3


def gecmis_guncelle(eski, urun):
    """Son 30 gunun fiyat gecmisini tutar ve 'en dusuk mu' bilgisini hesaplar."""
    simdi = int(time.time())
    gecmis = []
    if isinstance(eski, dict) and isinstance(eski.get("gecmis"), list):
        gecmis = [x for x in eski["gecmis"]
                  if isinstance(x, dict)
                  and isinstance(x.get("f"), (int, float))
                  and simdi - int(x.get("t", 0)) < GECMIS_PENCERE]

    bugun = simdi // GUN
    fiyat = urun["gecerli_fiyat"]
    bugunku = [x for x in gecmis if int(x.get("t", 0)) // GUN == bugun]
    if bugunku:
        # ayni gun icinde en dusugu sakla, yeni kayit acma
        for x in bugunku:
            x["f"] = min(x["f"], fiyat)
    else:
        gecmis.append({"t": simdi, "f": fiyat})

    gecmis = gecmis[-40:]
    fiyatlar = [x["f"] for x in gecmis]
    en_dusuk = min(fiyatlar) if fiyatlar else fiyat

    en_yuksek = max(fiyatlar) if fiyatlar else fiyat

    urun["gecmis"] = gecmis
    urun["en_dusuk_30g"] = en_dusuk
    urun["en_yuksek_30g"] = en_yuksek
    # Rozet sarti: yeterli veri + su an en dusuk + gecmiste DAHA YUKSEK fiyat gorulmus.
    # Son sart olmazsa fiyati hic degismeyen her urun rozet alir, rozet anlamsizlasir.
    urun["en_dusuk_mu"] = bool(
        len(gecmis) >= EN_DUSUK_ICIN_ASGARI_KAYIT
        and fiyat <= en_dusuk
        and en_yuksek > fiyat)
    # rozet cikiyorsa ne kadar dustugunu de sakla (arayuzde gosterilecek)
    urun["dusus_tutari"] = round(en_yuksek - fiyat, 2) if en_yuksek > fiyat else 0


def firebase_yaz(yol, veri):
    r = urllib.request.Request(
        db_url(yol),
        data=json.dumps(veri).encode("utf-8"),
        method="PUT", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return c.status == 200
    except Exception:
        return False


def firebase_yama(yol, veri):
    """Sadece belirtilen alanlari gunceller (PATCH)."""
    r = urllib.request.Request(
        db_url(yol),
        data=json.dumps(veri).encode("utf-8"),
        method="PATCH", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return c.status == 200
    except Exception:
        return False


def tarih_cevir(ms):
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")
    except Exception:
        return ""


# dususleri toplayacagimiz global liste: (urun_id, urun_dict)
DUSENLER = []
# indirime YENI giren urunler (ilk kez feed'de): (urun_id, urun_dict)
YENI_INDIRIMLER = []


def tr_ara(s):
    """Turkce + aksanli harf duyarli normalize (kelime eslestirme icin)."""
    s = s.lower()
    esle = {
        "ı": "i", "İ": "i", "i̇": "i",
        "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "á": "a", "à": "a", "â": "a", "ä": "a", "å": "a",
        "í": "i", "ì": "i", "î": "i", "ï": "i",
        "ó": "o", "ò": "o", "ô": "o", "õ": "o",
        "ú": "u", "ù": "u", "û": "u",
        "ñ": "n", "ý": "y", "ÿ": "y",
    }
    for a, b in esle.items():
        s = s.replace(a, b)
    return s


def kaydet(urun_id, urun):
    eski_kayit = onceki_kayit_al(urun_id)
    eski = eski_kayit.get("gecerli_fiyat") if isinstance(eski_kayit, dict) else None

    # gecmis + en dusuk rozeti
    gecmis_guncelle(eski_kayit, urun)

    # daha once yapilmis Migros karsilastirmasini koru (gunluk is yeniler)
    if isinstance(eski_kayit, dict):
        for alan in ("migros_normal", "migros_ad", "migros_zaman",
                     "migros_carpan", "migros_esdeger"):
            if eski_kayit.get(alan) is not None:
                urun[alan] = eski_kayit[alan]

    urun["onceki_fiyat"] = eski
    if eski is None:
        # ilk kez indirim feed'inde goruluyor -> "indirime yeni girdi"
        urun["dustu"] = False
        YENI_INDIRIMLER.append((urun_id, dict(urun)))
    elif urun["gecerli_fiyat"] < eski:
        urun["dustu"] = True
        DUSENLER.append((urun_id, dict(urun)))
    else:
        urun["dustu"] = False
    return firebase_yaz(f"urunler/{urun_id}", urun)


# ==================== MARKETLER ====================


def migros_link(dto, uid, kaynak_adi):
    """Migros/Macrocenter urun sayfasi adresi uretir."""
    temel = ("https://www.macrocenter.com.tr"
             if "Macro" in kaynak_adi else "https://www.migros.com.tr")
    slug = dto.get("prettyName") or dto.get("seoUrl") or ""
    if slug:
        slug = str(slug).strip("/")
        return f"{temel}/{slug}"
    return ""


def migros_calis():
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
            reg = dto.get("regularPrice"); sale = dto.get("salePrice"); loy = dto.get("loyaltyPrice")
            herkese = bool(sale and reg and sale < reg)
            money = bool(loy and sale and loy < sale)
            if not (herkese or money):
                continue
            gecerli = tl(loy) if money else tl(sale)
            urun = {
                "urun_adi": dto.get("name", "?"), "normal_fiyat": tl(reg),
                "herkese_fiyat": tl(sale), "money_fiyat": tl(loy), "gecerli_fiyat": gecerli,
                "indirim_orani": dto.get("discountRate", 0),
                "indirim_turu": "herkese" if herkese else "money",
                "market": "Migros", "kaynak": kaynak["kaynak"],
                "gorsel": (dto.get("images") or [{}])[0].get("urls", {}).get("PRODUCT_LIST", ""),
                "link": migros_link(dto, uid, kaynak["kaynak"]),
                "fiyat_notu": "online fiyat", "bitis_tarihi": "", "guncelleme": int(time.time()),
            }
            if kaydet(f"migros_{uid}", urun):
                yazilan += 1
    return yazilan


def a101_calis():
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
            normal = p.get("normal"); indirimli = p.get("discounted")
            if not (normal and indirimli and indirimli < normal):
                continue
            uid = str(u.get("id", ""))
            attrs = u.get("attributes", {})
            promo_bilgi = u.get("promotion") or {}
            urun = {
                "urun_adi": attrs.get("name", "?"), "normal_fiyat": tl(normal),
                "herkese_fiyat": tl(indirimli), "money_fiyat": tl(indirimli),
                "gecerli_fiyat": tl(indirimli), "indirim_orani": p.get("discountRate", 0),
                "indirim_turu": "herkese", "market": "A101", "kaynak": promo["ad"],
                "gorsel": (u.get("images") or [{}])[-1].get("url", ""),
                "link": str(attrs.get("seoUrl") or ""),
                "fiyat_notu": "online / kapida fiyat",
                "bitis_tarihi": tarih_cevir(promo_bilgi.get("endDate")),
                "guncelleme": int(time.time()),
            }
            if kaydet(f"a101_{uid}", urun):
                yazilan += 1
    return yazilan


def bim_ayikla(html):
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
        tam_ad = f"{marka} {ad_m.group(1).strip()}".strip()
        yeni = float(yeni_m.group(1) + "." + yeni_m.group(2))
        eski = float(eski_m.group(1).replace(".", "").replace(",", ".")) if eski_m else 0.0
        if not (eski and yeni and yeni < eski):
            continue
        kimlik = re.sub(r'\W+', '', link_m.group(1)) if link_m else re.sub(r'\W+', '', tam_ad)
        baglanti = ("https://www.bim.com.tr" + link_m.group(1)) if link_m else ""
        urunler.append({"kimlik": kimlik, "ad": tam_ad, "eski": eski, "yeni": yeni,
                        "gorsel": gorsel_m.group(1) if gorsel_m else "",
                        "link": baglanti})
    return urunler


def bim_calis():
    yazilan = 0
    print("\n--- BIM ---")
    ana = istek_html("https://www.bim.com.tr/Categories/100/aktuel-urunler.aspx")
    kodlar = list(dict.fromkeys(re.findall(r'Bim_AktuelTarihKey=(\d+)', ana)))[:BIM_AKTUEL_SAYISI]
    gorulen = set()
    for kod in kodlar:
        html = istek_html(f"https://www.bim.com.tr/Categories/100/aktuel-urunler.aspx?Bim_AktuelTarihKey={kod}")
        urunler = bim_ayikla(html)
        print(f"  aktuel {kod}: {len(urunler)} urun")
        for u in urunler:
            if u["kimlik"] in gorulen:
                continue
            gorulen.add(u["kimlik"])
            oran = round((1 - u["yeni"] / u["eski"]) * 100) if u["eski"] else 0
            urun = {
                "urun_adi": u["ad"], "normal_fiyat": u["eski"], "herkese_fiyat": u["yeni"],
                "money_fiyat": u["yeni"], "gecerli_fiyat": u["yeni"], "indirim_orani": oran,
                "indirim_turu": "herkese", "market": "BIM", "kaynak": "Aktuel",
                "gorsel": u["gorsel"], "fiyat_notu": "magaza fiyati",
                "link": u.get("link", ""),
                "bitis_tarihi": "", "guncelleme": int(time.time()),
            }
            if kaydet(f"bim_{u['kimlik']}", urun):
                yazilan += 1
    return yazilan



# ============ MOPAS (HTML kazima) ============

def mopas_cek_sayfa(sayfa):
    url = f"https://mopas.com.tr/search?q=%3Arelevance%3AdiscountFlag%3Atrue&page={sayfa}"
    return istek_html(url)


def mopas_para(s):
    return float(s.replace(".", "").replace(",", "."))


def mopas_calis():
    yazilan = 0
    print("\n--- MOPAS ---")
    gorulen = set()
    for sayfa in range(0, 5):
        html = mopas_cek_sayfa(sayfa)
        if not html:
            continue
        kartlar = html.split('<div class="card">')
        sayfa_urun = 0
        for k in kartlar[1:]:
            id_m = re.search(r'/p/(\d+)"', k)
            href_m = re.search(r'href="(/[^"]*?/p/\d+)"', k)
            ad_m = re.search(r'class="product-title">([^<]+)<', k)
            oran_m = re.search(r'discount">\s*%(\d+)', k)
            ind_m = re.search(r'sale-price discounted-price">\u20ba([\d.,]+)', k)
            eski_m = re.search(r'old-price">\u20ba([\d.,]+)', k)
            gorsel_m = re.search(r'<img src="(https://cdn[^"]+)"', k)
            if not (id_m and ad_m and ind_m and eski_m):
                continue
            pid = id_m.group(1)
            if pid in gorulen:
                continue
            gorulen.add(pid)
            normal = mopas_para(eski_m.group(1))
            indirimli = mopas_para(ind_m.group(1))
            if not (normal and indirimli and indirimli < normal):
                continue
            urun = {
                "urun_adi": ad_m.group(1).strip(), "normal_fiyat": normal,
                "herkese_fiyat": indirimli, "money_fiyat": indirimli,
                "gecerli_fiyat": indirimli,
                "indirim_orani": int(oran_m.group(1)) if oran_m else 0,
                "indirim_turu": "herkese", "market": "Mopas", "kaynak": "Indirimli",
                "gorsel": gorsel_m.group(1) if gorsel_m else "",
                "link": ("https://mopas.com.tr" + href_m.group(1)) if href_m
                        else f"https://mopas.com.tr/p/{pid}",
                "fiyat_notu": "online fiyat", "bitis_tarihi": "",
                "guncelleme": int(time.time()),
            }
            if kaydet(f"mopas_{pid}", urun):
                yazilan += 1
                sayfa_urun += 1
        print(f"  sayfa {sayfa}: {sayfa_urun} urun")
    return yazilan



# ============ MACROCENTER (Migros altyapisi, JSON) ============

def macrocenter_calis():
    yazilan = 0
    print("\n--- MACROCENTER ---")
    kamp_url = "https://www.macrocenter.com.tr/rest/shopping-lists/placeholder/CAMPAIGN_LIST"
    kveri = istek_json(kamp_url)
    kampanyalar = kveri.get("data", []) if kveri else []
    print(f"{len(kampanyalar)} kampanya taraniyor")
    gorulen = set()
    for kamp in kampanyalar:
        kid = kamp.get("id")
        if not kid:
            continue
        sayfa = 0
        while True:
            url = (f"https://www.macrocenter.com.tr/rest/products/search"
                   f"?shoppinglist-id={kid}&page-size=100&page={sayfa}")
            veri = istek_json(url)
            if not veri:
                break
            data = veri.get("data", {})
            urunler = data.get("storeProductInfos", [])
            for u in urunler:
                reg = u.get("regularPrice")
                shown = u.get("shownPrice")
                oran = u.get("discountRate", 0)
                if not (reg and shown and shown < reg and oran > 0):
                    continue
                uid = str(u.get("id", ""))
                if uid in gorulen:
                    continue
                gorulen.add(uid)
                urun = {
                    "urun_adi": u.get("name", "?"),
                    "normal_fiyat": tl(reg),
                    "herkese_fiyat": tl(shown),
                    "money_fiyat": tl(shown),
                    "gecerli_fiyat": tl(shown),
                    "indirim_orani": oran,
                    "indirim_turu": "herkese",
                    "market": "Macrocenter",
                    "kaynak": kamp.get("name", "")[:40],
                    "gorsel": (u.get("images") or [{}])[0].get("urls", {}).get("PRODUCT_LIST", ""),
                    "link": migros_link(u, uid, "Macro"),
                    "fiyat_notu": "online fiyat",
                    "bitis_tarihi": "",
                    "guncelleme": int(time.time()),
                }
                if kaydet(f"macrocenter_{uid}", urun):
                    yazilan += 1
            pageCount = data.get("pageCount", 1)
            sayfa += 1
            if sayfa >= pageCount or sayfa >= 3:
                break
            time.sleep(BEKLEME)
        time.sleep(BEKLEME)
    return yazilan



ESLESME_DOSYASI = "eslesmeler.json"


def eslesmeleri_yukle():
    """Depoya konan elle eslestirme tablosunu okur (yoksa bos doner)."""
    try:
        with open(ESLESME_DOSYASI, "r", encoding="utf-8") as f:
            tablo = json.load(f)
        if isinstance(tablo, dict):
            print(f"  Elle eslesme tablosu: {len(tablo)} kayit")
            return tablo
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  Eslesme tablosu okunamadi: {type(e).__name__}")
    return {}


def migros_urun_getir(sku):
    """Migros urununu sku ile dogrudan getirir (arama yok, tahmin yok)."""
    adres = f"https://www.migros.com.tr/rest/products/screens/{sku}"
    veri = istek_json(adres)
    if not veri:
        return None
    dto = veri.get("data", {}).get("storeProductInfoDTO") or {}
    normal = dto.get("regularPrice") or dto.get("shownPrice") or 0
    if not normal:
        return None
    return {"ad": dto.get("name", ""), "normal": tl(normal)}


# ==================== MIGROS FIYAT KARSILASTIRMASI ====================
# Gunde bir kez calisir. Migros disi urunleri Migros katalogunda arar,
# YALNIZCA kesin eslesmede normal fiyati kaydeder. Emin degilse hicbir sey yazmaz.

KARS_ARALIK = 20 * 3600          # 20 saatte bir
KARS_BEKLEME = 0.4               # istekler arasi bekleme


def kars_normalize(metin):
    """Kelime eslestirme icin: sadece harf/rakam birakir."""
    return re.sub(r"[^a-z0-9 ]", " ", tr_ara(metin))


def kars_miktar_metni(metin):
    """Miktar okuma icin: virgul, nokta ve carpim isaretini KORUR."""
    return re.sub(r"[^a-z0-9,.x* ]", " ", tr_ara(metin))


def _birim_cevir(sayi, birim):
    if birim == "kg":
        sayi, birim = sayi * 1000, "g"
    elif birim == "gr":
        birim = "g"
    elif birim in ("lt", "l"):
        sayi, birim = sayi * 1000, "ml"
    elif birim == "cl":
        sayi, birim = sayi * 10, "ml"
    return (round(sayi), birim)


def kars_miktar(ad):
    """
    Urun adindan TOPLAM miktari cikarir. Coklu paketleri carpar:
      '3x180 G' -> 540 g   |   '24x12,5 G' -> 300 g   |   '180 G' -> 180 g
    Coklu paketin tekli ile yanlis eslesmesini onler.
    """
    metin = kars_miktar_metni(ad)
    coklu = re.search(
        r"(\d+)\s*[x*]\s*(\d+[.,]?\d*)\s*(kg|gr|g|ml|lt|l|cl)\b", metin)
    if coklu:
        try:
            adet = int(coklu.group(1))
            deger = float(coklu.group(2).replace(",", "."))
        except ValueError:
            return None
        return _birim_cevir(adet * deger, coklu.group(3))

    bulunan = re.findall(r"(\d+[.,]?\d*)\s*(kg|gr|g|ml|lt|l|cl)\b", metin)
    if not bulunan:
        return None
    sayi_metin, birim = bulunan[-1]
    try:
        sayi = float(sayi_metin.replace(",", "."))
    except ValueError:
        return None
    return _birim_cevir(sayi, birim)


def kars_kelimeler(ad, adet=5):
    return [w for w in kars_normalize(ad).split()
            if len(w) >= 3 and not w.isdigit()][:adet]


def migros_katalog_ara(sorgu):
    adres = ("https://www.migros.com.tr/rest/products/search?q="
             + urllib.parse.quote(sorgu))
    veri = istek_json(adres)
    if not veri:
        return []
    return veri.get("data", {}).get("storeProductInfos", [])[:8]


def kesin_eslesme(ad):
    """Sıkı kural: ayni gramaj + ilk kelime (marka) birebir + >=3 ortak kelime."""
    hedef = kars_miktar(ad)
    if not hedef:
        return None
    kelimeler = kars_kelimeler(ad)
    if len(kelimeler) < 2:
        return None

    for sonuc in migros_katalog_ara(" ".join(kelimeler[:4])):
        migros_ad = sonuc.get("name", "")
        if kars_miktar(migros_ad) != hedef:
            continue
        parcalar = set(kars_normalize(migros_ad).split())
        if kelimeler[0] not in parcalar:       # marka birebir gecmeli
            continue
        ortak = len(set(kelimeler) & parcalar)
        yeterli = ortak >= 3 or (len(kelimeler) <= 3 and ortak == len(kelimeler))
        if not yeterli:
            continue
        normal = sonuc.get("regularPrice") or 0
        if not normal:
            continue
        return {"ad": migros_ad, "normal": tl(normal)}
    return None


def karsilastirma_zamani_mi():
    try:
        r = urllib.request.Request(db_url("sistem/son_karsilastirma"))
        with urllib.request.urlopen(r, timeout=15) as c:
            son = json.loads(c.read().decode("utf-8"))
        if not isinstance(son, (int, float)):
            return True
        return (time.time() - son) > KARS_ARALIK
    except Exception:
        return True


def karsilastirma_calis():
    if not karsilastirma_zamani_mi():
        print("Karsilastirma zamani degil (gunde bir calisir).")
        return 0

    print("\n--- MIGROS KARSILASTIRMASI ---")
    try:
        r = urllib.request.Request(db_url("urunler"))
        with urllib.request.urlopen(r, timeout=30) as c:
            urunler = json.loads(c.read().decode("utf-8")) or {}
    except Exception as e:
        print(f"  Urunler okunamadi: {type(e).__name__}")
        return 0

    tablo = eslesmeleri_yukle()

    # Elle onaylanmis urunler gramaj okunamasa da taranir:
    # kullanicinin onayi otomatik gramaj kontrolunden ustundur.
    hedefler = [(k, v) for k, v in urunler.items()
                if isinstance(v, dict) and v.get("market") != "Migros"
                and (k in tablo or kars_miktar(v.get("urun_adi", "")))]
    print(f"  Taranacak urun: {len(hedefler)}")

    bulundu = 0
    elle = 0
    for urun_id, veri in hedefler:
        ad = veri.get("urun_adi", "")
        eslesme = None

        kayit = tablo.get(urun_id)

        # 0) Tabloda "atla" isaretliyse: eslestirme yapma ve varsa
        #    daha once yazilmis karsilastirmayi TEMIZLE.
        if isinstance(kayit, dict) and kayit.get("atla"):
            if veri.get("migros_normal") is not None:
                firebase_yama(f"urunler/{urun_id}", {
                    "migros_normal": None,
                    "migros_ad": None,
                    "migros_carpan": None,
                    "migros_esdeger": None,
                    "migros_zaman": None,
                })
                print(f"  temizlendi: {ad[:45]}")
            continue

        # 1) Elle onaylanmis eslesme varsa onu kullan (kesin, tahmin yok)
        if isinstance(kayit, dict) and kayit.get("sku"):
            try:
                sonuc = migros_urun_getir(kayit["sku"])
            except Exception:
                sonuc = None
            if sonuc:
                # guvenlik: market sku'yu baska urune verdiyse eslesmeyi kullanma
                beklenen = tr_ara(kayit.get("ad", "")).split()
                gelen = set(tr_ara(sonuc["ad"]).split())
                ortak = len([w for w in beklenen if w in gelen])
                if ortak >= max(2, len(beklenen) // 3):
                    try:
                        carpan = float(kayit.get("carpan") or 1)
                    except (TypeError, ValueError):
                        carpan = 1.0
                    if carpan <= 0:
                        carpan = 1.0
                    sonuc["carpan"] = carpan
                    sonuc["esdeger"] = round(sonuc["normal"] * carpan, 2)
                    eslesme = sonuc
                    elle += 1

        # 2) Yoksa siki isim+gramaj kurali
        if eslesme is None:
            try:
                eslesme = kesin_eslesme(ad)
            except Exception:
                eslesme = None
        if eslesme:
            firebase_yama(f"urunler/{urun_id}", {
                "migros_normal": eslesme["normal"],
                "migros_ad": eslesme["ad"],
                "migros_carpan": eslesme.get("carpan", 1),
                "migros_esdeger": eslesme.get("esdeger", eslesme["normal"]),
                "migros_zaman": int(time.time()),
            })
            bulundu += 1
        time.sleep(KARS_BEKLEME)

    firebase_yaz("sistem/son_karsilastirma", int(time.time()))
    print(f"  Kesin eslesme: {bulundu}/{len(hedefler)}  (elle tablodan: {elle})")
    return bulundu


# ==================== BILDIRIM DAGITIMI ====================

def bildirimleri_gonder():
    if not DUSENLER and not YENI_INDIRIMLER:
        print("Bildirim gerektiren degisiklik yok.")
        return

    kullanicilar = kullanicilari_al()
    if not kullanicilar:
        print("Kayitli kullanici yok.")
        return

    access = fcm_access_token()
    if not access:
        print("FCM jetonu alinamadi, bildirim gonderilemiyor.")
        return

    gonderilen = 0
    for anahtar, kullanici in kullanicilar.items():
        if not isinstance(kullanici, dict):
            continue
        # Yeni yapi: kullanicilar/{uid}/token
        # Eski yapi: kullanicilar/{token}  (anahtarin kendisi token)
        token = kullanici.get("token") or anahtar
        if not token:
            continue
        gonderildi = set()

        # 1) Mod bazli dusus bildirimleri (tumu / esik / takip)
        for urun_id, urun in DUSENLER:
            if urun_id in gonderildi:
                continue
            if bildirim_gonderilsin_mi(kullanici, urun, urun_id):
                baslik = f"{urun['market']} indirim!"
                govde = (f"{urun['urun_adi']} {urun['onceki_fiyat']} -> "
                         f"{urun['gecerli_fiyat']} TL (%{urun['indirim_orani']})")
                if fcm_gonder(access, token, baslik, govde):
                    gonderilen += 1
                    gonderildi.add(urun_id)

        # 2) Kelime takibi bildirimleri (moddan bagimsiz)
        kelimeler = kullanici.get("kelimeler")
        if kelimeler:
            if isinstance(kelimeler, dict):
                ham = list(kelimeler.values())
            elif isinstance(kelimeler, list):
                ham = kelimeler
            else:
                ham = []
            kelime_normal = [tr_ara(str(x)) for x in ham if x]

            # yeni indirime girenler + dusenler taranir
            for urun_id, urun in (YENI_INDIRIMLER + DUSENLER):
                if urun_id in gonderildi:
                    continue
                ad_normal = tr_ara(urun.get("urun_adi", ""))
                if any(kn and kn in ad_normal for kn in kelime_normal):
                    baslik = f"Takip: {urun['market']}"
                    govde = (f"{urun['urun_adi']} {urun['gecerli_fiyat']} TL "
                             f"(%{urun['indirim_orani']}) indirimde!")
                    if fcm_gonder(access, token, baslik, govde):
                        gonderilen += 1
                        gonderildi.add(urun_id)

    print(f"Toplam {gonderilen} bildirim gonderildi.")


# ==================== ANA ====================

if __name__ == "__main__":
    if not FIREBASE_URL:
        print("HATA: FIREBASE_URL yok")
        raise SystemExit(1)

    print("Ucuzcum Botu v8 basladi.")
    print("\n==== MIGROS ===="); m = migros_calis()
    print("\n==== A101 ===="); a = a101_calis()
    print("\n==== BIM ===="); b = bim_calis()
    print("\n==== MOPAS ===="); mo = mopas_calis()
    print("\n==== MACROCENTER ===="); mc = macrocenter_calis()

    print("\n" + "=" * 50)
    print(f"Cekilen: Migros:{m}  A101:{a}  BIM:{b}  Mopas:{mo}  Macro:{mc}  Toplam:{m + a + b + mo + mc}")
    print(f"Fiyat dusen: {len(DUSENLER)}  Indirime yeni giren: {len(YENI_INDIRIMLER)}")

    bildirimleri_gonder()

    try:
        karsilastirma_calis()
    except Exception as e:
        print(f"Karsilastirma atlandi: {type(e).__name__}")
    print("\nBot tamamlandi.")
