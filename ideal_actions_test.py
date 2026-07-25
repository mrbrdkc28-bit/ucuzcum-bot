"""
GitHub Actions'tan Ideal erisim testi.
Botun calistigi ortamdan (Azure IP) Ideal API'sine ulasilabiliyor mu?
"""
import json, urllib.request, urllib.error

B = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
     "Accept":"application/json", "Accept-Language":"tr-TR,tr;q=0.9",
     "Referer":"https://www.ideal.com.tr/", "Origin":"https://www.ideal.com.tr",
     "sec-fetch-dest":"empty","sec-fetch-mode":"cors","sec-fetch-site":"same-origin"}

url = "https://www.ideal.com.tr/api/homepage"
print("Ideal API testi (GitHub Actions / Azure IP)")
print("URL:", url)
try:
    with urllib.request.urlopen(urllib.request.Request(url, headers=B), timeout=20) as c:
        d = json.loads(c.read().decode("utf-8"))
    print("SONUC: BASARILI (durum 200)")
    ind = d.get("data",{}).get("indirim",[])
    dolu = [u for u in ind if u.get("list_price")]
    print(f"  indirim kaynagi: {len(ind)} urun, {len(dolu)} tanesi indirimli")
    print("  >> Ideal Actions'tan ERISILEBILIR. Bota eklenebilir.")
except urllib.error.HTTPError as h:
    print(f"SONUC: ENGELLI (durum {h.code})")
    print("  >> Ideal Actions'tan erisilemiyor. Bota EKLENEMEZ.")
except Exception as e:
    print(f"SONUC: HATA {type(e).__name__}: {str(e)[:80]}")
