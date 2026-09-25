#!/usr/bin/env python3
"""
Trendyol Katalog Yardımcıları
------------------------------
Bu modül, ürün yüklerken ihtiyaç duyulan ve elle girilmesi yerine
Trendyol'un kendi API'sinden otomatik çekilmesi gereken bilgileri sağlar:

  1) Kategori ağacı  -> ürün tipi kelimesinden (örn. "küpe") doğru
     kategoriyi otomatik bulmak için (find_category)
  2) Kategori özellikleri (attributes) -> o kategoriye ürün eklerken
     ZORUNLU olan alanları bulmak için (get_required_attributes)
  3) Marka listesi -> marka ID'sini isimden bulmak için (find_brand)

NEDEN ÖNBELLEK (CACHE) VAR?
  Kategori ağacı binlerce kategori içerir ve sık değişmez. Her ürün
  eklemede baştan çekmek yerine, bir kere çekip 'katalog_cache.json'
  dosyasına kaydediyoruz. 24 saatten eskiyse otomatik tazelenir.

KULLANIM (örnek):
    from trendyol_katalog import find_category, get_required_attributes

    kategori = find_category(cfg, "küpe")
    print(kategori["id"], kategori["name"])

    zorunlu_ozellikler = get_required_attributes(cfg, kategori["id"])
    for oz in zorunlu_ozellikler:
        print(oz["name"], "->", [v["name"] for v in oz["values"]])
"""

import json
import os
import time
import base64
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "katalog_cache.json")
CACHE_GECERLILIK_SANIYE = 24 * 60 * 60  # 24 saat

TRENDYOL_BASE = "https://apigw.trendyol.com/integration"


# ---------------------------------------------------------------------------
# Trendyol_notify.py ile aynı yetkilendirme mantığı (tutarlılık için)
# ---------------------------------------------------------------------------

def trendyol_auth_header(cfg):
    token = base64.b64encode(
        f"{cfg['trendyol']['api_key']}:{cfg['trendyol']['api_secret']}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {token}",
        "User-Agent": f"{cfg['trendyol']['seller_id']} - SelfIntegration",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Önbellek yardımcıları
# ---------------------------------------------------------------------------

def _cache_yukle():
    if not os.path.exists(CACHE_PATH):
        return {}
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _cache_kaydet(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _cache_taze_mi(cache, anahtar):
    girdi = cache.get(anahtar)
    if not girdi:
        return False
    return (time.time() - girdi.get("_cekilme_zamani", 0)) < CACHE_GECERLILIK_SANIYE


# ---------------------------------------------------------------------------
# Kategori ağacı
# ---------------------------------------------------------------------------

def _kategori_agacini_cek(cfg):
    """Trendyol'dan TÜM kategori ağacını çeker (tek seferlik, büyük istek)."""
    url = f"{TRENDYOL_BASE}/product/product-categories"
    resp = requests.get(url, headers=trendyol_auth_header(cfg), timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Kategori ağacı çekilemedi: {resp.status_code} {resp.text}")
    return resp.json().get("categories", [])


def _agaci_duzlestir(kategoriler, sonuc=None):
    """İç içe geçmiş kategori ağacını tek boyutlu bir listeye çevirir.
    Sadece 'subCategories' boş olan (yani en alt seviye / yaprak) kategorileri
    işaretler, çünkü Trendyol'da ürün SADECE yaprak kategoriye eklenebilir."""
    if sonuc is None:
        sonuc = []
    for k in kategoriler:
        alt_kategoriler = k.get("subCategories") or []
        yaprak_mi = len(alt_kategoriler) == 0
        sonuc.append({
            "id": k.get("id"),
            "name": k.get("name"),
            "parentId": k.get("parentId"),
            "leaf": yaprak_mi,
        })
        if alt_kategoriler:
            _agaci_duzlestir(alt_kategoriler, sonuc)
    return sonuc


def get_kategori_listesi(cfg, zorla_tazele=False):
    """Düzleştirilmiş kategori listesini döner (önbellekten veya API'den)."""
    cache = _cache_yukle()
    if not zorla_tazele and _cache_taze_mi(cache, "kategoriler"):
        return cache["kategoriler"]["liste"]

    ham = _kategori_agacini_cek(cfg)
    liste = _agaci_duzlestir(ham)
    cache["kategoriler"] = {"_cekilme_zamani": time.time(), "liste": liste}
    _cache_kaydet(cache)
    return liste


def find_category(cfg, urun_tipi_kelime, zorla_tazele=False):
    """Ürün tipi kelimesinden (örn. 'küpe', 'kolye', 'bileklik') en uygun
    YAPRAK kategoriyi bulur. Birden fazla eşleşme varsa hepsini döner,
    tek eşleşme varsa direkt onu döner.

    NOT: Trendyol kategori isimleri Türkçe ve bazen 'Gümüş Küpe',
    'Küpe' gibi farklılık gösterebilir - bu yüzden 'kelime kategori
    isminin İÇİNDE geçiyor mu' şeklinde arıyoruz, birebir eşleşme aramıyoruz.
    """
    liste = get_kategori_listesi(cfg, zorla_tazele=zorla_tazele)
    kelime = urun_tipi_kelime.strip().lower()

    eslesmeler = [
        k for k in liste
        if k["leaf"] and kelime in (k["name"] or "").lower()
    ]

    if not eslesmeler:
        return None
    if len(eslesmeler) == 1:
        return eslesmeler[0]

    # Birden fazla eşleşme varsa, en kısa isimli olanı tercih et
    # (genelde en genel / en doğru kategori budur, örn. "Küpe" vs "Küpe Ucu Seti")
    eslesmeler.sort(key=lambda k: len(k["name"] or ""))
    return eslesmeler[0]


def kategori_secenekleri(cfg, urun_tipi_kelime, zorla_tazele=False):
    """find_category tek bir sonuca karar veremediğinde (0 ya da 2+ eşleşme),
    Telegram'da kullanıcıya sorulacak seçenek listesini döner."""
    liste = get_kategori_listesi(cfg, zorla_tazele=zorla_tazele)
    kelime = urun_tipi_kelime.strip().lower()
    return [k for k in liste if k["leaf"] and kelime in (k["name"] or "").lower()]


# ---------------------------------------------------------------------------
# Kategori özellikleri (attributes) - v2
# ---------------------------------------------------------------------------

def _ozellikleri_cek(cfg, category_id):
    url = f"{TRENDYOL_BASE}/product/categories/{category_id}/attributes"
    resp = requests.get(url, headers=trendyol_auth_header(cfg), timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Kategori özellikleri çekilemedi (id={category_id}): "
                            f"{resp.status_code} {resp.text}")
    return resp.json()


def get_kategori_ozellikleri(cfg, category_id, zorla_tazele=False):
    """Bir kategorinin TÜM özelliklerini (zorunlu + opsiyonel) döner,
    her özelliğin alabileceği değerlerle birlikte."""
    cache = _cache_yukle()
    anahtar = f"ozellikler_{category_id}"
    if not zorla_tazele and _cache_taze_mi(cache, anahtar):
        return cache[anahtar]["liste"]

    ham = _ozellikleri_cek(cfg, category_id)
    liste = []
    for oz in ham.get("categoryAttributes", []):
        attr = oz.get("attribute", {})
        liste.append({
            "attributeId": attr.get("id"),
            "name": attr.get("name"),
            "required": oz.get("required", False),
            "allowCustom": oz.get("allowCustom", False),
            "allowMultiple": oz.get("allowMultipleAttributeValues", False),
            "values": [
                {"id": v.get("id"), "name": v.get("name")}
                for v in oz.get("attributeValues", [])
            ],
        })

    cache[anahtar] = {"_cekilme_zamani": time.time(), "liste": liste}
    _cache_kaydet(cache)
    return liste


def get_required_attributes(cfg, category_id, zorla_tazele=False):
    """Sadece ZORUNLU özellikleri döner - ürün oluştururken bunları
    doldurmazsak Trendyol isteği reddeder."""
    tumu = get_kategori_ozellikleri(cfg, category_id, zorla_tazele=zorla_tazele)
    return [oz for oz in tumu if oz["required"]]


def eksik_zorunlu_ozellikler(cfg, category_id, doldurulmus_attribute_idler):
    """Elimizde hangi attributeId'ler için değer var (renk, uzunluk gibi
    kendi sistemimizden gelenler), hangileri hâlâ eksik - onu bulur.
    Telegram'da 'şu bilgi eksik, tamamlar mısın' diye sormak için kullanılır."""
    zorunlular = get_required_attributes(cfg, category_id)
    return [oz for oz in zorunlular if oz["attributeId"] not in doldurulmus_attribute_idler]


# ---------------------------------------------------------------------------
# Marka listesi
# ---------------------------------------------------------------------------

def get_marka_listesi(cfg, zorla_tazele=False):
    cache = _cache_yukle()
    if not zorla_tazele and _cache_taze_mi(cache, "markalar"):
        return cache["markalar"]["liste"]

    url = f"{TRENDYOL_BASE}/product/brands"
    resp = requests.get(url, headers=trendyol_auth_header(cfg), timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Marka listesi çekilemedi: {resp.status_code} {resp.text}")

    liste = [{"id": b.get("id"), "name": b.get("name")} for b in resp.json().get("brands", [])]
    cache["markalar"] = {"_cekilme_zamani": time.time(), "liste": liste}
    _cache_kaydet(cache)
    return liste


def find_brand(cfg, marka_adi, zorla_tazele=False):
    liste = get_marka_listesi(cfg, zorla_tazele=zorla_tazele)
    adi = marka_adi.strip().lower()
    for m in liste:
        if (m["name"] or "").strip().lower() == adi:
            return m
    return None


if __name__ == "__main__":
    print("Bu modül tek başına çalıştırılmaz, trendyol_urun_yukle.py içinden import edilir.")
    print("Test etmek için gerçek config.json / GitHub Secrets bilgileriyle bir test scripti çalıştır.")
