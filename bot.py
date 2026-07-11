import json
import os
import time
import urllib.request
import urllib.error

# Takip edilecek Migros urun ID'leri
TAKIP_LISTESI = [
    "7038030",   # Ulker Bol Sutlu Kare Cikolata 60 G
]

# Firebase adresi GitHub Secrets'tan gelecek (kod icinde acikta durmasin)
FIREBASE_URL = os.environ.get("FIREBASE_URL", "")

API = "https://www.migros.com.tr/rest/products/screens/{id}"

BASLIKLAR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}


def urun_cek(urun_id):
    istek = urllib.request.Request(API.format(id=urun_id), headers=BASLIKLAR)
    try:
        with urllib.request.urlopen(istek, timeout=20) as cevap:
            return json.loads(cevap.read().decode("utf-8"))
    except Exception as e:
        print(f"  [ATLANDI] {urun_id}: {type(e).__name__}")
        return None


def urun_isle(ham):
    dto = ham.get("data", {}).get("storeProductInfoDTO")
    if not dto:
        return None

    def tl(k):
        return round(k / 100, 2) if k else 0.0

    reg = dto.get("regularPrice")
    sale = dto.get("salePrice")
    loy = dto.get("loyaltyPrice")

    herkese = bool(sale and reg and sale < reg)
    moneyi = bool(loy and sale and loy < sale)

    urun = {
        "urun_adi": dto.get("name", "Bilinmiyor"),
        "normal_fiyat": tl(reg),
        "herkese_fiyat": tl(sale),
        "money_fiyat": tl(loy),
        "indirim_orani": dto.get("discountRate", 0),
        "herkese_indirim": herkese,
        "market": "Migros",
        "guncelleme": int(time.time()),
    }
    return urun, (herkese or moneyi)


def firebase_yaz(yol, veri):
    url = f"{FIREBASE_URL}/{yol}.json"
    istek = urllib.request.Request(
        url,
        data=json.dumps(veri).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(istek, timeout=20) as cevap:
            return cevap.status == 200
    except Exception as e:
        print(f"  [FIREBASE HATA] {type(e).__name__}")
        return False


if __name__ == "__main__":
    if not FIREBASE_URL:
        print("HATA: FIREBASE_URL bulunamadi. GitHub Secrets'a eklendi mi?")
        raise SystemExit(1)

    print("Ucuzcum Botu basladi.")
    yazilan = 0

    for urun_id in TAKIP_LISTESI:
        ham = urun_cek(urun_id)
        if not ham:
            continue

        sonuc = urun_isle(ham)
        if not sonuc:
            continue

        urun, indirim_var = sonuc
        print(f"* {urun['urun_adi']}")
        print(f"    kartsiz: {urun['herkese_fiyat']} TL | money: {urun['money_fiyat']} TL")

        if indirim_var:
            if firebase_yaz(f"urunler/{urun_id}", urun):
                yazilan += 1
                print("    [OK] Firebase'e yazildi")

    print(f"Bitti. {yazilan} urun guncellendi.")
