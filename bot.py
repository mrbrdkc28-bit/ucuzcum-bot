"""
UCUZCUM BOTU v4
- Iki kaynaktan toplu urun ceker: Migros Hemen + Sanal Market (Migroskop)
- Liste endpoint'i salePrice/loyaltyPrice VERMIYOR -> her indirimli urun icin
  detay endpoint'ine ikinci istek atip gercek fiyat ayrimini aliyoruz
- Kaynagi etiketler (kullanici filtreleyebilsin)
- Onceki fiyati saklar -> bildirim icin "dustu mu" karsilastirmasi
"""

import json
import os
import time
import urllib.request
import urllib.error

FIREBASE_URL = os.environ.get("FIREBASE_URL", "")

BASLIKLAR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}

KAYNAKLAR = [
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

BEKLEME = 0.4   # Migros'u bogmamak icin istekler arasi bekleme (saniye)


def istek(url):
    r = urllib.request.Request(url, headers=BASLIKLAR)
    try:
        with urllib.request.urlopen(r, timeout=20) as c:
            return json.loads(c.read().decode("utf-8"))
    except Exception as e:
        print(f"    [HATA] {type(e).__name__}")
        return None


def liste_cek(url):
    veri = istek(url)
    if not veri:
        return []
    try:
        return veri["data"]["searchInfo"]["storeProductInfos"]
    except (KeyError, TypeError):
        print("    [HATA] Liste yapisi beklendigi gibi degil")
        return []


def detay_cek(detay_kalibi, sku):
    temiz_sku = sku.lstrip("0")   # "08040900" -> "8040900"
    veri = istek(detay_kalibi.format(sku=temiz_sku))
    if not veri:
        return None
    return veri.get("data", {}).get("storeProductInfoDTO")


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


def tl(kurus):
    return round(kurus / 100, 2) if kurus else 0.0


if __name__ == "__main__":
    if not FIREBASE_URL:
        print("HATA: FIREBASE_URL yok (GitHub Secrets kontrol et)")
        raise SystemExit(1)

    print("Ucuzcum Botu v4 basladi.\n")
    toplam = 0
    herkese_sayisi = 0
    money_sayisi = 0
    dusenler = []

    for kaynak in KAYNAKLAR:
        ad = kaynak["kaynak"]
        print(f"{'=' * 58}")
        print(f"{ad}")
        print(f"{'=' * 58}")

        adaylar = liste_cek(kaynak["liste"])
        print(f"Listeden {len(adaylar)} urun geldi.\n")

        for aday in adaylar:
            if not aday.get("discountRate"):    # indirimsizleri ele
                continue

            sku = aday.get("sku", "")
            urun_id = str(aday.get("id", ""))
            if not sku or not urun_id:
                continue

            time.sleep(BEKLEME)
            dto = detay_cek(kaynak["detay"], sku)
            if not dto:
                continue

            reg = dto.get("regularPrice")
            sale = dto.get("salePrice")     # kartsiz musterinin odedigi
            loy = dto.get("loyaltyPrice")   # Money Kart'li fiyat

            herkese_indirim = bool(sale and reg and sale < reg)
            money_indirim = bool(loy and sale and loy < sale)

            if not (herkese_indirim or money_indirim):
                continue

            # Kullanicinin gercekten odeyecegi en dusuk fiyat
            gecerli = tl(loy) if money_indirim else tl(sale)
            eski = onceki_fiyati_al(urun_id)

            urun = {
                "urun_adi": dto.get("name", "Bilinmiyor"),
                "normal_fiyat": tl(reg),
                "herkese_fiyat": tl(sale),
                "money_fiyat": tl(loy),
                "gecerli_fiyat": gecerli,
                "onceki_fiyat": eski,
                "indirim_orani": dto.get("discountRate", 0),
                "herkese_indirim": herkese_indirim,
                "market": "Migros",
                "kaynak": ad,
                "gorsel": (dto.get("images") or [{}])[0]
                          .get("urls", {}).get("PRODUCT_LIST", ""),
                "guncelleme": int(time.time()),
            }

            if herkese_indirim:
                tur = "HERKESE"
                herkese_sayisi += 1
            else:
                tur = "MONEY  "
                money_sayisi += 1

            satir = f"  {urun['urun_adi'][:40]:40} {gecerli:>8.2f} TL  [{tur} %{urun['indirim_orani']}]"

            if eski and gecerli < eski:
                fark = round(eski - gecerli, 2)
                satir += f"  <<< DUSTU (-{fark} TL)"
                dusenler.append((urun["urun_adi"], eski, gecerli))

            print(satir)

            if firebase_yaz(f"urunler/{urun_id}", urun):
                toplam += 1

        print()

    print("=" * 58)
    print(f"Bitti. {toplam} indirimli urun Firebase'e yazildi.")
    print(f"  Herkese acik indirim : {herkese_sayisi}")
    print(f"  Sadece Money Kart    : {money_sayisi}")

    if dusenler:
        print(f"\n{len(dusenler)} URUNDE FIYAT DUSUSU:")
        for ad_, e, y in dusenler:
            print(f"  - {ad_}: {e} -> {y} TL")
    else:
        print("\nBu turda fiyat dususu yok.")
import json
import requests
from bs4 import BeautifulSoup

# --- 1. SENİN MEVCUT MİGROS KODUN ---
def migros_indirimleri_cek():
    # Burada senin hali hazırda çalışan Migros kodların olacak.
    # Sonucunda Migros ürünlerini liste olarak döndürmesi lazım.
    migros_urunleri = [] 
    # ... senin işlemlerin ...
    return migros_urunleri

# --- 2. BENİM VERDİĞİM A101 KODU ---
def a101_indirimleri_cek():
    url = "https://www.a101.com.tr/haftanin-yildizlari"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.content, "html.parser")
    
    a101_urunleri = []
    kartlar = soup.find_all("li", class_="col-md-4 col-sm-6 col-xs-6 set-product-item")
    
    for kart in kartlar:
        try:
            isim = kart.find("h3", class_="name").text.strip()
            yeni_fiyat = kart.find("span", class_="current").text.strip()
            eski_fiyat_etiketi = kart.find("s")
            eski_fiyat = eski_fiyat_etiketi.text.strip() if eski_fiyat_etiketi else yeni_fiyat
            resim = kart.find("img")["data-src"] if kart.find("img").has_attr("data-src") else kart.find("img")["src"]
            
            a101_urunleri.append({
                "market": "A101",
                "isim": isim,
                "eski_fiyat": eski_fiyat,
                "yeni_fiyat": yeni_fiyat,
                "resim_url": resim
            })
        except AttributeError:
            continue
            
    return a101_urunleri

# --- 3. ANA ÇALIŞMA VE BİRLEŞTİRME ALANI ---
if __name__ == "__main__":
    print("Veriler çekiliyor...")
    
    # İki marketin verisini de alıyoruz
    migros_verisi = migros_indirimleri_cek()
    a101_verisi = a101_indirimleri_cek()
    
    # İKİ MARKETİ TEK LİSTEDE BİRLEŞTİR (Parayı getirecek hamle)
    tum_indirimler = migros_verisi + a101_verisi
    
    # JSON dosyasına kaydet
    with open("indirimler.json", "w", encoding="utf-8") as dosya:
        json.dump(tum_indirimler, dosya, ensure_ascii=False, indent=4)
        
    print(f"Başarılı! Toplam {len(tum_indirimler)} ürün JSON dosyasına yazıldı.")
