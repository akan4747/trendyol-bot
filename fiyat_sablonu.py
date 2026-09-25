#!/usr/bin/env python3
"""
Fiyat Şablonu Sistemi
----------------------
Mantık (senin anlattığın haliyle):
  - Her ÜRÜN TİPİ (kolye, küpe, bileklik...) için bir "fiyat tablosu" var.
  - Bu tablo: {ölçü, renk} kombinasyonu -> sabit fiyat.
  - Bir kombinasyonun fiyatını BİR KERE girersin, o andan itibaren o ürün
    tipindeki TÜM tasarımlar (100'lük seri boyunca) aynı kombinasyon için
    aynı fiyatı kullanır.
  - Ölçüler ürün tipine göre değişebilir (kolye: 1cm/1.5cm/2cm,
    bileklik: 22cm gibi) - sabit bir liste değil, esnek.
  - Yeni bir kombinasyon (örn. daha önce hiç kullanılmamış bir ölçü)
    ilk geldiğinde tek seferlik sorulur, cevap şablona kalıcı eklenir.

Şablon dosyası (fiyat_sablonlari.json) GitHub reposunda tutulur ki
GitHub Actions her çalıştığında aynı şablonu görsün.
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SABLON_PATH = os.path.join(BASE_DIR, "fiyat_sablonlari.json")


def _kombinasyon_anahtari(olcu, renk):
    """{'1.5 cm', 'Gold'} -> '1.5 cm|Gold'  (tutarlı, boşluk/büyük-küçük harf
    farklarından etkilenmeyen bir anahtar üretir)."""
    return f"{olcu.strip().lower()}|{renk.strip().lower()}"


def sablonlari_yukle():
    if not os.path.exists(SABLON_PATH):
        return {}
    with open(SABLON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def sablonlari_kaydet(sablonlar):
    with open(SABLON_PATH, "w", encoding="utf-8") as f:
        json.dump(sablonlar, f, ensure_ascii=False, indent=2)


def fiyat_al(urun_tipi, olcu, renk):
    """Bu kombinasyon için daha önce kayıtlı bir fiyat varsa döner, yoksa None."""
    sablonlar = sablonlari_yukle()
    tip_tablosu = sablonlar.get(urun_tipi.strip().lower(), {})
    return tip_tablosu.get(_kombinasyon_anahtari(olcu, renk))


def fiyat_kaydet(urun_tipi, olcu, renk, fiyat):
    """Bir kombinasyon için fiyatı kalıcı olarak kaydeder/günceller."""
    sablonlar = sablonlari_yukle()
    tip_anahtari = urun_tipi.strip().lower()
    tip_tablosu = sablonlar.setdefault(tip_anahtari, {})
    tip_tablosu[_kombinasyon_anahtari(olcu, renk)] = float(fiyat)
    sablonlari_kaydet(sablonlar)


def eksik_kombinasyonlari_bul(urun_tipi, olculer, renkler):
    """Bir ürün tipi için gereken TÜM {ölçü x renk} kombinasyonlarını
    üretir, hangilerinin fiyatı henüz kayıtlı DEĞİL onu döner.
    Telegram'da 'şu kombinasyonların fiyatını gir' diye sormak için kullanılır.

    Döndürdüğü liste boşsa -> hepsi zaten kayıtlı, hiçbir şey sormaya gerek yok,
    direkt varyantları oluşturabiliriz.
    """
    eksikler = []
    for olcu in olculer:
        for renk in renkler:
            if fiyat_al(urun_tipi, olcu, renk) is None:
                eksikler.append({"olcu": olcu, "renk": renk})
    return eksikler


def tum_varyant_fiyatlarini_getir(urun_tipi, olculer, renkler):
    """Bir tasarımın TÜM varyantları için (ölçü x renk) fiyat sözlüğü döner.
    Önce eksik_kombinasyonlari_bul ile hiçbir eksik kalmadığından emin ol,
    sonra bunu çağır."""
    sonuc = {}
    for olcu in olculer:
        for renk in renkler:
            fiyat = fiyat_al(urun_tipi, olcu, renk)
            if fiyat is None:
                raise ValueError(
                    f"Eksik fiyat: '{urun_tipi}' / {olcu} / {renk} - "
                    f"önce eksik_kombinasyonlari_bul() ile kontrol et."
                )
            sonuc[(olcu, renk)] = fiyat
    return sonuc


if __name__ == "__main__":
    # ---- Basit kendi kendine test (gerçek API'ye dokunmaz) ----
    print("Test: 'kolye' için örnek fiyatlar kaydediliyor...")
    ornek_fiyatlar = {
        ("1.5 cm", "Gold"): 3300,
        ("1.5 cm", "Gümüş"): 3500,
        ("1.5 cm", "Rose"): 3600,
        ("2 cm", "Gold"): 3800,
        ("2 cm", "Gümüş"): 4000,
        ("2 cm", "Rose"): 4100,
        ("1 cm", "Gold"): 4300,
        ("1 cm", "Gümüş"): 4500,
        ("1 cm", "Rose"): 4600,
    }
    for (olcu, renk), fiyat in ornek_fiyatlar.items():
        fiyat_kaydet("kolye", olcu, renk, fiyat)

    olculer = ["1 cm", "1.5 cm", "2 cm"]
    renkler = ["Gold", "Gümüş", "Rose"]

    eksikler = eksik_kombinasyonlari_bul("kolye", olculer, renkler)
    print(f"'kolye' için eksik kombinasyon sayısı: {len(eksikler)} (0 olmalı)")

    yeni_eksikler = eksik_kombinasyonlari_bul("bileklik", ["22 cm"], renkler)
    print(f"'bileklik' (hiç girilmemiş) için eksik kombinasyonlar: {yeni_eksikler}")

    varyant_fiyatlari = tum_varyant_fiyatlarini_getir("kolye", olculer, renkler)
    print("\n'kolye' tüm varyant fiyatları:")
    for (olcu, renk), fiyat in sorted(varyant_fiyatlari.items(), key=lambda x: x[1]):
        print(f"  {olcu:8s} {renk:8s} -> {fiyat} TL")

    # Test dosyasını temizle (gerçek kullanımda SİLİNMEYECEK, bu sadece demo)
    os.remove(SABLON_PATH)
    print("\n[Test tamamlandı, demo dosyası temizlendi]")
