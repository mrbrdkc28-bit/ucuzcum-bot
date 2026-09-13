"""
UCUZCUM — CARREFOURSA TOPLAYICI (laptopta calisir)

Ne yapar:
  1) Secili market kategorilerinin vitrinlerini gezer
  2) Indirimli urunleri  -> urunler/carrefour_{id}   (ana liste)
  3) TUM urunleri        -> carrefour_katalog/{id}   (karsilastirma icin)

TEST MODU:
  Ayni klasorde 'carrefour-key.json' YOKSA hicbir sey yazmaz,
  sadece ne yazacagini ekrana basar. Once boyle calistir.

GEREKLI:
  pip install google-auth

CALISTIRMA:
  cmd > cd Desktop > python carrefour.py
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# ----------------------------------------------------------------- ayarlar

# Veri Firebase'e DOGRUDAN yazilmaz. GitHub'a yuklenir, bot oradan alir.
# Boylece laptopta Firebase anahtari durmaz.
DEPO = "mrbrdkc28-bit/ucuzcum-bot"
DOSYA_ADI = "carrefour.json"
TOKEN_DOSYASI = "github-token.txt"         # yoksa test modu
BEKLEME = 12                               # kategoriler arasi saniye
TEMEL = "https://www.carrefoursa.com"

BASLIK = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "identity",
    "Referer": "https://www.carrefoursa.com/",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "upgrade-insecure-requests": "1",
}

# UST SEVIYE market kategorileri (category-rotating sadece bu seviyede
# calisiyor; alt kategoriler bos donuyor).
# Disarida birakilanlar: 2286 elektronik, 2188 ev-yasam,
# 1913 kitap-kirtasiye-oyuncak
KATEGORILER = [
    ("1014", "meyve-sebze"),
    ("1044", "et-tavuk-balik"),
    ("1310", "sut-urunleri"),
    ("1363", "kahvaltilik-urunler"),
    ("1110", "temel-gida"),
    ("1493", "atistirmalik"),
    ("1064", "hazir-yemek-donuk"),
    ("1275", "firin"),
    ("1409", "icecekler"),
    ("1938", "saglikli-yasam"),
    ("1260", "dondurma"),
    ("1846", "bebek-urunleri"),
    ("2054", "pet-shop"),
    ("1556", "temizlik-urunleri"),
    ("1674", "kisisel-bakim"),
]


# ----------------------------------------------------------------- ag

def getir(adres):
    istek = urllib.request.Request(adres, headers=BASLIK)
    try:
        with urllib.request.urlopen(istek, timeout=30) as cevap:
            return cevap.status, cevap.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as h:
        return h.code, ""
    except Exception as e:
        return f"HATA:{type(e).__name__}", ""


# ----------------------------------------------------------------- ayiklama

def etiket_bul(parca, sinif):
    for m in re.finditer(r"<span[^>]*>", parca):
        if sinif in m.group(0):
            return m.group(0), m.end()
    return None, None


def fiyat_oku(parca, sinif):
    etiket, son = etiket_bul(parca, sinif)
    if not etiket:
        return 0.0
    icerik = re.search(r'content="([\d.]+)"', etiket)
    if icerik:
        try:
            return round(float(icerik.group(1)), 2)
        except ValueError:
            pass
    kuyruk = parca[son:son + 160]
    m = re.search(r"(\d{1,4})\s*,\s*(?:<[^>]+>)?\s*(\d{2})", kuyruk)
    if m:
        try:
            return round(float(f"{m.group(1)}.{m.group(2)}"), 2)
        except ValueError:
            pass
    return 0.0


def urunleri_ayikla(html):
    urunler = []
    for p in re.split(r'<div class="product_click"', html)[1:]:
        p = p[:8000]
        uid_m = re.search(r'id="(\d+)"', p)
        ad_m = re.search(r'class="item-name"[^>]*>([^<]+)<', p)
        if not (uid_m and ad_m):
            continue
        link_m = re.search(r'href="(/[^"]*-p-\d+)"', p)
        gorsel_m = re.search(
            r'data-src="(https://images\.csfour\.com/[^"]+)"', p)

        yeni = fiyat_oku(p, "js-variant-discounted-price") \
            or fiyat_oku(p, "item-price")
        eski = fiyat_oku(p, "priceLineThrough") \
            or fiyat_oku(p, "js-variant-price")

        urunler.append({
            "id": uid_m.group(1),
            "ad": re.sub(r"\s+", " ", ad_m.group(1)).strip(),
            "yeni": yeni,
            "eski": eski,
            "link": (TEMEL + link_m.group(1)) if link_m else "",
            "gorsel": gorsel_m.group(1) if gorsel_m else "",
            "kart": ("Kart ile" in p or "CarrefourSA Kart" in p),
        })
    return urunler


# ----------------------------------------------------------------- github

def token_oku():
    """Once ortam degiskeni (GitHub Actions), sonra yerel dosya (laptop).

    Actions'ta github-token.txt yok; jeton is akisindan CARREFOUR_TOKEN
    olarak geliyor. Laptopta calistirinca eski davranis aynen surer.
    """
    jeton = (os.environ.get("CARREFOUR_TOKEN") or "").strip()
    if jeton:
        return jeton
    if not os.path.exists(TOKEN_DOSYASI):
        return None
    with open(TOKEN_DOSYASI, "r", encoding="utf-8") as f:
        return f.read().strip() or None


def github_istek(adres, token, veri=None, yontem="GET"):
    basliklar = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ucuzcum-carrefour",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    govde = json.dumps(veri).encode("utf-8") if veri is not None else None
    if govde:
        basliklar["Content-Type"] = "application/json"
    istek = urllib.request.Request(adres, data=govde,
                                   headers=basliklar, method=yontem)
    try:
        with urllib.request.urlopen(istek, timeout=60) as c:
            return c.status, json.loads(c.read().decode("utf-8"))
    except urllib.error.HTTPError as h:
        try:
            ayrinti = json.loads(h.read().decode("utf-8")).get("message", "")
        except Exception:
            ayrinti = ""
        return h.code, {"message": ayrinti}
    except Exception as e:
        return f"HATA:{type(e).__name__}", {}


def github_yukle(icerik_metni):
    """carrefour.json dosyasini depoya yazar (varsa uzerine)."""
    token = token_oku()
    if not token:
        return False, "token yok"

    adres = f"https://api.github.com/repos/{DEPO}/contents/{DOSYA_ADI}"

    # mevcut dosyanin sha degeri (guncelleme icin gerekli)
    sha = None
    durum, cevap = github_istek(adres, token)
    if durum == 200 and isinstance(cevap, dict):
        sha = cevap.get("sha")
    elif durum not in (200, 404):
        return False, f"okuma hatasi {durum} {cevap.get('message','')}"

    govde = {
        "message": f"carrefour verisi {time.strftime('%d.%m.%Y %H:%M')}",
        "content": base64.b64encode(
            icerik_metni.encode("utf-8")).decode("ascii"),
    }
    if sha:
        govde["sha"] = sha

    durum, cevap = github_istek(adres, token, govde, "PUT")
    if durum in (200, 201):
        return True, "yuklendi"
    return False, f"{durum} {cevap.get('message','')}"


# ----------------------------------------------------------------- ana

def calis():
    # Jeton ortam degiskeninden de gelebilir; bu yuzden dosyanin
    # varligina degil dogrudan token_oku()'ya soruyoruz.
    test_modu = token_oku() is None
    print("CARREFOURSA TOPLAYICI")
    if test_modu:
        print("!! TEST MODU: jeton yok, GitHub'a YUKLENMEYECEK\n")
    else:
        print(f"Toplanan veri {DEPO}/{DOSYA_ADI} dosyasina yuklenecek.\n")

    urunler_kayit = {}   # indirimliler (bot ana listeye yazacak)
    katalog = {}         # hepsi (bot karsilastirmada kullanacak)
    toplam = indirimli_toplam = 0
    bos_kategori = []

    for i, (kod, ad) in enumerate(KATEGORILER):
        if i:
            time.sleep(BEKLEME)
        durum, html = getir(f"{TEMEL}/component/category-rotating/{kod}")
        if not html:
            bos_kategori.append(f"{ad}({durum})")
            print(f"  {ad:22} durum {durum} — atlandi")
            continue

        urunler = urunleri_ayikla(html)
        ind = 0
        for u in urunler:
            if not u["yeni"]:
                continue
            toplam += 1
            katalog[u["id"]] = {"a": u["ad"], "f": u["yeni"],
                                "l": u["link"]}

            if u["eski"] and u["yeni"] < u["eski"]:
                ind += 1
                # kompakt kayit: bot tam kaydi bundan uretecek
                urunler_kayit[u["id"]] = {
                    "a": u["ad"],
                    "e": u["eski"],
                    "y": u["yeni"],
                    "k": 1 if u["kart"] else 0,
                    "l": u["link"],
                    "g": u["gorsel"],
                    "c": ad,
                }
        indirimli_toplam += ind
        print(f"  {ad:22} {len(urunler):3} urun, {ind:3} indirimli")

    print("\n" + "=" * 58)
    print(f"TOPLAM: {toplam} urun katalogda, {indirimli_toplam} indirimli")
    if bos_kategori:
        print(f"Bos donen: {', '.join(bos_kategori)}")

    if not urunler_kayit:
        print("Veri toplanamadi, yukleme yapilmiyor.")
        return

    paket = {
        "toplama_zamani": int(time.time()),
        "kategori_sayisi": len(KATEGORILER) - len(bos_kategori),
        "urunler": urunler_kayit,
        "katalog": katalog,
    }
    metin = json.dumps(paket, ensure_ascii=False, separators=(",", ":"))
    print(f"paket boyutu: {len(metin)//1024} KB")

    if test_modu:
        print("\nTest modu — yuklenmedi. Ornek kayitlar:")
        for uid, v in list(urunler_kayit.items())[:5]:
            oran = round((1 - v["y"] / v["e"]) * 100)
            isaret = " [KART]" if v["k"] else ""
            print(f"  {v['a'][:40]:42} {v['y']} <- {v['e']} (%{oran}){isaret}")
        return

    ok, mesaj = github_yukle(metin)
    if ok:
        print(f"\nGitHub'a yuklendi: {DEPO}/{DOSYA_ADI}")
        print("Bot bir sonraki turunda Firebase'e yazacak.")
    else:
        print(f"\nYUKLENEMEDI: {mesaj}")


calis()
