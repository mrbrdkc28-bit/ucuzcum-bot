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
BIM_AKTUEL_SAYISI = 5      # kac aktuel brosur taranir
MACRO_SAYFA = 10           # Macrocenter kampanya basina sayfa siniri
MOPAS_SAYFA = 12           # Mopas indirim listesi sayfa sayisi


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


# FCM 404 donen (artik gecersiz) cihaz jetonlari; tur sonunda silinir
OLU_JETONLAR = set()


def olu_jetonlari_temizle(kullanicilar):
    """
    Gecersiz jetonlari Firebase'den temizler.
    Yeni yapida sadece 'token' alani silinir; kullanicinin mod, kelime ve
    saat tercihleri korunur, uygulama yeniden acilinca yeni jeton yazar.
    Eski yapida (dugum anahtari jetonun kendisi) kayit tumden silinir.
    """
    if not OLU_JETONLAR:
        return 0
    silinen = 0
    for anahtar, kullanici in kullanicilar.items():
        if not isinstance(kullanici, dict):
            continue
        token = kullanici.get("token")
        if token and token in OLU_JETONLAR:
            firebase_yaz(f"kullanicilar/{anahtar}/token", None)
            silinen += 1
        elif not token and anahtar in OLU_JETONLAR:
            firebase_yaz(f"kullanicilar/{anahtar}", None)
            silinen += 1
    if silinen:
        print(f"[temizlik] {silinen} gecersiz bildirim jetonu silindi")
    return silinen


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
        # 404 = jeton artik gecersiz (uygulama silinmis / yeniden kurulmus).
        # Bu jetonu isaretliyoruz, tur sonunda Firebase'den temizlenecek.
        # 404 = kayit yok, 400 = jeton bicimi gecersiz. Ikisi de olu jeton.
        if e.code in (400, 404):
            OLU_JETONLAR.add(cihaz_token)
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
# 2 kayit yeterli: "gecmiste daha yuksek fiyat gorulmus olma" sarti zaten
# sahte/sabit fiyatli urunleri eliyor. 3 sarti gereksiz gecikme yaratiyordu.
EN_DUSUK_ICIN_ASGARI_KAYIT = 2
URUN_OMRU = 30 * GUN          # bu suredir gorulmeyen urun silinir

# Fiyat gecmisi artik urun kaydinin ICINDE degil, ayri "gecmis" dugumunde.
# Boylece uygulama urunleri indirirken gecmisi indirmez (veri tasarrufu).
GECMIS_HEPSI = {}     # urun_id -> [{"t":..,"f":..}, ...]
GECMIS_DEGISEN = {}   # bu turda degisenler (tek PATCH ile yazilir)
GECMIS_AKTIF = False  # gunde bir kez True olur


def gecmis_yukle():
    """Tum fiyat gecmisini tek istekte okur. Bos ise eski yapidan tasir."""
    global GECMIS_HEPSI
    try:
        r = urllib.request.Request(db_url("gecmis"))
        with urllib.request.urlopen(r, timeout=30) as c:
            veri = json.loads(c.read().decode("utf-8"))
        GECMIS_HEPSI = veri if isinstance(veri, dict) else {}
    except Exception as e:
        print(f"[gecmis] okunamadi: {type(e).__name__}")
        GECMIS_HEPSI = {}

    if not GECMIS_HEPSI:
        GECMIS_HEPSI = gecmis_tasi()

    print(f"[gecmis] {len(GECMIS_HEPSI)} urunun gecmisi yuklendi")


def gecmis_tasi():
    """
    TEK SEFERLIK: eski surumde gecmis, urun kaydinin icinde tutuluyordu.
    Yeni yapiya gecerken o veriyi kaybetmemek icin buraya tasinir.
    Boylece firsat rozetleri sifirlanmaz.
    """
    try:
        r = urllib.request.Request(db_url("urunler"))
        with urllib.request.urlopen(r, timeout=30) as c:
            urunler = json.loads(c.read().decode("utf-8")) or {}
    except Exception as e:
        print(f"[tasima] urunler okunamadi: {type(e).__name__}")
        return {}

    tasinan = {}
    for urun_id, veri in urunler.items():
        if not isinstance(veri, dict):
            continue
        ham = veri.get("gecmis")
        if isinstance(ham, list) and ham:
            tasinan[urun_id] = ham

    if tasinan:
        if firebase_yama("gecmis", tasinan):
            print(f"[tasima] {len(tasinan)} urunun eski gecmisi aktarildi")
        else:
            print("[tasima] aktarim yazilamadi")
    else:
        print("[tasima] aktarilacak eski gecmis bulunamadi")
    return tasinan


def gecmis_yaz():
    """Bu turda degisen gecmisleri tek PATCH ile yazar."""
    if not GECMIS_DEGISEN:
        return
    if firebase_yama("gecmis", GECMIS_DEGISEN):
        print(f"[gecmis] {len(GECMIS_DEGISEN)} urun guncellendi")
    else:
        print("[gecmis] yazilamadi")


def gecmis_guncelle(urun_id, urun):
    """Son 30 gunun fiyat gecmisini tutar ve 'en dusuk mu' bilgisini hesaplar."""
    simdi = int(time.time())
    ham = GECMIS_HEPSI.get(urun_id)
    gecmis = []
    if isinstance(ham, list):
        gecmis = [x for x in ham
                  if isinstance(x, dict)
                  and isinstance(x.get("f"), (int, float))
                  and simdi - int(x.get("t", 0)) < GECMIS_PENCERE]

    bugun = simdi // GUN
    fiyat = urun["gecerli_fiyat"]
    bugunku = [x for x in gecmis if int(x.get("t", 0)) // GUN == bugun]
    if bugunku:
        for x in bugunku:
            x["f"] = min(x["f"], fiyat)
    else:
        gecmis.append({"t": simdi, "f": fiyat})

    gecmis = gecmis[-40:]
    GECMIS_DEGISEN[urun_id] = gecmis
    fiyatlar = [x["f"] for x in gecmis]
    en_dusuk = min(fiyatlar) if fiyatlar else fiyat

    en_yuksek = max(fiyatlar) if fiyatlar else fiyat

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
# Ayni turda ayni urunun iki kaynaktan (orn Migros Hemen + Sanal Market)
# gelmesini onlemek icin gorulen imzalar: (ad|market|fiyat)
YAZILAN_IMZALAR = set()
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



def birim_fiyat_hesapla(urun):
    """
    Urun adindaki gramajdan birim fiyat cikarir.
    500 g 45 TL -> 90,00 TL/kg  |  1,5 L 12 TL -> 8,00 TL/L
    Gramaj okunamayan urunlerde (karpuz, kiyma kg) None doner.
    """
    try:
        olcu = kars_miktar(urun.get("urun_adi", ""))
    except Exception:
        return None, None
    fiyat = urun.get("gecerli_fiyat") or 0
    if not olcu or not fiyat:
        return None, None
    deger, birim = olcu
    if not deger or deger <= 0:
        return None, None
    if birim == "g":
        return round(fiyat * 1000.0 / deger, 2), "TL/kg"
    if birim == "ml":
        return round(fiyat * 1000.0 / deger, 2), "TL/L"
    if birim == "adet":
        return round(fiyat / deger, 2), "TL/adet"
    return None, None



def kaydet(urun_id, urun):
    # Cift kayit engeli: ayni ad + market + fiyat bu turda yazildiysa atla
    imza = (tr_ara(urun.get("urun_adi", "")).strip(),
            urun.get("market", ""),
            urun.get("gecerli_fiyat", 0))
    if imza in YAZILAN_IMZALAR:
        return False
    YAZILAN_IMZALAR.add(imza)

    eski_kayit = onceki_kayit_al(urun_id)
    eski = eski_kayit.get("gecerli_fiyat") if isinstance(eski_kayit, dict) else None

    if GECMIS_AKTIF:
        # gunluk gecmis kaydi + en dusuk rozeti yeniden hesaplanir
        gecmis_guncelle(urun_id, urun)
    elif isinstance(eski_kayit, dict):
        # gecmis bu turda islenmiyor: mevcut rozet bilgilerini oldugu gibi koru
        for alan in ("en_dusuk_30g", "en_yuksek_30g", "en_dusuk_mu",
                     "dusus_tutari"):
            if eski_kayit.get(alan) is not None:
                urun[alan] = eski_kayit[alan]

    # daha once yapilmis Migros karsilastirmasini koru (gunluk is yeniler)
    if isinstance(eski_kayit, dict):
        for alan in ("karsilastirma", "karsilastirma_link", "en_ucuz_market",
                     "migros_normal", "migros_ad", "migros_zaman",
                     "migros_carpan", "migros_esdeger"):
            if eski_kayit.get(alan) is not None:
                urun[alan] = eski_kayit[alan]

    # Birim fiyat (birim basina karsilastirma icin)
    bf, bm = birim_fiyat_hesapla(urun)
    if bf:
        urun["birim_fiyat"] = bf
        urun["birim_metni"] = bm

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


def gunluk_isler_gerekli_mi():
    """Fiyat gecmisi ve temizlik gunde bir kez yapilir."""
    bugun = int(time.time()) // GUN
    try:
        r = urllib.request.Request(db_url("sistem/gecmis_gunu"))
        with urllib.request.urlopen(r, timeout=15) as c:
            son = json.loads(c.read().decode("utf-8"))
        return not isinstance(son, (int, float)) or int(son) != bugun
    except Exception:
        return True


def gunluk_isler_bitti():
    firebase_yaz("sistem/gecmis_gunu", int(time.time()) // GUN)


def cift_kayitlari_temizle():
    """
    Ayni urun birden fazla kaynaktan yazilmis olabilir
    (orn Migros Hemen + Sanal Market). Ayni ad+market+fiyat
    tasiyan kayitlardan en tazesini birak, digerlerini sil.
    """
    try:
        r = urllib.request.Request(db_url("urunler"))
        with urllib.request.urlopen(r, timeout=30) as c:
            urunler = json.loads(c.read().decode("utf-8")) or {}
    except Exception as e:
        print(f"[cift] okunamadi: {type(e).__name__}")
        return 0

    gruplar = {}
    for uid, v in urunler.items():
        if not isinstance(v, dict):
            continue
        imza = (tr_ara(v.get("urun_adi", "")).strip(),
                v.get("market", ""),
                v.get("gecerli_fiyat", 0))
        gruplar.setdefault(imza, []).append((uid, v.get("guncelleme") or 0))

    silinen = 0
    for imza, liste in gruplar.items():
        if len(liste) < 2:
            continue
        # en tazeyi koru, gerisini sil
        liste.sort(key=lambda x: x[1], reverse=True)
        for uid, _ in liste[1:]:
            try:
                for yol in (f"urunler/{uid}", f"gecmis/{uid}"):
                    istek = urllib.request.Request(db_url(yol), method="DELETE")
                    urllib.request.urlopen(istek, timeout=15)
                silinen += 1
            except Exception:
                pass

    if silinen:
        print(f"[cift] {silinen} tekrar eden kayit silindi")
    return silinen



# ---- Market sagligi: ardisik sifir sayaci ----
ALARM_ESIGI = 3          # bir market bu kadar tur ust uste 0 donerse alarm


def sifir_sayaclarini_guncelle(sayimlar):
    """
    Her market icin ardisik sifir sayisini Firebase'de tutar.
    Urun gelince sayac sifirlanir. Esigi asanlarin listesini dondurur.
    Tek turluk gecici sifirlarda yanlis alarm vermemek icin.
    """
    try:
        r = urllib.request.Request(db_url("sistem/sifir_sayaci"))
        with urllib.request.urlopen(r, timeout=15) as c:
            mevcut = json.loads(c.read().decode("utf-8"))
        if not isinstance(mevcut, dict):
            mevcut = {}
    except Exception:
        mevcut = {}

    yeni = {}
    alarm = []
    for ad, sayi in sayimlar.items():
        if sayi and sayi > 0:
            yeni[ad] = 0
            continue
        try:
            onceki = int(mevcut.get(ad) or 0)
        except (TypeError, ValueError):
            onceki = 0
        yeni[ad] = onceki + 1
        if yeni[ad] >= ALARM_ESIGI:
            alarm.append(f"{ad} ({yeni[ad]} tur)")
        else:
            print(f"[not] {ad} bu turda 0 urun dondurdu "
                  f"({yeni[ad]}/{ALARM_ESIGI})")

    firebase_yaz("sistem/sifir_sayaci", yeni)
    return alarm


def eski_urunleri_temizle():
    """Uzun suredir indirimde gorulmeyen urunleri ve gecmislerini siler."""
    try:
        r = urllib.request.Request(db_url("urunler"))
        with urllib.request.urlopen(r, timeout=30) as c:
            urunler = json.loads(c.read().decode("utf-8")) or {}
    except Exception as e:
        print(f"[temizlik] okunamadi: {type(e).__name__}")
        return 0

    simdi = int(time.time())
    silinecek = [k for k, v in urunler.items()
                 if isinstance(v, dict)
                 and simdi - int(v.get("guncelleme") or 0) > URUN_OMRU]

    for urun_id in silinecek:
        try:
            for yol in (f"urunler/{urun_id}", f"gecmis/{urun_id}"):
                istek = urllib.request.Request(db_url(yol), method="DELETE")
                urllib.request.urlopen(istek, timeout=15)
        except Exception:
            pass

    print(f"[temizlik] {len(silinecek)} eski urun silindi "
          f"({len(urunler)} kayittan)")
    return len(silinecek)


# ==================== MARKETLER ====================


def market_urun_link(dto, uid, kaynak_adi="", yedek=None):
    """
    Migros/Macrocenter urun sayfasi adresi.
    prettyName "urun-adi-p-6d2d6a" bicimindedir ve tek basina yeterlidir.
    Detay kaydinda yoksa liste kaydindan (yedek) alinir; o da yoksa
    bos link yerine arama sayfasina dusuruluyor.
    """
    temel = ("https://www.macrocenter.com.tr"
             if "Macro" in kaynak_adi else "https://www.migros.com.tr")
    yedek = yedek or {}
    slug = (dto.get("prettyName") or yedek.get("prettyName")
            or dto.get("seoUrl") or "")
    if slug:
        return f"{temel}/{str(slug).strip('/')}"
    ad = dto.get("name") or yedek.get("name") or ""
    if ad:
        return f"{temel}/arama?q=" + urllib.parse.quote(ad)
    return temel


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
                "link": market_urun_link(dto, uid, kaynak["kaynak"], aday),
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
    for sayfa in range(0, MOPAS_SAYFA):
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
                    "link": market_urun_link(u, uid, "Macro"),
                    "fiyat_notu": "online fiyat",
                    "bitis_tarihi": "",
                    "guncelleme": int(time.time()),
                }
                if kaydet(f"macrocenter_{uid}", urun):
                    yazilan += 1
            pageCount = data.get("pageCount", 1)
            sayfa += 1
            if sayfa >= pageCount or sayfa >= MACRO_SAYFA:
                break
            time.sleep(BEKLEME)
        time.sleep(BEKLEME)
    return yazilan


# ============ IDEAL (JSON API) ============
# Ana sayfa API'si vitrin/firsat/indirim kaynaklarini birlikte veriyor.
# Sadece list_price'i olan (yani gercekten indirimli) urunler alinir.

def ideal_calis():
    yazilan = 0
    print("\n--- IDEAL ---")
    basliklar = {
        "User-Agent": BASLIKLAR["User-Agent"],
        "Accept": "application/json",
        "Referer": "https://www.ideal.com.tr/",
        "Origin": "https://www.ideal.com.tr",
    }
    try:
        istek = urllib.request.Request(
            "https://www.ideal.com.tr/api/homepage", headers=basliklar)
        with urllib.request.urlopen(istek, timeout=20) as c:
            veri = json.loads(c.read().decode("utf-8"))
    except Exception as e:
        print(f"  Ideal alinamadi: {type(e).__name__}")
        return 0

    data = veri.get("data", {})
    gorulen = set()
    for kaynak_ad, liste in data.items():
        if not isinstance(liste, list):
            continue
        for u in liste:
            if not isinstance(u, dict) or "price" not in u:
                continue
            liste_fiyat = u.get("list_price")
            if not liste_fiyat:          # indirimsiz urunu alma
                continue
            try:
                normal = float(liste_fiyat)
                indirimli = float(u["price"])
            except (TypeError, ValueError):
                continue
            if not (normal and indirimli and indirimli < normal):
                continue

            uid = str(u.get("id", ""))
            if not uid or uid in gorulen:
                continue
            gorulen.add(uid)

            oran = round((1 - indirimli / normal) * 100)
            gorsel = u.get("image", "")
            yol = u.get("url", "")
            link = ("https://www.ideal.com.tr" + yol) if yol.startswith("/") else yol
            urun = {
                "urun_adi": u.get("title", "?"),
                "normal_fiyat": normal,
                "herkese_fiyat": indirimli,
                "money_fiyat": indirimli,
                "gecerli_fiyat": indirimli,
                "indirim_orani": oran,
                "indirim_turu": "herkese",
                "market": "Ideal",
                "kaynak": kaynak_ad,
                "gorsel": gorsel,
                "link": link,
                "fiyat_notu": "online fiyat",
                "bitis_tarihi": "",
                "guncelleme": int(time.time()),
            }
            if kaydet(f"ideal_{uid}", urun):
                yazilan += 1
    print(f"  {yazilan} urun")
    return yazilan


# ============ OZDILEK (JSON arama API) ============
# 12.000+ urunluk katalogu var. Indirimli olanlari (price < listPrice)
# sayfalayarak toplariz. hasDiscount alani indirim isaretidir.

OZDILEK_TEMEL = ("https://api.ozdilekteyim.com/rest/v2/market-gecit-store")
OZDILEK_SAYFA = 30         # 100'luk sayfa; ilk 3000 urun taranir


def ozdilek_calis():
    yazilan = 0
    print("\n--- OZDILEK ---")
    basliklar = {
        "User-Agent": BASLIKLAR["User-Agent"],
        "Accept": "application/json",
        "Referer": "https://www.ozdilekteyim.com/",
        "Origin": "https://www.ozdilekteyim.com",
    }
    gorulen = set()
    for sayfa in range(OZDILEK_SAYFA):
        url = (f"{OZDILEK_TEMEL}/products/search?query=:relevance"
               f"&pageSize=100&currentPage={sayfa}&lang=tr&curr=TRY")
        try:
            istek = urllib.request.Request(url, headers=basliklar)
            with urllib.request.urlopen(istek, timeout=20) as c:
                veri = json.loads(c.read().decode("utf-8"))
        except Exception as e:
            if sayfa == 0:
                print(f"  Ozdilek alinamadi: {type(e).__name__}")
            break

        urunler = veri.get("products", [])
        if not urunler:
            break

        for u in urunler:
            fiyat_obj = u.get("price") or {}
            liste_obj = u.get("listPrice") or {}
            try:
                indirimli = float(fiyat_obj.get("value"))
                normal = float(liste_obj.get("value"))
            except (TypeError, ValueError):
                continue
            if not (normal and indirimli and indirimli < normal):
                continue

            uid = str(u.get("code") or u.get("id") or "")
            if not uid or uid in gorulen:
                continue
            gorulen.add(uid)

            oran = round((1 - indirimli / normal) * 100)
            gorsel = ""
            imgs = u.get("images") or []
            if imgs:
                g0 = imgs[0]
                gorsel = g0.get("url", "") if isinstance(g0, dict) else ""
                if gorsel and gorsel.startswith("/"):
                    gorsel = "https://www.ozdilekteyim.com" + gorsel
            link = ozdilek_link(u, u.get("name", ""))

            urun = {
                "urun_adi": u.get("name", "?"),
                "normal_fiyat": normal,
                "herkese_fiyat": indirimli,
                "money_fiyat": indirimli,
                "gecerli_fiyat": indirimli,
                "indirim_orani": oran,
                "indirim_turu": "herkese",
                "market": "Ozdilek",
                "kaynak": "Indirimli",
                "gorsel": gorsel,
                "link": link,
                "fiyat_notu": "online fiyat",
                "bitis_tarihi": "",
                "guncelleme": int(time.time()),
            }
            if kaydet(f"ozdilek_{uid}", urun):
                yazilan += 1
        time.sleep(BEKLEME)

    print(f"  {yazilan} urun")
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
    normal = satis_fiyati(dto)
    if not normal:
        return None
    return {"ad": dto.get("name", ""), "normal": tl(normal),
            "link": migros_link(dto)}


# ==================== MIGROS FIYAT KARSILASTIRMASI ====================
# Gunde bir kez calisir. Migros disi urunleri Migros katalogunda arar,
# YALNIZCA kesin eslesmede normal fiyati kaydeder. Emin degilse hicbir sey yazmaz.

KARS_ARALIK = 20 * 3600          # 20 saatte bir
KARS_BEKLEME = 0.2                # istekler arasi bekleme (3 arama/urun)


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

    # Cozulemeyen carpan kalibi varsa miktari OKUNAMADI say.
    # Ornek: "Firsat Paketi 3x1 180 G" -> gercekte 3 x 180 g = 540 g, ama
    # kalip "3x1" oldugu icin ustteki coklu paket kurali tutmuyor. Boyle
    # adlarda "180 g" okumak, coklu paketi tekli sanmaya yol acar ve
    # karsilastirmada dort kat hatali fiyat gosterir. Riskli olani atlamak
    # yanlis eslestirmekten iyidir.
    if re.search(r"\b\d+\s*[x*]\s*\d+", metin):
        return None

    bulunan = re.findall(r"(\d+[.,]?\d*)\s*(kg|gr|g|ml|lt|l|cl)\b", metin)
    if not bulunan:
        # Gramaj/hacim yoksa ADET ifadesine bak: 10'lu, 12 li, 30 lu, 62 li
        # (yumurta, ped, bez, kagit havlu, cay posedi gibi urunler)
        adet_m = re.search(r"\b(\d+)\s*(?:li|lu|lı|lü|'?li|'?lu)\b", metin)
        if adet_m:
            try:
                return (int(adet_m.group(1)), "adet")
            except ValueError:
                return None
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
        normal = satis_fiyati(sonuc)
        if not normal:
            continue
        return {"ad": migros_ad, "normal": tl(normal),
                "link": migros_link(sonuc, migros_ad)}
    return None


def ozdilek_katalog_ara(sorgu):
    """Ozdilek arama ucundan sonuc listesi (karsilastirma icin)."""
    basliklar = {
        "User-Agent": BASLIKLAR["User-Agent"],
        "Accept": "application/json",
        "Referer": "https://www.ozdilekteyim.com/",
        "Origin": "https://www.ozdilekteyim.com",
    }
    url = (f"{OZDILEK_TEMEL}/products/search?query="
           + urllib.parse.quote(sorgu)
           + "&pageSize=8&currentPage=0&lang=tr&curr=TRY")
    try:
        istek = urllib.request.Request(url, headers=basliklar)
        with urllib.request.urlopen(istek, timeout=15) as c:
            veri = json.loads(c.read().decode("utf-8"))
        return veri.get("products", [])[:8]
    except Exception:
        return []


def ozdilek_kesin_eslesme(ad):
    """Ozdilek katalogunda ayni gramaj + marka + >=3 ortak kelimeli urun."""
    hedef = kars_miktar(ad)
    if not hedef:
        return None
    kelimeler = kars_kelimeler(ad)
    if len(kelimeler) < 2:
        return None
    for u in ozdilek_katalog_ara(" ".join(kelimeler[:4])):
        oz_ad = u.get("name", "")
        if kars_miktar(oz_ad) != hedef:
            continue
        parcalar = set(kars_normalize(oz_ad).split())
        if kelimeler[0] not in parcalar:
            continue
        ortak = len(set(kelimeler) & parcalar)
        yeterli = ortak >= 3 or (len(kelimeler) <= 3 and ortak == len(kelimeler))
        if not yeterli:
            continue
        fiyat = (u.get("price") or {}).get("value")
        liste = (u.get("listPrice") or {}).get("value")
        deger = fiyat or liste          # kasada odenen fiyat
        if not deger:
            continue
        return {"ad": oz_ad, "normal": round(float(deger), 2),
                "link": ozdilek_link(u, oz_ad)}
    return None


def macro_katalog_ara(sorgu):
    """Macrocenter arama ucundan sonuc listesi (Migros ile ayni yapida)."""
    adres = ("https://www.macrocenter.com.tr/rest/products/search?q="
             + urllib.parse.quote(sorgu))
    veri = istek_json(adres)
    if not veri:
        return []
    return veri.get("data", {}).get("storeProductInfos", [])[:8]


def macro_kesin_eslesme(ad):
    """Macrocenter katalogunda ayni gramaj + marka + >=3 ortak kelimeli urun."""
    hedef = kars_miktar(ad)
    if not hedef:
        return None
    kelimeler = kars_kelimeler(ad)
    if len(kelimeler) < 2:
        return None
    for sonuc in macro_katalog_ara(" ".join(kelimeler[:4])):
        m_ad = sonuc.get("name", "")
        if kars_miktar(m_ad) != hedef:
            continue
        parcalar = set(kars_normalize(m_ad).split())
        if kelimeler[0] not in parcalar:
            continue
        ortak = len(set(kelimeler) & parcalar)
        yeterli = ortak >= 3 or (len(kelimeler) <= 3 and ortak == len(kelimeler))
        if not yeterli:
            continue
        normal = satis_fiyati(sonuc)
        if not normal:
            continue
        return {"ad": m_ad, "normal": tl(normal),
                "link": macro_link(sonuc, m_ad)}
    return None



# ---- Karsilastirma icin market urun adresi ----
def migros_link(dto, ad=""):
    """Migros/Macrocenter ayni altyapi: /{prettyName} (ek kimlik gerekmez)"""
    return _mig_temelli("https://www.migros.com.tr", dto, ad)


def macro_link(dto, ad=""):
    return _mig_temelli("https://www.macrocenter.com.tr", dto, ad)


def _mig_temelli(temel, dto, ad):
    # prettyName ZATEN "-p-6d2d6a" ekini tasiyor; ustune sku eklemek
    # gecersiz adres uretiyordu (.../...-p-6d2d6a-p-07155050).
    guzel = (dto.get("prettyName") or "").strip("/")
    if guzel:
        return f"{temel}/{guzel}"
    aranan = ad or dto.get("name", "")
    if aranan:
        return f"{temel}/arama?q=" + urllib.parse.quote(aranan)
    return temel


def ozdilek_link(dto, ad=""):
    # customUrl basta egik cizgi OLMADAN geliyor: "market/urun-adi".
    # Alan adini her durumda basa ekliyoruz.
    yol = (dto.get("url") or dto.get("customUrl") or "").strip()
    if yol.startswith("http"):
        return yol
    if yol:
        return "https://www.ozdilekteyim.com/" + yol.lstrip("/")
    aranan = ad or dto.get("name", "")
    if aranan:
        return ("https://www.ozdilekteyim.com/search?text="
                + urllib.parse.quote(aranan))
    return "https://www.ozdilekteyim.com/"


# ---- Karsilastirmada kullanilacak fiyat ----
def satis_fiyati(dto):
    """
    Karsilastirma KASADA ODENEN fiyati gosterir (indirimliyse indirimli).
    Migros/Macrocenter: shownPrice = satis fiyati, regularPrice = ustu cizili.
    """
    reg = dto.get("regularPrice") or 0
    shown = dto.get("shownPrice") or 0
    if shown and (not reg or shown <= reg):
        return shown
    return reg


# ---- Elle eslesme tablosu okuma (eski + yeni bicim) ----

def elle_esdeger(sonuc, kaynak_ad, elle_kayit):
    """
    Elle eslenen urunun gramaji kaynak urunden farkliysa fiyati esdegere
    cevirir. Ornek: 250 g urun 500 g'lik urune eslenmisse fiyat x2 olur.
    Boylece kullanicinin bilerek yaptigi farkli-gramaj eslesmeleri
    karsilastirmada dogru gorunur.
    """
    if not sonuc:
        return sonuc
    try:
        carpan = kars_carpan_hesapla(
            kaynak_ad, sonuc.get("ad", ""), (elle_kayit or {}).get("carpan"))
    except Exception:
        carpan = 1.0
    if not carpan or carpan == 1.0:
        return sonuc
    sonuc["carpan"] = carpan
    sonuc["normal"] = round(sonuc["normal"] * carpan, 2)
    return sonuc



def elle_engelli(kayit, market):
    """
    Kullanici eslestirme aracinda o urun icin "bu markette yok" dediyse
    True doner. Bu durumda OTOMATIK eslestirme de yapilmaz.

    Onceden bu isaret yalnizca aracin ayni soruyu tekrar sormamasi icin
    kullaniliyordu; bot yine otomatik eslestirdigi icin kullanicinin
    sildigi hatali eslesme her turda geri geliyordu.
    """
    if not isinstance(kayit, dict):
        return False
    return bool(kayit.get(market + "_yok"))


def tablo_market(kayit, market):
    """
    Eski bicim: {"sku": ..., "ad": ..., "carpan": ...}  -> Migros
    Yeni bicim: {"migros": {...}, "ozdilek": {...}, "macro": {...}}
    """
    if not isinstance(kayit, dict):
        return None
    alt = kayit.get(market)
    if isinstance(alt, dict) and alt.get("kod"):
        return alt
    if market == "migros" and kayit.get("sku"):
        return {"kod": str(kayit["sku"]), "ad": kayit.get("ad", ""),
                "carpan": kayit.get("carpan")}
    return None


def ozdilek_fiyat_getir(kod, ad):
    """Elle eslenen Ozdilek urununun guncel fiyatini getirir."""
    for u in ozdilek_katalog_ara(ad):
        if str(u.get("code")) != str(kod):
            continue
        fiyat = (u.get("price") or {}).get("value")
        liste = (u.get("listPrice") or {}).get("value")
        deger = fiyat or liste          # kasada odenen fiyat
        if not deger:
            return None
        return {"ad": u.get("name", ""),
                "normal": round(float(deger), 2),
                "link": ozdilek_link(u)}
    return None


def macro_fiyat_getir(kod, ad):
    """Elle eslenen Macrocenter urununun guncel fiyatini getirir."""
    for s in macro_katalog_ara(ad):
        if str(s.get("sku") or s.get("id")) != str(kod):
            continue
        normal = satis_fiyati(s)
        if not normal:
            return None
        return {"ad": s.get("name", ""), "normal": tl(normal),
                "link": macro_link(s)}
    return None


# ============ CARREFOUR (laptop betiginin yukledigi dosyadan) ============
# carrefour.py laptopta calisip carrefour.json'i bu depoya yukluyor.
# Bot dosyayi okur; 24 saatten eskiyse yazmaz (laptop calismamis demektir),
# boylece bayat indirim gosterilmez.

CARREFOUR_DOSYASI = "carrefour.json"
CARREFOUR_OMRU = 24 * 3600        # bu yastan eski veri yazilmaz
CARREFOUR_ALARM_YASI = 48 * 3600  # bu yastan eskiyse uyari verilir
CARREFOUR_KATALOG = {}            # {id: {"a": ad, "f": fiyat, "l": link}}
CARREFOUR_VERI_YASI = None        # saniye; dosya yoksa None


def carrefour_calis():
    global CARREFOUR_KATALOG, CARREFOUR_VERI_YASI
    print("\n--- CARREFOUR ---")
    try:
        with open(CARREFOUR_DOSYASI, "r", encoding="utf-8") as f:
            paket = json.load(f)
    except FileNotFoundError:
        print("  carrefour.json yok, atlaniyor")
        return 0
    except Exception as e:
        print(f"  okunamadi: {type(e).__name__}")
        return 0

    CARREFOUR_VERI_YASI = int(time.time()) - int(paket.get("toplama_zamani") or 0)
    print(f"  veri yasi: {CARREFOUR_VERI_YASI // 3600} saat "
          f"({paket.get('kategori_sayisi', '?')} kategori)")
    if CARREFOUR_VERI_YASI > CARREFOUR_OMRU:
        print("  24 saatten eski — yazilmiyor (laptop calismamis)")
        return 0

    CARREFOUR_KATALOG = paket.get("katalog") or {}
    urun_kayitlari = paket.get("urunler") or {}

    yazilan = 0
    for uid, v in urun_kayitlari.items():
        try:
            eski = float(v.get("e") or 0)
            yeni = float(v.get("y") or 0)
        except (TypeError, ValueError):
            continue
        if not (eski and yeni and yeni < eski):
            continue
        kart = bool(v.get("k"))
        urun = {
            "urun_adi": v.get("a", "?"),
            "normal_fiyat": eski,
            "herkese_fiyat": eski if kart else yeni,
            "money_fiyat": yeni,
            "gecerli_fiyat": yeni,
            "indirim_orani": round((1 - yeni / eski) * 100),
            "indirim_turu": "money" if kart else "herkese",
            "market": "Carrefour",
            "kaynak": v.get("c", ""),
            "gorsel": v.get("g", ""),
            "link": v.get("l", ""),
            "fiyat_notu": "CarrefourSA Kart fiyati" if kart else "online fiyat",
            "bitis_tarihi": "",
            "guncelleme": int(time.time()),
        }
        if kaydet(f"carrefour_{uid}", urun):
            yazilan += 1

    print(f"  {yazilan} urun yazildi, karsilastirma katalogu "
          f"{len(CARREFOUR_KATALOG)} urun")
    return yazilan



def carrefour_fiyat_getir(kod):
    """Elle eslenen Carrefour urununu yerel katalogdan bulur (ag istegi yok)."""
    v = (CARREFOUR_KATALOG or {}).get(str(kod))
    if not isinstance(v, dict):
        return None
    fiyat = v.get("f")
    if not fiyat:
        return None
    return {"ad": v.get("a", ""), "normal": round(float(fiyat), 2),
            "link": v.get("l", "")}


def carrefour_kesin_eslesme(ad):
    """Yerel katalogda siki eslesme arar. AG ISTEGI YOK, dosyadan bakar."""
    if not CARREFOUR_KATALOG:
        return None
    hedef = kars_miktar(ad)
    if not hedef:
        return None
    kelimeler = kars_kelimeler(ad)
    if len(kelimeler) < 2:
        return None
    kume = set(kelimeler)
    for uid, v in CARREFOUR_KATALOG.items():
        c_ad = v.get("a", "")
        if not c_ad or kars_miktar(c_ad) != hedef:
            continue
        parcalar = set(kars_normalize(c_ad).split())
        if kelimeler[0] not in parcalar:
            continue
        ortak = len(kume & parcalar)
        yeterli = ortak >= 3 or (len(kelimeler) <= 3 and ortak == len(kelimeler))
        if not yeterli:
            continue
        fiyat = v.get("f")
        if not fiyat:
            continue
        return {"ad": c_ad, "normal": round(float(fiyat), 2),
                "link": v.get("l", "")}
    return None


# ---- Karsilastirma guvenlik kontrolleri ----
KARS_UST_ORAN = 3.0    # migros esdegeri bizim fiyatin 3 katindan fazlaysa suphe
KARS_ALT_ORAN = 0.33   # ucte birinden azsa da suphe


def kars_carpan_hesapla(kaynak_ad, migros_ad, kayitli):
    """
    Carpani ONCELIKLE urun adlarindan hesaplar.
    Adlardan okunamazsa elle kaydedilen degeri kullanir.
    Boylece elle girilen hatali carpanlar kendini duzeltir.
    """
    a = kars_miktar(kaynak_ad)
    b = kars_miktar(migros_ad)
    if a and b and a[1] == b[1] and b[0] > 0:
        oran = a[0] / b[0]
        if abs(oran - round(oran)) < 0.02:
            oran = float(round(oran))
        return round(oran, 3)
    try:
        deger = float(kayitli or 1)
        return deger if deger > 0 else 1.0
    except (TypeError, ValueError):
        return 1.0


def kars_makul_mu(esdeger, bizim_fiyat, ad=""):
    """
    Akil testi: absurt farklari ekrana hic cikarma.
    Ornegin Migros agirlikli urunlerde kilo fiyati dondurebiliyor;
    o durumda 70 TL'lik urun 4500 TL gorunur. Boyle kayitlar elenir.
    """
    if not bizim_fiyat or bizim_fiyat <= 0 or not esdeger or esdeger <= 0:
        return False
    oran = esdeger / bizim_fiyat
    if oran > KARS_UST_ORAN or oran < KARS_ALT_ORAN:
        print(f"  ! makul degil ({oran:.1f}x) atlandi: {ad[:42]}")
        return False
    return True


# Karsilastirma artik TEK TURDA HEPSINI degil, dilim dilim yapiyor.
# Her urun icin 3 arama (Migros + Ozdilek + Macrocenter) yapildigi icin
# 1000 urunu tek turda taramak yarim saati asiyordu ve 30 dakikalik cron
# ile cakisiyordu. Imlec Firebase'de tutulur, tarama turlara yayilir.
KARS_PARCA = 200         # tur basina karsilastirilacak urun sayisi


def kars_imlec_oku():
    """Son islenen urun kimligini dondurur (bos ise basa doner)."""
    try:
        r = urllib.request.Request(db_url("sistem/kars_imlec"))
        with urllib.request.urlopen(r, timeout=15) as c:
            v = json.loads(c.read().decode("utf-8"))
        return v if isinstance(v, str) else ""
    except Exception:
        return ""


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
    print("\n--- FIYAT KARSILASTIRMASI ---")
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
    # Taranacaklar:
    #   - elle tabloda olanlar (kullanici onayi gramaj kontrolunden ustun)
    #   - gramaji okunabilenler
    #   - ELINDE ESKI KARSILASTIRMA OLANLAR: gramaj kurali sıkilastiginda
    #     (orn "3x1 180 G" artik okunamaz sayiliyor) eski hatali satirin
    #     Firebase'de kalmamasi icin bunlar da taranir. Bu urunlerde hicbir
    #     eslesme bulunamayacagi icin kayit temizlenir; ek istek atilmaz,
    #     cunku eslesme fonksiyonlari gramaj okunamayinca hemen doner.
    # Migros kaynakli urunler de taranir: onlar da Ozdilek/Macrocenter/
    # Carrefour ile kiyaslanabiliyor. Eskiden Migros referans market oldugu
    # icin disarida birakiliyordu, artik dort hedef var.
    tumu = [(k, v) for k, v in urunler.items()
            if isinstance(v, dict)
            and (k in tablo
                 or kars_miktar(v.get("urun_adi", ""))
                 or v.get("karsilastirma") is not None)]
    tumu.sort(key=lambda x: x[0])          # sabit sira: imlec guvenilir olsun

    if not tumu:
        print("  Taranacak urun yok.")
        return 0

    # ---- Imlecten sonraki dilimi al ----
    imlec = kars_imlec_oku()
    baslangic = 0
    if imlec:
        for i, (uid, _) in enumerate(tumu):
            if uid > imlec:
                baslangic = i
                break
        else:
            baslangic = 0              # sona gelinmis, basa don
    tur_bitti = baslangic == 0 and imlec

    hedefler = tumu[baslangic:baslangic + KARS_PARCA]
    if tur_bitti:
        print("  (tam tarama tamamlandi, basa donuluyor)")
    print(f"  Bu turda: {len(hedefler)} urun  "
          f"({baslangic + 1}-{baslangic + len(hedefler)} / {len(tumu)})")

    bulundu = 0
    elle = 0
    for urun_id, veri in hedefler:
        ad = veri.get("urun_adi", "")
        market = veri.get("market", "")
        bizim_fiyat = veri.get("gecerli_fiyat", 0)
        eslesme = None

        kayit = tablo.get(urun_id)

        # 0) Tabloda "atla" isaretliyse: eslestirme yapma ve varsa temizle
        if isinstance(kayit, dict) and kayit.get("atla"):
            if veri.get("karsilastirma") is not None or \
                    veri.get("migros_normal") is not None:
                firebase_yama(f"urunler/{urun_id}", {
                    "karsilastirma": None, "karsilastirma_link": None,
                    "en_ucuz_market": None,
                    "migros_normal": None, "migros_ad": None,
                    "migros_carpan": None, "migros_esdeger": None,
                    "migros_zaman": None,
                })
                print(f"  temizlendi: {ad[:45]}")
            continue

        # ---- MIGROS tarafi (elle tablo oncelikli, sonra otomatik) ----
        # Urunun kendisi Migros'tansa bu tarafi atla; yoksa urun kendi
        # kendiyle kiyaslanip kendi fiyatinin uzerine yaziyor.
        elle_migros = (tablo_market(kayit, "migros")
                       if market != "Migros" else None)
        if elle_migros:
            try:
                sonuc = migros_urun_getir(elle_migros["kod"])
            except Exception:
                sonuc = None
            if sonuc:
                beklenen = tr_ara(elle_migros.get("ad", "")).split()
                gelen = set(tr_ara(sonuc["ad"]).split())
                ortak = len([w for w in beklenen if w in gelen])
                if ortak >= max(2, len(beklenen) // 3):
                    carpan = kars_carpan_hesapla(
                        ad, sonuc["ad"], elle_migros.get("carpan"))
                    sonuc["carpan"] = carpan
                    sonuc["esdeger"] = round(sonuc["normal"] * carpan, 2)
                    if kars_makul_mu(sonuc["esdeger"], bizim_fiyat, ad):
                        eslesme = sonuc
                        elle += 1
        if (eslesme is None and market != "Migros"
                and not elle_engelli(kayit, "migros")):
            try:
                oto = kesin_eslesme(ad)
            except Exception:
                oto = None
            if oto:
                oto["carpan"] = 1.0
                oto["esdeger"] = oto["normal"]
                if kars_makul_mu(oto["esdeger"], bizim_fiyat, ad):
                    eslesme = oto

        # ---- OZDILEK tarafi (elle tablo oncelikli, sonra otomatik) ----
        ozdilek = None
        if market != "Ozdilek":
            elle_oz = tablo_market(kayit, "ozdilek")
            if elle_oz:
                try:
                    oz = ozdilek_fiyat_getir(elle_oz["kod"],
                                             elle_oz.get("ad", ad))
                except Exception:
                    oz = None
                oz = elle_esdeger(oz, ad, elle_oz)
                if oz and kars_makul_mu(oz["normal"], bizim_fiyat, ad):
                    ozdilek = oz
                    elle += 1
            if ozdilek is None and not elle_engelli(kayit, "ozdilek"):
                try:
                    oz = ozdilek_kesin_eslesme(ad)
                except Exception:
                    oz = None
                if oz and kars_makul_mu(oz["normal"], bizim_fiyat, ad):
                    ozdilek = oz

        # ---- MACROCENTER tarafi (elle tablo oncelikli, sonra otomatik) ----
        macro = None
        if market != "Macrocenter":
            elle_mc = tablo_market(kayit, "macro")
            if elle_mc:
                try:
                    mc = macro_fiyat_getir(elle_mc["kod"],
                                           elle_mc.get("ad", ad))
                except Exception:
                    mc = None
                mc = elle_esdeger(mc, ad, elle_mc)
                if mc and kars_makul_mu(mc["normal"], bizim_fiyat, ad):
                    macro = mc
                    elle += 1
            if macro is None and not elle_engelli(kayit, "macro"):
                try:
                    mc = macro_kesin_eslesme(ad)
                except Exception:
                    mc = None
                if mc and kars_makul_mu(mc["normal"], bizim_fiyat, ad):
                    macro = mc

        # ---- CARREFOUR tarafi (elle tablo oncelikli, sonra otomatik) ----
        carre = None
        if market != "Carrefour":
            elle_cr = tablo_market(kayit, "carrefour")
            if elle_cr:
                try:
                    cr = carrefour_fiyat_getir(elle_cr["kod"])
                except Exception:
                    cr = None
                cr = elle_esdeger(cr, ad, elle_cr)
                if cr and kars_makul_mu(cr["normal"], bizim_fiyat, ad):
                    carre = cr
                    elle += 1
            if carre is None and not elle_engelli(kayit, "carrefour"):
                try:
                    cr = carrefour_kesin_eslesme(ad)
                except Exception:
                    cr = None
                if cr and kars_makul_mu(cr["normal"], bizim_fiyat, ad):
                    carre = cr

        # ---- Karsilastirma yapisi: bizim fiyat + bulunan diger marketler ----
        kars = {market: bizim_fiyat} if market and bizim_fiyat else {}
        kars_link = {}
        if veri.get("link"):
            kars_link[market] = veri["link"]        # urunun kendi adresi
        if eslesme:
            kars["Migros"] = eslesme.get("esdeger", eslesme["normal"])
            if eslesme.get("link"):
                kars_link["Migros"] = eslesme["link"]
        if ozdilek:
            kars["Ozdilek"] = ozdilek["normal"]
            if ozdilek.get("link"):
                kars_link["Ozdilek"] = ozdilek["link"]
        if macro:
            kars["Macrocenter"] = macro["normal"]
            if macro.get("link"):
                kars_link["Macrocenter"] = macro["link"]
        if carre:
            kars["Carrefour"] = carre["normal"]
            if carre.get("link"):
                kars_link["Carrefour"] = carre["link"]

        if len(kars) >= 2:
            en_ucuz = min(kars, key=kars.get)
            firebase_yama(f"urunler/{urun_id}", {
                "karsilastirma": kars,
                "karsilastirma_link": kars_link or None,
                "en_ucuz_market": en_ucuz,
                "migros_normal": eslesme.get("normal") if eslesme else None,
                "migros_ad": eslesme.get("ad") if eslesme else None,
                "migros_carpan": eslesme.get("carpan", 1) if eslesme else None,
                "migros_esdeger": eslesme.get("esdeger") if eslesme else None,
                "migros_zaman": int(time.time()),
            })
            bulundu += 1
        else:
            if veri.get("karsilastirma") is not None or \
                    veri.get("migros_normal") is not None:
                firebase_yama(f"urunler/{urun_id}", {
                    "karsilastirma": None, "karsilastirma_link": None,
                    "en_ucuz_market": None,
                    "migros_normal": None, "migros_ad": None,
                    "migros_carpan": None, "migros_esdeger": None,
                    "migros_zaman": None,
                })
        time.sleep(KARS_BEKLEME)

    # Imleci son islenen urune tasi; tur sonuna gelindiyse basa al
    yeni_imlec = hedefler[-1][0] if hedefler else ""
    if baslangic + len(hedefler) >= len(tumu):
        yeni_imlec = ""            # sonraki turda bastan baslar
        firebase_yaz("sistem/son_karsilastirma", int(time.time()))
        print("  tam tarama bitti, imlec basa alindi")
    firebase_yaz("sistem/kars_imlec", yeni_imlec)

    print(f"  Karsilastirma bulunan: {bulundu}/{len(hedefler)}  "
          f"(elle tablodan: {elle})")
    return bulundu



# ==================== ARAMA DIZINI ====================
# Uygulamadaki takip kutusu, indirimde OLMAYAN urunleri de onerebilsin diye
# genis bir kelime dizini kurulur. Her anlamli kelime icin BIR temsilci urun
# saklanir (ad, gorsel, fiyat, market). Uygulama yazdikca onek sorgusuyla
# yalnizca eslesen 6 kaydi indirir; dizin ne kadar buyurse buyusun indirilen
# veri kilobayt duzeyinde kalir.

ARAMA_ARALIK = 20 * 3600          # gunde bir kurulur
OZDILEK_KATALOG_SAYFA = 130       # 100'luk sayfa -> ~13.000 urun
ARAMA_ANLAMSIZ = {
    "ve", "ile", "gr", "kg", "ml", "lt", "cl", "adet", "paket", "li", "lu",
    "adet", "kutu", "poset", "sise", "tane", "boy", "yeni", "ozel", "super",
}


def arama_kelimeleri(ad):
    """Bir urun adindan dizine girecek anlamli kelimeleri cikarir."""
    temiz = re.sub(r"[^a-z0-9 ]", " ", tr_ara(ad))
    cikti = []
    for w in temiz.split():
        if len(w) < 3 or w.isdigit() or w in ARAMA_ANLAMSIZ:
            continue
        if w in cikti:
            continue
        cikti.append(w)
        if len(cikti) >= 6:       # urun adlari kisa; hepsini alalim
            break
    return cikti


def arama_zamani_mi():
    try:
        r = urllib.request.Request(db_url("sistem/son_arama_dizini"))
        with urllib.request.urlopen(r, timeout=15) as c:
            son = json.loads(c.read().decode("utf-8"))
        if not isinstance(son, (int, float)):
            return True
        return (time.time() - son) > ARAMA_ARALIK
    except Exception:
        return True


def ozdilek_tam_katalog():
    """Ozdilek katalogunu sayfalayarak gezer. (ad, fiyat, gorsel) dondurur."""
    basliklar = {
        "User-Agent": BASLIKLAR["User-Agent"],
        "Accept": "application/json",
        "Referer": "https://www.ozdilekteyim.com/",
        "Origin": "https://www.ozdilekteyim.com",
    }
    urunler = []
    for sayfa in range(OZDILEK_KATALOG_SAYFA):
        url = (f"{OZDILEK_TEMEL}/products/search?query=:relevance"
               f"&pageSize=100&currentPage={sayfa}&lang=tr&curr=TRY")
        try:
            istek = urllib.request.Request(url, headers=basliklar)
            with urllib.request.urlopen(istek, timeout=20) as c:
                veri = json.loads(c.read().decode("utf-8"))
        except Exception:
            break
        gelen = veri.get("products", [])
        if not gelen:
            break
        for u in gelen:
            ad = u.get("name") or ""
            fiyat = ((u.get("price") or {}).get("value")
                     or (u.get("listPrice") or {}).get("value"))
            if not ad or not fiyat:
                continue
            gorsel = ""
            imgs = u.get("images") or []
            if imgs and isinstance(imgs[0], dict):
                gorsel = imgs[0].get("url", "") or ""
                if gorsel.startswith("/"):
                    gorsel = "https://www.ozdilekteyim.com" + gorsel
            urunler.append((ad, round(float(fiyat), 2), gorsel, "Ozdilek"))
        time.sleep(0.15)
    return urunler


def arama_dizini_kur():
    """Kelime dizinini kurar ve Firebase'e yazar."""
    if not arama_zamani_mi():
        print("Arama dizini zamani degil (gunde bir kurulur).")
        return 0

    print("\n--- ARAMA DIZINI ---")
    kaynaklar = []

    # 1) Ozdilek tam katalogu (en genis kaynak)
    ozd = ozdilek_tam_katalog()
    print(f"  Ozdilek katalogu: {len(ozd)} urun")
    kaynaklar.extend(ozd)

    # 2) Carrefour katalogu (laptop dosyasindan, zaten bellekte)
    for uid, v in (CARREFOUR_KATALOG or {}).items():
        ad = v.get("a") or ""
        fiyat = v.get("f") or 0
        if ad and fiyat:
            kaynaklar.append((ad, fiyat, "", "Carrefour"))
    print(f"  Carrefour katalogu eklendi, toplam {len(kaynaklar)}")

    # 3) Mevcut indirimli urunler (gorselleri en iyi olanlar)
    try:
        r = urllib.request.Request(db_url("urunler"))
        with urllib.request.urlopen(r, timeout=30) as c:
            mevcut = json.loads(c.read().decode("utf-8")) or {}
        for v in mevcut.values():
            if not isinstance(v, dict):
                continue
            ad = v.get("urun_adi") or ""
            fiyat = v.get("gecerli_fiyat") or 0
            if ad and fiyat:
                kaynaklar.append((ad, fiyat, v.get("gorsel", ""),
                                  v.get("market", "")))
    except Exception as e:
        print(f"  mevcut urunler okunamadi: {type(e).__name__}")

    # ---- kelime -> temsilci urun ----
    dizin = {}
    for ad, fiyat, gorsel, market in kaynaklar:
        for kelime in arama_kelimeleri(ad):
            eski = dizin.get(kelime)
            # gorseli olan kaydi tercih et, yoksa ilk geleni tut
            if eski and (eski.get("g") or not gorsel):
                continue
            dizin[kelime] = {"a": ad[:70], "f": fiyat,
                             "g": gorsel, "m": market}

    print(f"  dizin: {len(dizin)} kelime")
    if not dizin:
        return 0

    # ---- parca parca yaz (tek istek cok buyuk olmasin) ----
    anahtarlar = list(dizin.keys())
    parca_boy = 1500
    yazilan = 0
    for i in range(0, len(anahtarlar), parca_boy):
        parca = {a: dizin[a] for a in anahtarlar[i:i + parca_boy]}
        if firebase_yama("arama", parca):
            yazilan += len(parca)
        else:
            print(f"  parca {i // parca_boy + 1} yazilamadi")
    print(f"  {yazilan} kelime yazildi")
    firebase_yaz("sistem/son_arama_dizini", int(time.time()))
    return yazilan



# ==================== WEB VERISI (GitHub Pages) ====================
# Uygulama urun listesini Firebase yerine GitHub Pages'ten okur.
# Sebep: Firebase ucretsiz plani ayda 10 GB indirme veriyor; tam liste
# ~2.7 MB oldugu icin 50 kullanicida kota doluyordu. GitHub Pages ayda
# 100 GB veriyor ve gzip ile servis ettigi icin ayni veri ~350 KB iniyor.
#
# Depo gecmisi sismesin diye 2 saatte bir yaziliyor; uygulama tarafinda
# 12 saatten eski dosya kabul edilmiyor, o durumda Firebase'e dusuluyor.

WEB_DEPO = "mrbrdkc28-bit/ucuzcum-web"
WEB_DOSYA = "urunler.json"
WEB_ARALIK = 2 * 3600          # bu siklikta yazilir
WEB_TAZELIK = 6 * 3600         # bundan eski urunler dosyaya konmaz

# Uygulamanin okumadigi alanlar dosyaya konmaz (%22 kucultuyor)
WEB_ATILAN = {
    "kaynak", "fiyat_notu", "onceki_fiyat", "migros_normal", "migros_ad",
    "migros_carpan", "migros_esdeger", "migros_zaman",
}



def pages_yukle(dosya_adi, metin):
    """
    Verilen metni ucuzcum-web deposuna yazar (GitHub Pages'ten servis edilir).
    Firebase yerine burasi kullaniliyor cunku Pages ayda 100 GB veriyor ve
    gzip ile servis ediyor; Firebase ucretsiz plani 10 GB.
    """
    token = os.environ.get("WEB_TOKEN")
    if not token:
        print(f"  [{dosya_adi}] WEB_TOKEN yok")
        return False

    adres = f"https://api.github.com/repos/{WEB_DEPO}/contents/{dosya_adi}"
    basliklar = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ucuzcum-bot",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

    sha = None
    try:
        istek = urllib.request.Request(adres, headers=basliklar)
        with urllib.request.urlopen(istek, timeout=60) as c:
            sha = json.loads(c.read().decode("utf-8")).get("sha")
    except urllib.error.HTTPError as h:
        if h.code != 404:
            print(f"  [{dosya_adi}] okuma hatasi {h.code}")
            return False
    except Exception as e:
        print(f"  [{dosya_adi}] okuma hatasi: {type(e).__name__}")
        return False

    govde = {
        "message": f"{dosya_adi} {time.strftime('%d.%m.%Y %H:%M')}",
        "content": base64.b64encode(metin.encode("utf-8")).decode("ascii"),
    }
    if sha:
        govde["sha"] = sha

    try:
        istek = urllib.request.Request(
            adres, data=json.dumps(govde).encode("utf-8"),
            headers=basliklar, method="PUT")
        with urllib.request.urlopen(istek, timeout=120) as c:
            return c.status in (200, 201)
    except urllib.error.HTTPError as h:
        try:
            ayrinti = json.loads(h.read().decode("utf-8")).get("message", "")
        except Exception:
            ayrinti = ""
        print(f"  [{dosya_adi}] YUKLENEMEDI {h.code} {ayrinti}")
    except Exception as e:
        print(f"  [{dosya_adi}] YUKLENEMEDI: {type(e).__name__}")
    return False


def web_zamani_mi():
    try:
        r = urllib.request.Request(db_url("sistem/son_web_yazma"))
        with urllib.request.urlopen(r, timeout=15) as c:
            son = json.loads(c.read().decode("utf-8"))
        if not isinstance(son, (int, float)):
            return True
        return (time.time() - son) > WEB_ARALIK
    except Exception:
        return True


def web_verisi_yaz():
    """Taze urunleri sadelestirip GitHub Pages'e yazar."""
    token = os.environ.get("WEB_TOKEN")
    if not token:
        print("\n[web] WEB_TOKEN yok, atlaniyor")
        return 0
    if not web_zamani_mi():
        print("\n[web] zamani degil (2 saatte bir yazilir)")
        return 0

    print("\n--- WEB VERISI ---")
    try:
        r = urllib.request.Request(db_url("urunler"))
        with urllib.request.urlopen(r, timeout=60) as c:
            urunler = json.loads(c.read().decode("utf-8")) or {}
    except Exception as e:
        print(f"  urunler okunamadi: {type(e).__name__}")
        return 0

    if not urunler:
        print("  urun yok")
        return 0

    # En taze kayda gore bayat olanlari ele (uygulamadaki 6 saat kurali)
    en_taze = 0
    for v in urunler.values():
        if isinstance(v, dict):
            g = v.get("guncelleme") or 0
            if isinstance(g, (int, float)) and g > en_taze:
                en_taze = int(g)
    esik = en_taze - WEB_TAZELIK

    sade = {}
    for uid, v in urunler.items():
        if not isinstance(v, dict):
            continue
        g = v.get("guncelleme") or 0
        if not isinstance(g, (int, float)) or g < esik:
            continue
        sade[uid] = {a: b for a, b in v.items()
                     if a not in WEB_ATILAN and b is not None}

    paket = {
        "olusturma": int(time.time()),
        "urun_sayisi": len(sade),
        "urunler": sade,
    }
    metin = json.dumps(paket, ensure_ascii=False, separators=(",", ":"))
    print(f"  {len(sade)}/{len(urunler)} taze urun, {len(metin)//1024} KB")

    adres = f"https://api.github.com/repos/{WEB_DEPO}/contents/{WEB_DOSYA}"
    basliklar = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ucuzcum-bot",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

    # mevcut dosyanin sha degeri
    sha = None
    try:
        istek = urllib.request.Request(adres, headers=basliklar)
        with urllib.request.urlopen(istek, timeout=60) as c:
            sha = json.loads(c.read().decode("utf-8")).get("sha")
    except urllib.error.HTTPError as h:
        if h.code != 404:
            print(f"  okuma hatasi {h.code}")
            return 0
    except Exception as e:
        print(f"  okuma hatasi: {type(e).__name__}")
        return 0

    govde = {
        "message": f"urun verisi {time.strftime('%d.%m.%Y %H:%M')}",
        "content": base64.b64encode(metin.encode("utf-8")).decode("ascii"),
    }
    if sha:
        govde["sha"] = sha

    try:
        istek = urllib.request.Request(
            adres, data=json.dumps(govde).encode("utf-8"),
            headers=basliklar, method="PUT")
        with urllib.request.urlopen(istek, timeout=120) as c:
            if c.status in (200, 201):
                firebase_yaz("sistem/son_web_yazma", int(time.time()))
                print(f"  yuklendi: {WEB_DEPO}/{WEB_DOSYA}")
                return len(sade)
    except urllib.error.HTTPError as h:
        try:
            ayrinti = json.loads(h.read().decode("utf-8")).get("message", "")
        except Exception:
            ayrinti = ""
        print(f"  YUKLENEMEDI {h.code} {ayrinti}")
    except Exception as e:
        print(f"  YUKLENEMEDI: {type(e).__name__}")
    return 0



# ==================== FIYAT KATALOGU ====================
# Kiyasla ekranindaki "Tum urunler" sekmesini besler.
# Indirimde OLMAYAN urunlerin de marketler arasi fiyatini tutar.
#
# Neden ayri: indirim listesi 30 dakikada bir tazeleniyor cunku indirim
# fiyatlari oynak. Raf fiyatlari haftalarca degismiyor, o yuzden katalog
# gunde bir kez kuruluyor. Boylece Actions dakikasi bosa gitmiyor.
#
# Kaynak: Ozdilek tam katalogu (en genis, tek istekte 100 urun) + elle
# eslesme tablosundaki Migros/Macrocenter kayitlari. Elle eslesenler
# kullanicinin dogruladigi eslesmeler oldugu icin en guvenilir kisim.

KATALOG_ARALIK = 20 * 3600        # gunde bir kurulur
KATALOG_OZDILEK_SAYFA = 130       # 100'luk sayfa -> ~13.000 urun
KATALOG_ELLE_BEKLEME = 0.25       # elle eslesenlerde istekler arasi bekleme
KATALOG_EN_AZ_MARKET = 2          # bu kadar markette fiyati olmayan yazilmaz


def katalog_zamani_mi():
    try:
        r = urllib.request.Request(db_url("sistem/son_katalog"))
        with urllib.request.urlopen(r, timeout=15) as c:
            son = json.loads(c.read().decode("utf-8"))
        if not isinstance(son, (int, float)):
            return True
        return (time.time() - son) > KATALOG_ARALIK
    except Exception:
        return True


def katalog_kur():
    """
    Marketler arasi fiyat katalogunu kurar ve `katalog` dugumune yazar.
    Yapisi urun kayitlarina benzer ama indirim alanlari YOKTUR; uygulama
    bunlari indirim listesine karistirmaz.
    """
    if not katalog_zamani_mi():
        print("\nKatalog zamani degil (gunde bir kurulur).")
        return 0

    print("\n--- FIYAT KATALOGU ---")
    tablo = eslesmeleri_yukle()

    # ---- 1) Ozdilek tam katalogu ----
    ozd = ozdilek_tam_katalog()
    print(f"  Ozdilek: {len(ozd)} urun")

    # ad -> {market: fiyat} eslesme havuzu
    havuz = {}
    for ad, fiyat, gorsel, market in ozd:
        anahtar = kars_normalize(ad)
        if not anahtar:
            continue
        kayit = havuz.setdefault(anahtar, {
            "ad": ad, "gorsel": gorsel, "fiyat": {}, "link": {}})
        kayit["fiyat"]["Ozdilek"] = fiyat

    # ---- 2) Elle eslesme tablosundaki Migros/Macrocenter kayitlari ----
    # Bunlar kullanicinin dogruladigi eslesmeler; kod ile dogrudan fiyat
    # cekiliyor, isim tahminine gerek yok.
    islenen = 0
    for urun_id, kayit in tablo.items():
        if not isinstance(kayit, dict):
            continue
        for market, getir in (("migros", None), ("macro", macro_fiyat_getir),
                              ("ozdilek", ozdilek_fiyat_getir)):
            alt = kayit.get(market)
            if not isinstance(alt, dict) or not alt.get("kod"):
                continue
            ad = alt.get("ad") or ""
            if not ad:
                continue
            anahtar = kars_normalize(ad)
            if not anahtar:
                continue
            hedef = havuz.setdefault(anahtar, {
                "ad": ad, "gorsel": "", "fiyat": {}, "link": {}})
            fb_ad = {"migros": "Migros", "macro": "Macrocenter",
                     "ozdilek": "Ozdilek"}[market]
            if fb_ad in hedef["fiyat"]:
                continue
            try:
                if market == "migros":
                    sonuc = migros_urun_getir(alt["kod"])
                else:
                    sonuc = getir(alt["kod"], ad)
            except Exception:
                sonuc = None
            if sonuc and sonuc.get("normal"):
                hedef["fiyat"][fb_ad] = sonuc["normal"]
                if sonuc.get("link"):
                    hedef["link"][fb_ad] = sonuc["link"]
            islenen += 1
            time.sleep(KATALOG_ELLE_BEKLEME)
    print(f"  elle tablodan {islenen} sorgu yapildi")

    # ---- 3) En az iki markette fiyati olanlari yaz ----
    cikti = {}
    for anahtar, v in havuz.items():
        if len(v["fiyat"]) < KATALOG_EN_AZ_MARKET:
            continue
        en_ucuz = min(v["fiyat"], key=v["fiyat"].get)
        kimlik = "k_" + re.sub(r"[^a-z0-9]", "", anahtar)[:40]
        if not kimlik or kimlik == "k_":
            continue
        cikti[kimlik] = {
            "urun_adi": v["ad"][:90],
            "gorsel": v["gorsel"],
            "karsilastirma": v["fiyat"],
            "karsilastirma_link": v["link"],
            "en_ucuz_market": en_ucuz,
            "en_ucuz_fiyat": v["fiyat"][en_ucuz],
            "guncelleme": int(time.time()),
        }

    print(f"  katalog: {len(cikti)} urun (en az "
          f"{KATALOG_EN_AZ_MARKET} markette fiyati var)")
    if not cikti:
        return 0

    # GitHub Pages'e yaz: uygulama yalnizca "Tum urunler" sekmesine
    # basildiginda indiriyor, her acilista degil.
    paket = {
        "olusturma": int(time.time()),
        "urun_sayisi": len(cikti),
        "urunler": cikti,
    }
    metin = json.dumps(paket, ensure_ascii=False, separators=(",", ":"))
    print(f"  paket: {len(metin)//1024} KB")

    if pages_yukle("katalog.json", metin):
        firebase_yaz("sistem/son_katalog", int(time.time()))
        print(f"  yuklendi: {WEB_DEPO}/katalog.json")
        return len(cikti)
    return 0


# ==================== BILDIRIM DAGITIMI ====================

# ==================== BILDIRIM SAATI (kullanici bazli) ====================
# Her kullanici bildirim penceresini kendisi belirler:
#   bildirim_baslangic / bildirim_bitis  (0-24 arasi saat, TR saati)
# Ayarlamamis kullanici icin varsayilan 08:00-22:00.
# Pencere disinda olusan bildirimler o kullanici icin kuyruga alinir,
# kendi penceresi acilinca gonderilir.

# Bir kullaniciya tek turda gonderilecek en fazla bildirim.
# Bunu asarsa tek tek degil, tek ozet bildirim gonderilir.
BILDIRIM_SINIRI = 5

VARSAYILAN_BASLANGIC = 8
VARSAYILAN_BITIS = 22
KUYRUK_SINIRI = 40          # kullanici basina bekleyen bildirim ust siniri


def turkiye_saati():
    """GitHub Actions UTC calisir; Turkiye UTC+3."""
    return (datetime.datetime.utcnow().hour + 3) % 24


def kullanici_saati_uygun_mu(kullanici, saat=None):
    """Bu kullanicinin penceresi su an bildirime uygun mu?"""
    if saat is None:
        saat = turkiye_saati()
    try:
        bas = int(kullanici.get("bildirim_baslangic", VARSAYILAN_BASLANGIC))
        bit = int(kullanici.get("bildirim_bitis", VARSAYILAN_BITIS))
    except (TypeError, ValueError):
        bas, bit = VARSAYILAN_BASLANGIC, VARSAYILAN_BITIS

    # "her saat": 0-24 => her zaman uygun
    if bas == 0 and bit >= 24:
        return True
    # normal aralik (08-22 gibi)
    if bas <= bit:
        return bas <= saat < bit
    # gece asan aralik (orn 22-06)
    return saat >= bas or saat < bit


def _sadelestir(urun):
    """Kuyrukta saklanacak asgari alanlar."""
    return {
        "urun_adi": urun.get("urun_adi", ""),
        "market": urun.get("market", ""),
        "gecerli_fiyat": urun.get("gecerli_fiyat", 0),
        "onceki_fiyat": urun.get("onceki_fiyat", 0),
        "indirim_orani": urun.get("indirim_orani", 0),
    }


def kuyruk_oku(anahtar):
    """Bir kullanicinin bekleyen bildirim kuyrugunu okur."""
    try:
        r = urllib.request.Request(db_url(f"kullanicilar/{anahtar}/bekleyen"))
        with urllib.request.urlopen(r, timeout=15) as c:
            veri = json.loads(c.read().decode("utf-8"))
        return veri if isinstance(veri, list) else []
    except Exception:
        return []


def kuyruga_ekle(anahtar, kayitlar):
    """Kullanicinin kuyruguna yeni bildirimleri ekler (sinirli)."""
    mevcut = kuyruk_oku(anahtar)
    varolan = {x.get("id") for x in mevcut if isinstance(x, dict)}
    for urun_id, urun in kayitlar:
        if urun_id in varolan:
            continue
        mevcut.append({"id": urun_id, "u": _sadelestir(urun)})
    mevcut = mevcut[-KUYRUK_SINIRI:]
    firebase_yaz(f"kullanicilar/{anahtar}/bekleyen", mevcut)
    return len(mevcut)


def kuyruk_temizle(anahtar):
    firebase_yaz(f"kullanicilar/{anahtar}/bekleyen", None)


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

    saat = turkiye_saati()
    gonderilen = 0
    kuyruga_alinan = 0
    ozetlenen = 0

    for anahtar, kullanici in kullanicilar.items():
        if not isinstance(kullanici, dict):
            continue
        token = kullanici.get("token") or anahtar
        if not token:
            continue

        # Bu kullanicinin penceresi su an uygun mu?
        uygun = kullanici_saati_uygun_mu(kullanici, saat)

        # Once bu kullanici icin bildirim gerektiren urunleri topla
        benim_dusen = []
        benim_kelime = []

        for urun_id, urun in DUSENLER:
            if bildirim_gonderilsin_mi(kullanici, urun, urun_id):
                benim_dusen.append((urun_id, urun))

        kelimeler = kullanici.get("kelimeler")
        if kelimeler:
            if isinstance(kelimeler, dict):
                ham = list(kelimeler.values())
            elif isinstance(kelimeler, list):
                ham = kelimeler
            else:
                ham = []
            kelime_normal = [tr_ara(str(x)) for x in ham if x]
            for urun_id, urun in (YENI_INDIRIMLER + DUSENLER):
                ad_normal = tr_ara(urun.get("urun_adi", ""))
                if any(kn and kn in ad_normal for kn in kelime_normal):
                    benim_kelime.append((urun_id, urun))

        if not benim_dusen and not benim_kelime:
            # yine de kuyrukta bekleyen olabilir; asagida ele alinir
            pass

        # ---- Pencere UYGUN DEGILSE: kuyruga al, gonderme ----
        if not uygun:
            hepsi = benim_dusen + benim_kelime
            if hepsi:
                kuyruga_alinan += kuyruga_ekle(anahtar, hepsi)
            continue

        # ---- Pencere UYGUN: once kuyrugu bosalt, sonra bu turu gonder ----
        gonderildi = set()

        # kuyruktaki bekleyenler
        bekleyen = kuyruk_oku(anahtar)

        # ---- Once bu kullaniciya gidecek HER SEYI topla, sonra karar ver ----
        # Ayni urun birden fazla listede olabilir; id ile tekillestiriyoruz.
        gonderilecek = []      # (id, baslik, govde)
        eklendi = set()

        for kayit in bekleyen:
            if not isinstance(kayit, dict) or not kayit.get("id"):
                continue
            uid_k = kayit["id"]
            if uid_k in eklendi:
                continue
            eklendi.add(uid_k)
            urun = kayit.get("u") or {}
            gonderilecek.append((
                uid_k,
                f"{urun.get('market', '')} indirim!",
                f"{urun.get('urun_adi', '')} {urun.get('gecerli_fiyat', '')} TL "
                f"(%{urun.get('indirim_orani', 0)})"))

        for urun_id, urun in benim_dusen:
            if urun_id in eklendi:
                continue
            eklendi.add(urun_id)
            gonderilecek.append((
                urun_id,
                f"{urun['market']} indirim!",
                f"{urun['urun_adi']} {urun['onceki_fiyat']} -> "
                f"{urun['gecerli_fiyat']} TL (%{urun['indirim_orani']})"))

        for urun_id, urun in benim_kelime:
            if urun_id in eklendi:
                continue
            eklendi.add(urun_id)
            gonderilecek.append((
                urun_id,
                f"Takip: {urun['market']}",
                f"{urun['urun_adi']} {urun['gecerli_fiyat']} TL "
                f"(%{urun['indirim_orani']}) indirimde!"))

        if bekleyen:
            kuyruk_temizle(anahtar)

        if not gonderilecek:
            continue

        # ---- Cok fazlaysa tek tek degil, TEK OZET bildirim ----
        # Yeni bir market eklendiginde ya da buyuk kampanya donusunde
        # kullaniciya onlarca bildirim gitmesini onler.
        if len(gonderilecek) > BILDIRIM_SINIRI:
            adet = len(gonderilecek)
            baslik = "Ucuzcum"
            govde = (f"{adet} takip ettigin urun indirimde. "
                     f"Listeyi gormek icin dokun.")
            if fcm_gonder(access, token, baslik, govde):
                gonderilen += 1
                ozetlenen += adet
            continue

        for urun_id, baslik, govde in gonderilecek:
            if fcm_gonder(access, token, baslik, govde):
                gonderilen += 1
                gonderildi.add(urun_id)

    print(f"Toplam {gonderilen} bildirim gonderildi."
          + (f"  {kuyruga_alinan} kuyruga alindi." if kuyruga_alinan else "")
          + (f"  {ozetlenen} bildirim ozete donusturuldu." if ozetlenen else ""))

    olu_jetonlari_temizle(kullanicilar)


# ==================== ANA ====================

if __name__ == "__main__":
    if not FIREBASE_URL:
        print("HATA: FIREBASE_URL yok")
        raise SystemExit(1)

    print("Ucuzcum Botu basladi.")

    # Eski surumden kalan ortak kuyrugu temizle (artik kullanici bazli)
    try:
        firebase_yaz("sistem/bekleyen", None)
    except Exception:
        pass

    GECMIS_AKTIF = gunluk_isler_gerekli_mi()
    if GECMIS_AKTIF:
        print("[gunluk] fiyat gecmisi bu turda islenecek")
        gecmis_yukle()
    else:
        print("[gunluk] fiyat gecmisi bugun zaten islendi, atlaniyor")

    print("\n==== MIGROS ===="); m = migros_calis()
    print("\n==== A101 ===="); a = a101_calis()
    print("\n==== BIM ===="); b = bim_calis()
    print("\n==== MOPAS ===="); mo = mopas_calis()
    print("\n==== MACROCENTER ===="); mc = macrocenter_calis()
    print("\n==== IDEAL ===="); idl = ideal_calis()
    print("\n==== OZDILEK ===="); ozd = ozdilek_calis()
    car = carrefour_calis()

    print("\n" + "=" * 50)
    print(f"Cekilen: Migros:{m}  A101:{a}  BIM:{b}  Mopas:{mo}  "
          f"Macro:{mc}  Ideal:{idl}  Ozdilek:{ozd}  Carrefour:{car}  "
          f"Toplam:{m + a + b + mo + mc + idl + ozd + car}")
    print(f"Fiyat dusen: {len(DUSENLER)}  Indirime yeni giren: {len(YENI_INDIRIMLER)}")

    if GECMIS_AKTIF:
        gecmis_yaz()

    bildirimleri_gonder()

    try:
        karsilastirma_calis()
    except Exception as e:
        print(f"Karsilastirma atlandi: {type(e).__name__}")

    try:
        arama_dizini_kur()
    except Exception as e:
        print(f"Arama dizini atlandi: {type(e).__name__}")

    try:
        katalog_kur()
    except Exception as e:
        print(f"Katalog atlandi: {type(e).__name__}")

    try:
        web_verisi_yaz()
    except Exception as e:
        print(f"Web verisi atlandi: {type(e).__name__}")

    if GECMIS_AKTIF:
        try:
            eski_urunleri_temizle()
        except Exception as e:
            print(f"Temizlik atlandi: {type(e).__name__}")
        try:
            cift_kayitlari_temizle()
        except Exception as e:
            print(f"Cift temizligi atlandi: {type(e).__name__}")
        gunluk_isler_bitti()

    # ---- Market sagligi: ARDISIK sifir sayaci ----
    # Tek turluk sifir gecici olabilir (site yavasladi, istek zaman asimina
    # ugradi). Bu yuzden alarm ancak bir market ust uste ALARM_ESIGI tur
    # sifir dondurunce calar. Urun gelince sayac sifirlanir.
    sayimlar = {"Migros": m, "A101": a, "BIM": b, "Mopas": mo,
                "Macrocenter": mc, "Ideal": idl, "Ozdilek": ozd}
    olu = sifir_sayaclarini_guncelle(sayimlar)

    # Carrefour ayri: sifir olmasi cogu zaman "laptop bugun calismadi"
    # demek, ariza degil. Sadece veri cok eskiyse uyariyoruz.
    carrefour_uyari = False
    if CARREFOUR_VERI_YASI is None:
        print("[not] Carrefour: carrefour.json yok")
    elif CARREFOUR_VERI_YASI > CARREFOUR_ALARM_YASI:
        saat = CARREFOUR_VERI_YASI // 3600
        print(f"[not] Carrefour verisi {saat} saatlik — laptop betigi "
              f"uzun suredir calismamis")
        carrefour_uyari = True

    if olu or carrefour_uyari:
        print("\n" + "!" * 52)
        if olu:
            print(f"UYARI: su marketler {ALARM_ESIGI} turdur hic urun "
                  f"dondurmedi: {', '.join(olu)}")
            print("Muhtemelen site yapisi degisti, bot guncellenmeli.")
        if carrefour_uyari:
            print(f"UYARI: Carrefour verisi {CARREFOUR_VERI_YASI // 3600} "
                  f"saatlik. Laptopta carrefour.py calistir.")
        print("!" * 52)
        raise SystemExit(1)   # GitHub hata e-postasi gondersin
    print("\nBot tamamlandi.")
