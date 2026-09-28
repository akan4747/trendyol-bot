#!/usr/bin/env python3
"""
Trendyol -> Telegram Bildirim Botu
-----------------------------------
Bu script Trendyol Satıcı API'sinden:
  1) Yeni SİPARİŞLERİ
  2) Ürün değişikliği / iade taleplerini (Claims)
  3) Müşteri SORULARINI (ürün görseli dahil)
çekip Telegram'a mesaj olarak gönderir.

ÇALIŞTIRMA MANTIĞI:
  Script bir kez çalışıp çıkar ("run-once"). Sürekli bildirim almak için
  bunu cron ile her 5 dakikada bir çalıştırman yeterli (aşağıdaki kuruluma bak).
  Daha önce gönderilen siparişleri/soruları tekrar göndermemek için
  'state.json' dosyasında son görülen ID'leri saklar.

KURULUM:
  1) config.json dosyasını doldur (Trendyol + Telegram bilgileri)
  2) pip install requests
  3) python3 trendyol_notify.py   (ilk çalıştırmada test amaçlı elle dene)
  4) crontab -e ile her 5 dakikada bir çalışacak şekilde ekle:
       */5 * * * * /usr/bin/python3 /path/to/trendyol_notify.py >> /path/to/trendyol_notify.log 2>&1
"""

import json
import os
from html import escape as html_escape
import sys
import base64
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TURKEY_TZ = ZoneInfo("Europe/Istanbul")


def now_tr():
    """Türkiye saatiyle 'şu an'ı döner.
    ÖNEMLİ: GitHub Actions sunucuları UTC (İngiltere) saatini kullanır,
    normal datetime.now() kullanırsak tüm saat mantığımız (10:30, 14:00,
    16:30 gibi) 3 saat kayar. Bu yüzden HER YERDE bu fonksiyon kullanılmalı."""
    return datetime.now(TURKEY_TZ)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

TRENDYOL_BASE = "https://apigw.trendyol.com/integration"

# Kargoya henüz verilmemiş sayılan sipariş durumları.
# Trendyol farklı bir statü ismi bekliyorsa (örn. "Picking" yerine başka bir şey),
# bunu ihtiyaca göre güncelleyebiliriz.
UNSHIPPED_STATUSES = ["Created", "Picking", "Invoiced"]


# ---------------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ---------------------------------------------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def trendyol_auth_header(cfg):
    token = base64.b64encode(
        f"{cfg['trendyol']['api_key']}:{cfg['trendyol']['api_secret']}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {token}",
        # Trendyol, User-Agent header'ında SellerId ister
        "User-Agent": f"{cfg['trendyol']['seller_id']} - SelfIntegration",
        "Content-Type": "application/json",
    }


def send_telegram_message(cfg, text, photo_url=None, reply_markup=None, chat_id=None,
                          silent=False):
    """Telegram'a metin (ve varsa fotoğraf, varsa buton) gönderir.
    chat_id verilmezse varsayılan (ana kargo) grubuna gönderir.
    silent=True ise bildirim sesi/titreşimi olmadan gönderir.
    Başarılıysa gönderilen mesajın numarasını (message_id), değilse None döner."""
    bot_token = cfg["telegram"]["bot_token"]
    chat_id = chat_id or cfg["telegram"]["chat_id"]

    extra_data = {}
    if reply_markup:
        extra_data["reply_markup"] = json.dumps(reply_markup)
    if silent:
        extra_data["disable_notification"] = "true"

    try:
        if photo_url:
            url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": text[:1024], "parse_mode": "HTML", **extra_data},
                params={"photo": photo_url},
                timeout=20,
            )
            # Eğer görsel URL'i Telegram tarafından çekilemezse düz metne düş
            if not resp.ok:
                raise RuntimeError(resp.text)
        else:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True, **extra_data},
                timeout=20,
            )
        if not resp.ok:
            print(f"[TELEGRAM HATA] {resp.status_code}: {resp.text}")
            return None
        return resp.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"[TELEGRAM GONDERIM HATASI] {e}")
        # Görselli gönderim başarısız olduysa düz metinle tekrar dene
        if photo_url:
            return send_telegram_message(cfg, text, photo_url=None, reply_markup=reply_markup,
                                         chat_id=chat_id, silent=silent)
        return None


def delete_telegram_message(cfg, chat_id, message_id):
    """Daha önce gönderilmiş bir mesajı siler (örn. eski 'Bekleyen kargolar'
    mesajını, grupta hep tek bir tane kalsın diye)."""
    if not message_id:
        return
    bot_token = cfg["telegram"]["bot_token"]
    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "message_id": message_id}, timeout=15)
    except Exception as e:
        print(f"[TELEGRAM MESAJ SİLME HATASI] {e}")


def answer_callback_query(cfg, callback_query_id, text=None):
    """Kullanıcı bir butona (örn. 'Kargoya Verdim') bastığında, Telegram'a
    'aldım, işledim' bilgisini gönderir. Bu sayede buton üzerindeki
    yüklenme animasyonu kaybolur ve küçük bir onay mesajı görünür."""
    bot_token = cfg["telegram"]["bot_token"]
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    try:
        requests.post(
            url,
            data={"callback_query_id": callback_query_id, "text": text or "Kaydedildi ✅"},
            timeout=10,
        )
    except Exception as e:
        print(f"[TELEGRAM CALLBACK YANIT HATASI] {e}")


def build_shipped_button(order_number):
    """'Kargoya Verdim' butonunu oluşturur. Butona basılınca Telegram,
    callback_data içindeki bu sipariş numarasını bize geri bildirir."""
    return {
        "inline_keyboard": [[
            {"text": "📦 Kargoya Verdim", "callback_data": f"shipped:{order_number}"}
        ]]
    }


def build_shipped_confirmed_button():
    """Onaylandıktan sonra butonun yeni (tıklanınca bir şey yapmayan) hâli."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Kargoya Verildi", "callback_data": "noop"}
        ]]
    }


def shipped_button_text(order_number, customer_name=""):
    """Özel klavye butonunun üzerindeki yazı. Bu yazı, mevcut
    'kargo çıktı <no>' metin algılama sistemiyle uyumlu olacak şekilde
    kasıtlı olarak 'kargo' ve 'çıktı' kelimelerini içeriyor — buna basınca
    bu yazı OLDUĞU GİBİ mesaj olarak gönderilir, bot onu otomatik yakalar.
    Alıcı ismi de eklenir, hangi paketin hangisi olduğunu ayırt etmek kolaylaşsın diye."""
    text = f"📦 Kargo Çıktı {order_number}"
    if customer_name:
        text += f" - {customer_name}"
    return text


def build_reply_keyboard(orders_info):
    """Grubun mesaj yazma alanının ÜSTÜNDE duran, tıklanınca ANINDA
    (bot'un uyanmasını beklemeden) mesaj gönderen özel klavyeyi oluşturur.
    orders_info: [(order_number, customer_name), ...] listesi.
    Her bekleyen sipariş için bir buton olur. Sipariş kalmazsa None döner
    (klavyeyi kaldırmak için)."""
    if not orders_info:
        return None
    buttons = [[{"text": shipped_button_text(on, name)}] for on, name in orders_info]
    return {"keyboard": buttons, "resize_keyboard": True, "is_persistent": True}


def remove_reply_keyboard():
    """Özel klavyeyi kaldırır (bekleyen sipariş kalmadığında)."""
    return {"remove_keyboard": True}


def sync_reply_keyboard(cfg, state, pending_orders_info):
    """Özel klavyeyi, o an gerçekten bekleyen siparişlerle eşleşecek şekilde
    günceller. pending_orders_info: [(order_number, customer_name), ...]
    Liste değişmediyse hiçbir şey yapmaz (gereksiz mesaj atmaz).
    Mesaj SESSİZ gönderilir (telefon titremez) ve bir önceki 'Bekleyen kargolar'
    mesajı silinir — grupta hep tek bir tane kalır."""
    pending_sorted = sorted((str(on), name) for on, name in pending_orders_info)
    # Sadece sipariş numaralarını karşılaştırıyoruz (isim değişmez zaten, gereksiz tetiklenmesin)
    pending_numbers_only = [on for on, _ in pending_sorted]
    if state.get("reply_keyboard_signature") == pending_numbers_only:
        return  # zaten güncel, tekrar göndermeye gerek yok

    if pending_sorted:
        markup = build_reply_keyboard(pending_sorted)
        text = ("📋 <b>Bekleyen kargolar</b> — kargoya verdiğin siparişin "
                "butonuna bas, mesaj otomatik gönderilsin:")
    else:
        markup = remove_reply_keyboard()
        text = "✅ Şu an bekleyen kargolanmamış sipariş yok."

    chat_id = cfg["telegram"]["chat_id"]
    new_message_id = send_telegram_message(cfg, text, reply_markup=markup, silent=True)

    # Yeni mesaj başarıyla gittiyse, eskisini sil
    if new_message_id:
        old_message_id = state.get("reply_keyboard_message_id")
        if old_message_id:
            delete_telegram_message(cfg, chat_id, old_message_id)
        state["reply_keyboard_message_id"] = new_message_id
        state["reply_keyboard_signature"] = pending_numbers_only


def edit_message_reply_markup(cfg, chat_id, message_id, reply_markup):
    """Zaten gönderilmiş bir mesajın ALTINDAKİ BUTONU değiştirir
    (örn. 'Kargoya Verdim' -> '✅ Kargoya Verildi')."""
    bot_token = cfg["telegram"]["bot_token"]
    url = f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": json.dumps(reply_markup),
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"[TELEGRAM BUTON GÜNCELLEME HATASI] {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[TELEGRAM BUTON GÜNCELLEME HATASI] {e}")


# ---------------------------------------------------------------------------
# Trendyol veri çekme fonksiyonları
# ---------------------------------------------------------------------------

def fetch_new_orders(cfg, state):
    """Son 1 saat içindeki siparişleri çeker, daha önce bildirilmemiş olanları döner."""
    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/order/sellers/{seller_id}/orders"

    start_date = int((now_tr() - timedelta(hours=1)).timestamp() * 1000)
    end_date = int(now_tr().timestamp() * 1000)

    params = {
        "startDate": start_date,
        "endDate": end_date,
        "orderByField": "PackageLastModifiedDate",
        "orderByDirection": "DESC",
        "size": 50,
        "page": 0,
    }

    resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=30)
    if not resp.ok:
        print(f"[SIPARIS ÇEKME HATASI] {resp.status_code}: {resp.text}")
        return []

    data = resp.json()
    packages = data.get("content", [])

    seen_ids = set(state.get("seen_order_package_ids", []))
    new_packages = [p for p in packages if str(p.get("id")) not in seen_ids]

    # State güncelle
    for p in packages:
        seen_ids.add(str(p.get("id")))
    state["seen_order_package_ids"] = list(seen_ids)[-2000:]  # şişmesin diye sınırla

    return new_packages


def fetch_new_claims(cfg, state):
    """İade / ürün değişikliği taleplerini (Claims) çeker."""
    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/order/sellers/{seller_id}/claims"

    params = {"page": 0, "size": 50}

    resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=30)
    if not resp.ok:
        print(f"[CLAIM ÇEKME HATASI] {resp.status_code}: {resp.text}")
        return []

    data = resp.json()
    claims = data.get("content", [])

    seen_ids = set(state.get("seen_claim_ids", []))
    new_claims = [c for c in claims if str(c.get("id")) not in seen_ids]

    for c in claims:
        seen_ids.add(str(c.get("id")))
    state["seen_claim_ids"] = list(seen_ids)[-2000:]

    return new_claims


def fetch_new_questions(cfg, state):
    """Müşteri sorularını / sipariş notlarını çeker.
    ÖNEMLİ: Statü filtresi kasıtlı olarak kullanılmıyor — bazı sorular/notlar
    çok hızlı 'ANSWERED' (cevaplanmış) durumuna geçebiliyor, sadece
    'WAITING_FOR_ANSWER' filtrelersek bunları kaçırırdık. Bunun yerine HER
    yeni soruyu (durumu ne olursa olsun) yakalıyoruz, tekrar bildirmemek için
    'seen_question_ids' ile takip ediyoruz."""
    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/qna/sellers/{seller_id}/questions/filter"

    params = {
        "page": 0,
        "size": 50,
        "orderByField": "CreatedDate",
        "orderByDirection": "DESC",
    }

    resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=30)
    if not resp.ok:
        print(f"[SORU ÇEKME HATASI] {resp.status_code}: {resp.text}")
        return []

    data = resp.json()
    questions = data.get("content", [])

    seen_ids = set(state.get("seen_question_ids", []))
    new_questions = [q for q in questions if str(q.get("id")) not in seen_ids]

    for q in questions:
        seen_ids.add(str(q.get("id")))
    state["seen_question_ids"] = list(seen_ids)[-2000:]

    return new_questions


def find_order_by_number(cfg, order_number):
    """Verilen sipariş numarasına ait siparişi Trendyol'dan çeker.
    Soru metninde geçen sipariş numarasını otomatik eşleştirmek için kullanılır."""
    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/order/sellers/{seller_id}/orders"

    params = {"orderNumber": order_number, "size": 5, "page": 0}

    try:
        resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=20)
        if not resp.ok:
            print(f"[SIPARIS EŞLEŞTİRME HATASI] {resp.status_code}: {resp.text}")
            return None
        content = resp.json().get("content", [])
        return content[0] if content else None
    except Exception as e:
        print(f"[SIPARIS EŞLEŞTİRME HATASI] {e}")
        return None


# ---------------------------------------------------------------------------
# Mesaj biçimlendirme
# ---------------------------------------------------------------------------

def clean_product_name(name, max_len=45):
    """Ürün adını kısaltır (çok uzun ürün başlıklarını sadeleştirmek için)."""
    name = (name or "Ürün").strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip() + "…"
    return name


def format_product_line(line):
    """Tek bir ürün satırını, boy/beden bilgisi KALIN ve her zaman görünür
    şekilde, kısa ve tutarlı bir formatta döner."""
    name = html_escape(clean_product_name(line.get("productName", "Ürün")))
    size = html_escape((line.get("productSize") or line.get("productColor") or "").strip())
    qty = line.get("quantity", 1)
    barcode = line.get("barcode", "")

    size_suffix = f" <b>({size})</b>" if size else ""

    result = f"▫️ {name}{size_suffix} — <b>{qty} adet</b>"
    if barcode:
        result += f"\n    <code>{barcode}</code>"
    return result


def format_order_message(pkg):
    order_number = pkg.get("orderNumber", "—")
    lines = pkg.get("lines", [])
    total = pkg.get("totalPrice", "—")
    customer = f"{pkg.get('customerFirstName','')} {pkg.get('customerLastName','')}".strip()
    cargo_tracking_number = pkg.get("cargoTrackingNumber", "")
    cargo_provider = pkg.get("cargoProviderName", "")

    text = "🆕 <b>YENİ SİPARİŞ</b>\n"
    text += f"№ <b>{order_number}</b>"
    if customer:
        text += f" · {customer}"
    text += f" · {total} TL\n"

    if cargo_tracking_number:
        text += f"🚚 {cargo_tracking_number}"
        if cargo_provider:
            text += f" · {cargo_provider}"
        text += "\n"
    else:
        text += "🚚 Kargo takip no henüz atanmamış\n"

    if pkg.get("giftBoxRequested"):
        text += "🎁 Hediye paketi istendi\n"

    text += "\n" + "\n".join(format_product_line(line) for line in lines)
    return text


def format_claim_message(claim):
    """İade / ürün değişikliği talebi bildirimi."""
    claim_id = claim.get("id", "—")
    order_number = claim.get("orderNumber", "—")
    items = claim.get("items", [])

    text = "🔁 <b>ÜRÜN DEĞİŞİKLİĞİ / İADE TALEBİ</b>\n"
    text += f"№ <b>{order_number}</b> · Talep {claim_id}\n\n"
    for item in items:
        reason = item.get("claimItems", [{}])[0].get("reason", {}).get("name", "Belirtilmemiş")
        product = clean_product_name(item.get("orderLine", {}).get("productName", "Ürün"))
        text += f"▫️ {product}\n    Sebep: {reason}\n"
    return text


def extract_order_number(text):
    """Soru metni içinde geçen, sipariş numarası olabilecek 6+ haneli rakam dizisini bulur.
    Örn: 'siparişim 123456789, d yerine a olsun' -> '123456789'
    Birden fazla rakam grubu varsa en uzun olanı (en olası sipariş no) seçer."""
    import re
    candidates = re.findall(r"\d{6,}", text or "")
    if not candidates:
        return None
    return max(candidates, key=len)


def to_ms(value):
    """Tarih değerini (milisaniye sayısı, sayı gibi yazı ya da ISO tarih yazısı)
    milisaniyeye çevirir. Çevrilemezse None döner."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TURKEY_TZ)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def question_matches_order_by_customer(q, order):
    """Soruyu soran müşteri, bu siparişi veren müşteriyle aynı kişi mi VE soru
    siparişten SONRA mı yazılmış? (Sipariş vermeden önce sorduğu eski ürün
    soruları eşleşmesin diye zaman sırası da kontrol edilir.)"""
    q_customer, o_customer = q.get("customerId"), order.get("customerId")
    if q_customer is None or o_customer is None or str(q_customer) != str(o_customer):
        return False
    q_time, o_time = to_ms(q.get("creationDate")), to_ms(order.get("orderDate"))
    if q_time is None or o_time is None:
        return False
    return q_time >= o_time - 60_000  # 1 dakikalık tolerans


def format_question_message(q, cfg=None, pending_orders=None):
    """Soru bildirimini hazırlar. Dönüş: (metin, görsel_url, siparişle_eşleşti_mi)
    Eşleşme iki yoldan aranır:
      1) Soru metninde 6+ haneli sipariş numarası geçiyorsa
      2) Numara yoksa: soruyu soran müşteri, kargolanmamış bir siparişin sahibiyle
         aynı kişiyse ve soru siparişten sonra yazılmışsa"""
    product = clean_product_name(q.get("productName", "Ürün"))
    question_text = q.get("text", "")

    order_number = extract_order_number(question_text)
    order = find_order_by_number(cfg, order_number) if (order_number and cfg is not None) else None

    if not order and pending_orders:
        candidates = [o for o in pending_orders if question_matches_order_by_customer(q, o)]
        if candidates:
            order = max(candidates, key=lambda o: o.get("orderDate") or 0)  # en yeni sipariş
            order_number = str(order.get("orderNumber", ""))

    if not order_number:
        # Siparişle bağlantısı olmayan SADE soru: kısa ve öz bir bildirim yeter.
        text = "💬 <b>Yeni mesaj var</b> — cevapla\n"
        text += f"Ürün: {product}"
        return text, q.get("imageUrl"), False

    # Siparişle ilgili istek (hediye kutusu, kişiselleştirme vb.):
    # detaylı göster, ilgili siparişin ürünüyle birleştir.
    text = "🎁 <b>ÖZELLEŞTİRME / SİPARİŞLE İLGİLİ MESAJ</b>\n"
    text += f"Ürün: <b>{product}</b>\n"
    text += f"💬 {html_escape(question_text)}\n"

    if order:
        text += f"\n📦 <b>Sipariş — № {order_number}</b>\n"
        customer = f"{order.get('customerFirstName','')} {order.get('customerLastName','')}".strip()
        if customer:
            text += f"{html_escape(customer)}"
        cargo_tracking_number = order.get("cargoTrackingNumber", "")
        cargo_provider = order.get("cargoProviderName", "")
        if cargo_tracking_number:
            text += f" · 🚚 {cargo_tracking_number}"
            if cargo_provider:
                text += f" ({cargo_provider})"
        text += "\n"
        text += "\n".join(format_product_line(line) for line in order.get("lines", []))
    else:
        text += f"\n⚠️ {order_number} numaralı sipariş bulunamadı, elle kontrol etmen gerekebilir.\n"

    return text, q.get("imageUrl"), bool(order)


def collect_order_notes(cfg, orders):
    """Müşteri sorularını tarar ve her siparişe ait 'notları' toplar. Bir soru
    bir siparişin notu sayılır, eğer:
      1) sorunun içinde o siparişin numarası geçiyorsa, YA DA
      2) soruyu soran müşteri o siparişin sahibiyse ve soru siparişten sonra
         yazılmışsa (müşteri numara yazmasa bile).
    Dönüş: {sipariş_no: [not_metni, ...]}"""
    import re
    notes = {}
    if not orders:
        return notes

    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/qna/sellers/{seller_id}/questions/filter"
    params = {"page": 0, "size": 100, "orderByField": "CreatedDate", "orderByDirection": "DESC"}
    by_number = by_customer = 0
    try:
        resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=30)
        if not resp.ok:
            print(f"[NOT TOPLAMA HATASI] {resp.status_code}: {resp.text}")
            return notes
        order_numbers = {str(o.get("orderNumber", "")): o for o in orders}
        for q in resp.json().get("content", []):
            text = (q.get("text") or "").strip()
            if not text:
                continue
            attached_to = set()
            for number in re.findall(r"\d{6,}", text):
                if number in order_numbers:
                    attached_to.add(number)
                    by_number += 1
            if not attached_to:
                for number, order in order_numbers.items():
                    if question_matches_order_by_customer(q, order):
                        attached_to.add(number)
                        by_customer += 1
            for number in attached_to:
                notes.setdefault(number, [])
                if text not in notes[number]:
                    notes[number].append(text)
        print(f"[BİLGİ] Notlar: {by_number} tanesi sipariş numarasıyla, "
              f"{by_customer} tanesi müşteri eşleşmesiyle siparişlere eklendi.")
    except Exception as e:
        print(f"[NOT TOPLAMA HATASI] {e}")
    return notes


def hours_since_order(order, now):
    """Siparişin verilmesinden bu yana geçen saat (yaklaşık). Bilinmiyorsa None."""
    order_date = order.get("orderDate")
    if not order_date:
        return None
    try:
        ordered_at = datetime.fromtimestamp(order_date / 1000, tz=TURKEY_TZ)
        return int((now - ordered_at).total_seconds() // 3600)
    except Exception:
        return None


def format_unshipped_summary(orders, notes_by_order, now, max_len=3500):
    """Kargolanmamış TÜM siparişleri, tek bir toplu mesaj olarak (çok uzarsa
    birkaç parça halinde) hazırlar. En eski sipariş en üstte (en acil olan).
    Her siparişte: alıcı adı, sipariş no, hediye kutusu, geçen süre, kargo takip
    no, ürünler ve varsa müşterinin özelleştirme notu görünür.
    Dönüş: mesaj parçalarının listesi."""
    orders = sorted(orders, key=lambda o: o.get("orderDate") or 0)

    header = f"⏰ <b>{len(orders)} SİPARİŞ KARGO BEKLİYOR</b> ({now.strftime('%H:%M')})\n"

    blocks = []
    for i, order in enumerate(orders, start=1):
        order_number = str(order.get("orderNumber", "—"))
        customer = f"{order.get('customerFirstName','')} {order.get('customerLastName','')}".strip()

        title = f"<b>{i}.</b> {html_escape(customer) or 'Alıcı bilinmiyor'} · <b>№ {order_number}</b>"
        if order.get("giftBoxRequested"):
            title += " 🎁"
        hours = hours_since_order(order, now)
        if hours is not None:
            title += f" · {'🔴' if hours >= 20 else '⏳'} {hours} sa"

        lines = [title]

        tracking = order.get("cargoTrackingNumber", "")
        provider = order.get("cargoProviderName", "")
        if tracking:
            lines.append(f"🚚 {tracking}" + (f" · {provider}" if provider else ""))

        for line in order.get("lines", []):
            lines.append(format_product_line(line))

        for note in notes_by_order.get(order_number, []):
            short_note = note if len(note) <= 200 else note[:200].rstrip() + "…"
            lines.append(f"📝 <i>{html_escape(short_note)}</i>")

        blocks.append("\n".join(lines))

    # Mesajı Telegram'ın uzunluk sınırını aşmayacak şekilde parçalara böl
    messages = []
    current = header
    for block in blocks:
        candidate = current + "\n" + block + "\n"
        if len(candidate) > max_len and current != header:
            messages.append(current)
            current = "⏰ <b>(devamı)</b>\n\n" + block + "\n"
        else:
            current = candidate
    messages.append(current)
    return messages


def fetch_unshipped_orders(cfg):
    """Henüz kargoya verilmemiş (Created/Picking/Invoiced durumundaki)
    siparişleri çeker. SON 7 GÜN içindeki tüm siparişlere bakar — böylece
    dünden (ya da önceki günlerden) kalan, hâlâ kargolanmamış siparişler de
    gece yarısı geçince kaybolmaz."""
    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/order/sellers/{seller_id}/orders"

    start_ts = int((now_tr() - timedelta(days=7)).timestamp() * 1000)
    end_ts = int(now_tr().timestamp() * 1000)

    all_orders = {}
    for status in UNSHIPPED_STATUSES:
        params = {
            "status": status,
            "startDate": start_ts,
            "endDate": end_ts,
            "size": 200,
            "page": 0,
        }
        try:
            resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=30)
            if not resp.ok:
                print(f"[KARGOLANMAMIŞ SİPARİŞ ÇEKME HATASI - {status}] {resp.status_code}: {resp.text}")
                continue
            for pkg in resp.json().get("content", []):
                all_orders[pkg.get("id")] = pkg
        except Exception as e:
            print(f"[KARGOLANMAMIŞ SİPARİŞ ÇEKME HATASI - {status}] {e}")

    return list(all_orders.values())


def get_reminder_mode(now):
    """Şu anki güne/saate göre ne yapılacağını belirler.
    Pazar günleri: hiç hatırlatma yok.
    10:30-14:00 arası: güne özel TEK SEFERLİK hatırlatma (mode='once') —
      bu aralıkta script İLK ne zaman çalışırsa o an gönderir (GitHub'ın
      zamanlayıcısı birkaç dakika gecikebildiği için dar bir pencereye
      bağlı kalınmıyor, tüm aralık boyunca 'bugün gönderildi mi' kontrol edilir).
    14:00-15:00 arası: 20 dakikada bir (mode='interval', 20).
    15:00-16:30 arası: 5 dakikada bir (mode='interval', 5).
    Diğer tüm saatler: hiç hatırlatma yok.
    Dönüş: (mode, interval_dakika) — mode 'none' | 'once' | 'interval'"""
    if now.weekday() == 6:  # Python'da Pazartesi=0 ... Pazar=6
        return ("none", None)

    t = now.time()
    t_1030 = datetime.strptime("10:30", "%H:%M").time()
    t_14 = datetime.strptime("14:00", "%H:%M").time()
    t_15 = datetime.strptime("15:00", "%H:%M").time()
    t_1630 = datetime.strptime("16:30", "%H:%M").time()

    if t_1030 <= t < t_14:
        return ("once", None)
    if t_14 <= t < t_15:
        return ("interval", 20)
    if t_15 <= t < t_1630:
        return ("interval", 5)
    return ("none", None)


def get_telegram_updates(cfg, state):
    """Telegram'daki YENİ mesajları/buton tıklamalarını çeker (grup dahil).
    'Kargoya Verdim' butonuna basılmasını ya da 'kargo çıktı <sipariş no>'
    yazılmasını yakalamak için kullanılır."""
    bot_token = cfg["telegram"]["bot_token"]
    offset = state.get("telegram_update_offset", 0)
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"offset": offset, "timeout": 0}

    try:
        resp = requests.get(url, params=params, timeout=20)
        if not resp.ok:
            print(f"[TELEGRAM GÜNCELLEME OKUMA HATASI] {resp.status_code}: {resp.text}")
            return []
        updates = resp.json().get("result", [])
        if updates:
            state["telegram_update_offset"] = updates[-1]["update_id"] + 1
        return updates
    except Exception as e:
        print(f"[TELEGRAM GÜNCELLEME OKUMA HATASI] {e}")
        return []


def process_shipped_confirmations(cfg, state, updates):
    """Grup içinde 'Kargoya Verdim' butonuna basılan ya da
    'kargo çıktı <sipariş no>' yazılan mesajları bulup,
    o sipariş için hatırlatmaları durdurur."""
    confirmed = set(str(x) for x in state.get("shipped_confirmed_orders", []))
    group_chat_id = str(cfg["telegram"]["chat_id"])

    for u in updates:
        # 1) Butona basma (callback_query) kontrolü
        cq = u.get("callback_query")
        if cq:
            data = cq.get("data", "") or ""

            if data == "noop":
                # Zaten '✅ Kargoya Verildi' olmuş butona tekrar basılmış, yapacak bir şey yok
                answer_callback_query(cfg, cq.get("id"), "Bu sipariş zaten kargoya verildi olarak işaretli ✅")
                continue

            cq_chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            message_id = cq.get("message", {}).get("message_id")

            if cq_chat_id == group_chat_id and data.startswith("shipped:"):
                order_number = data.split("shipped:", 1)[1].strip()
                if order_number:
                    confirmed.add(order_number)
                    who = cq.get("from", {}).get("first_name", "")
                    answer_callback_query(
                        cfg, cq.get("id"),
                        f"✅ {order_number} kargoya verildi olarak işaretlendi"
                    )
                    # Butonun görünümünü '✅ Kargoya Verildi' olarak değiştir
                    if message_id:
                        edit_message_reply_markup(
                            cfg, cq_chat_id, message_id, build_shipped_confirmed_button()
                        )
                    print(f"[BİLGİ] '{order_number}' numaralı sipariş, {who} tarafından "
                          f"'Kargoya Verdim' butonuyla onaylandı, hatırlatmalar durduruldu.")
            continue  # callback_query'nin ayrıca 'message' metni de yok, devam etmeye gerek yok

        # 2) Eski yöntem: elle 'kargo çıktı <sipariş no>' yazma (yedek olarak duruyor)
        msg = u.get("message", {})
        text = (msg.get("text") or "")
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id != group_chat_id:
            continue

        lower = text.lower()
        if "kargo" in lower and ("çıktı" in lower or "cikti" in lower or "çikti" in lower):
            order_number = extract_order_number(text)
            if order_number:
                confirmed.add(order_number)
                print(f"[BİLGİ] '{order_number}' numaralı sipariş için kargo onayı alındı, "
                      f"hatırlatmalar durduruldu.")

    state["shipped_confirmed_orders"] = list(confirmed)[-2000:]


def send_unshipped_reminders(cfg, state):
    """Kargoya verilmemiş siparişler için, saate göre gerekiyorsa TEK BİR TOPLU
    hatırlatma mesajı gönderir (her sipariş için ayrı mesaj atmaz).
    - 'once' modunda (10:30-14:00): o gün henüz hatırlatılmamış siparişler
      için, günde bir kere.
    - 'interval' modunda (14:00-15:00 ve 15:00-16:30): belirtilen dakika
      aralığında, TÜM bekleyen siparişlerin güncel listesi.
    ÖNEMLİ: Telegram'daki buton/klavye tıklamaları saat/mod ne olursa olsun
    HER ÇALIŞTIRMADA kontrol edilir (kasıtlı olarak saat kontrolünden ÖNCE)."""
    # Önce Telegram grubunda yeni "Kargo Çıktı" onayı var mı diye bak
    updates = get_telegram_updates(cfg, state)
    process_shipped_confirmations(cfg, state, updates)

    now = now_tr()
    confirmed = set(str(x) for x in state.get("shipped_confirmed_orders", []))

    # Bekleyen (kargolanmamış ve henüz onaylanmamış) siparişler
    unshipped = fetch_unshipped_orders(cfg)
    pending_orders = [
        o for o in unshipped
        if str(o.get("orderNumber", "")) and str(o.get("orderNumber", "")) not in confirmed
    ]

    # Özel klavyeyi HER ZAMAN güncel tut (saat/mod farketmeksizin)
    sync_reply_keyboard(cfg, state, [
        (
            str(o.get("orderNumber", "")),
            f"{o.get('customerFirstName','')} {o.get('customerLastName','')}".strip(),
        )
        for o in pending_orders
    ])

    mode, interval = get_reminder_mode(now)
    if mode == "none" or not pending_orders:
        return 0

    orders_to_send = []

    if mode == "once":
        today_str = now.strftime("%Y-%m-%d")
        already_sent_today = set(state.get("morning_reminder_sent", {}).get(today_str, []))
        orders_to_send = [
            o for o in pending_orders
            if str(o.get("orderNumber", "")) not in already_sent_today
        ]
        if not orders_to_send:
            return 0
        for o in orders_to_send:
            already_sent_today.add(str(o.get("orderNumber", "")))
        # Sadece bugünün kaydını tutuyoruz, eski günleri temizliyoruz
        state["morning_reminder_sent"] = {today_str: list(already_sent_today)}

    else:  # mode == "interval"
        last_str = state.get("last_interval_reminder")
        due = True
        if last_str:
            try:
                last_dt = datetime.fromisoformat(last_str)
                due = (now - last_dt) >= timedelta(minutes=interval)
            except Exception:
                due = True
        if not due:
            return 0
        orders_to_send = pending_orders
        state["last_interval_reminder"] = now.isoformat()

    notes_by_order = collect_order_notes(cfg, orders_to_send)
    for message in format_unshipped_summary(orders_to_send, notes_by_order, now):
        send_telegram_message(cfg, message)
        time.sleep(1)

    return len(orders_to_send)


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------

def load_config():
    """Config bilgilerini yükler.
    Önce ortam değişkenlerine (GitHub Actions Secrets gibi) bakar,
    hepsi doluysa onları kullanır. Yoksa yerel config.json dosyasına döner
    (Windows'ta elle test ederken kullanılan yöntem)."""
    env_seller = os.environ.get("TRENDYOL_SELLER_ID")
    env_key = os.environ.get("TRENDYOL_API_KEY")
    env_secret = os.environ.get("TRENDYOL_API_SECRET")
    env_bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    env_chat = os.environ.get("TELEGRAM_CHAT_ID")
    # İkinci grup: iade talepleri + sipariş numarası içermeyen basit sorular.
    # Verilmezse (isteğe bağlı), her şey ana gruba gider (eski davranış).
    env_chat_secondary = os.environ.get("TELEGRAM_IADE_SORU_CHAT_ID")

    print("[HATA AYIKLAMA] Ortam değişkenleri durumu (gerçek değerler gösterilmiyor):")
    print(f"  TRENDYOL_SELLER_ID  dolu mu: {bool(env_seller)}")
    print(f"  TRENDYOL_API_KEY    dolu mu: {bool(env_key)}")
    print(f"  TRENDYOL_API_SECRET dolu mu: {bool(env_secret)}")
    print(f"  TELEGRAM_BOT_TOKEN  dolu mu: {bool(env_bot)}")
    print(f"  TELEGRAM_CHAT_ID    dolu mu: {bool(env_chat)}")
    print(f"  TELEGRAM_IADE_SORU_CHAT_ID dolu mu: {bool(env_chat_secondary)} (opsiyonel)")

    if env_seller and env_key and env_secret and env_bot and env_chat:
        return {
            "trendyol": {
                "seller_id": env_seller,
                "api_key": env_key,
                "api_secret": env_secret,
            },
            "telegram": {
                "bot_token": env_bot,
                "chat_id": env_chat,
                "iade_soru_chat_id": env_chat_secondary or env_chat,
            },
        }

    if not os.path.exists(CONFIG_PATH):
        print("HATA: Ne ortam değişkenleri (GitHub Secrets) ne de config.json bulundu.")
        sys.exit(1)

    cfg = load_json(CONFIG_PATH, {})
    # Yerel config.json'da ikinci grup tanımlı değilse, ana grubu kullan
    if cfg.get("telegram") and not cfg["telegram"].get("iade_soru_chat_id"):
        cfg["telegram"]["iade_soru_chat_id"] = cfg["telegram"].get("chat_id")
    return cfg


def debug_print_order_field_names(cfg, state):
    """Bir kereliğine, en güncel siparişin içindeki ALAN İSİMLERİNİ (değerleri değil,
    sadece 'seller_id', 'orderNumber' gibi anahtar isimlerini) log'a yazar.
    Bu sayede 'sipariş notu' bilgisinin hangi alanda geldiğini, hiçbir kişisel
    bilgiyi (isim, adres vb.) paylaşmadan bulabiliriz. Bir kez çalışıp bir daha
    tekrar etmez."""
    if state.get("debug_fields_printed"):
        return

    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/order/sellers/{seller_id}/orders"
    params = {"size": 1, "page": 0, "orderByField": "PackageLastModifiedDate",
              "orderByDirection": "DESC"}
    try:
        resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=30)
        if resp.ok:
            content = resp.json().get("content", [])
            if content:
                order = content[0]
                print("[HATA AYIKLAMA - ALAN İSİMLERİ] Sipariş üst seviye alanları:")
                print("  " + ", ".join(sorted(order.keys())))
                lines = order.get("lines", [])
                if lines:
                    print("[HATA AYIKLAMA - ALAN İSİMLERİ] Ürün satırı (lines) alanları:")
                    print("  " + ", ".join(sorted(lines[0].keys())))
    except Exception as e:
        print(f"[HATA AYIKLAMA HATASI] {e}")

    state["debug_fields_printed"] = True


def debug_print_all_questions(cfg, state):
    """Bir kereliğine, TÜM soru/notları (statü filtresi olmadan) çeker ve
    her birinin sadece STATÜSÜNÜ ve ALAN İSİMLERİNİ log'a yazar
    (soru metni, müşteri bilgisi gibi kişisel içerik YAZILMAZ).
    'Sipariş notu' hangi statüde geliyor, bunu bulmak için kullanılır."""
    if state.get("debug_questions_printed"):
        return

    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/qna/sellers/{seller_id}/questions/filter"
    params = {"page": 0, "size": 50, "orderByField": "CreatedDate", "orderByDirection": "DESC"}

    try:
        resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=30)
        if resp.ok:
            content = resp.json().get("content", [])
            print(f"[HATA AYIKLAMA - SORULAR] Statü filtresi olmadan toplam {len(content)} kayıt bulundu.")
            if content:
                statuses = [str(q.get("status")) for q in content]
                print("[HATA AYIKLAMA - SORULAR] Bulunan statüler: " + ", ".join(sorted(set(statuses))))
                print("[HATA AYIKLAMA - SORULAR] İlk kaydın alan isimleri: "
                      + ", ".join(sorted(content[0].keys())))
        else:
            print(f"[HATA AYIKLAMA - SORULAR HATASI] {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[HATA AYIKLAMA - SORULAR HATASI] {e}")

    state["debug_questions_printed"] = True


def migrate_seed_existing_questions(cfg, state):
    """Statü filtresini kaldırdığımız için, geçmişte zaten var olan
    (özellikle 'ANSWERED' durumundaki) soruların hepsi birden 'yeni' sayılıp
    Telegram'a art arda düşmesin diye, bir kereliğine mevcut soruları
    bildirim GÖNDERMEDEN 'görülmüş' olarak işaretler."""
    if state.get("questions_migration_done"):
        return

    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/qna/sellers/{seller_id}/questions/filter"
    params = {"page": 0, "size": 200, "orderByField": "CreatedDate", "orderByDirection": "DESC"}

    try:
        resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=30)
        if resp.ok:
            content = resp.json().get("content", [])
            seen_ids = set(state.get("seen_question_ids", []))
            for q in content:
                seen_ids.add(str(q.get("id")))
            state["seen_question_ids"] = list(seen_ids)[-2000:]
            print(f"[BİLGİ] Geçiş tamamlandı: {len(content)} mevcut soru/not, "
                  f"bildirim gönderilmeden 'görülmüş' olarak işaretlendi.")
    except Exception as e:
        print(f"[GEÇİŞ HATASI] {e}")

    state["questions_migration_done"] = True


def fix_missed_pending_questions(cfg, state):
    """Bir kerelik düzeltme: geçiş (migration) sırasında yanlışlıkla
    'görülmüş' işaretlenmiş olabilecek, ama HÂLÂ CEVAPLANMAMIŞ durumdaki
    soruları tekrar 'görülmemiş' yapar, böylece bir sonraki taramada
    yakalanıp Telegram'a bildirilirler."""
    if state.get("pending_questions_fix_done"):
        return

    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/qna/sellers/{seller_id}/questions/filter"
    params = {"page": 0, "size": 200, "orderByField": "CreatedDate", "orderByDirection": "DESC"}

    try:
        resp = requests.get(url, headers=trendyol_auth_header(cfg), params=params, timeout=30)
        if resp.ok:
            content = resp.json().get("content", [])
            seen_ids = set(state.get("seen_question_ids", []))
            freed_count = 0
            for q in content:
                status = str(q.get("status", "")).upper()
                qid = str(q.get("id"))
                if status != "ANSWERED" and qid in seen_ids:
                    seen_ids.discard(qid)
                    freed_count += 1
            state["seen_question_ids"] = list(seen_ids)
            print(f"[BİLGİ] Düzeltme tamamlandı: {freed_count} adet hâlâ cevaplanmamış soru "
                  f"tekrar 'yeni' olarak işaretlendi, bir sonraki adımda bildirilecek.")
    except Exception as e:
        print(f"[DÜZELTME HATASI] {e}")

    state["pending_questions_fix_done"] = True


def main():
    cfg = load_config()
    state = load_json(STATE_PATH, {})

    try:
        debug_print_order_field_names(cfg, state)
        debug_print_all_questions(cfg, state)
        migrate_seed_existing_questions(cfg, state)
        fix_missed_pending_questions(cfg, state)

        new_orders = fetch_new_orders(cfg, state)
        for pkg in new_orders:
            send_telegram_message(cfg, format_order_message(pkg))
            time.sleep(1)  # Telegram rate limit'e takılmamak için

        new_claims = fetch_new_claims(cfg, state)
        for claim in new_claims:
            send_telegram_message(cfg, format_claim_message(claim),
                                   chat_id=cfg["telegram"]["iade_soru_chat_id"])
            time.sleep(1)

        new_questions = fetch_new_questions(cfg, state)
        pending_for_questions = []
        if new_questions:
            confirmed_now = set(str(x) for x in state.get("shipped_confirmed_orders", []))
            pending_for_questions = [
                o for o in fetch_unshipped_orders(cfg)
                if str(o.get("orderNumber", "")) not in confirmed_now
            ]
        for q in new_questions:
            text, image_url, matched_order = format_question_message(q, cfg, pending_for_questions)
            # Sipariş numarasıyla eşleşen (özelleştirme vb.) sorular -> ana kargo grubu
            # Basit/genel sorular -> ikinci grup (iade/soru)
            target_chat_id = cfg["telegram"]["chat_id"] if matched_order else cfg["telegram"]["iade_soru_chat_id"]
            send_telegram_message(cfg, text, photo_url=image_url, chat_id=target_chat_id)
            time.sleep(1)

        unshipped_reminder_count = send_unshipped_reminders(cfg, state)

        print(f"[{now_tr()}] Kontrol tamamlandı. "
              f"{len(new_orders)} yeni sipariş, {len(new_claims)} yeni talep, "
              f"{len(new_questions)} yeni soru, "
              f"{unshipped_reminder_count} kargolanmamış sipariş hatırlatması bulundu.")

    finally:
        save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
