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
import sys
import base64
import time
import requests
from datetime import datetime, timedelta

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


def send_telegram_message(cfg, text, photo_url=None, reply_markup=None):
    """Telegram'a metin (ve varsa fotoğraf, varsa buton) gönderir."""
    bot_token = cfg["telegram"]["bot_token"]
    chat_id = cfg["telegram"]["chat_id"]

    extra_data = {}
    if reply_markup:
        extra_data["reply_markup"] = json.dumps(reply_markup)

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
                      "disable_web_page_preview": False, **extra_data},
                timeout=20,
            )
        if not resp.ok:
            print(f"[TELEGRAM HATA] {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[TELEGRAM GONDERIM HATASI] {e}")
        # Görselli gönderim başarısız olduysa düz metinle tekrar dene
        if photo_url:
            send_telegram_message(cfg, text, photo_url=None)


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


# ---------------------------------------------------------------------------
# Trendyol veri çekme fonksiyonları
# ---------------------------------------------------------------------------

def fetch_new_orders(cfg, state):
    """Son 1 saat içindeki siparişleri çeker, daha önce bildirilmemiş olanları döner."""
    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/order/sellers/{seller_id}/orders"

    start_date = int((datetime.now() - timedelta(hours=1)).timestamp() * 1000)
    end_date = int(datetime.now().timestamp() * 1000)

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
    """Müşteri sorularını (cevap bekleyenleri) çeker."""
    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/qna/sellers/{seller_id}/questions/filter"

    params = {
        "status": "WAITING_FOR_ANSWER",
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

def format_order_message(pkg):
    order_number = pkg.get("orderNumber", "—")
    lines = pkg.get("lines", [])
    total = pkg.get("totalPrice", "—")
    customer = f"{pkg.get('customerFirstName','')} {pkg.get('customerLastName','')}".strip()
    cargo_tracking_number = pkg.get("cargoTrackingNumber", "")
    cargo_provider = pkg.get("cargoProviderName", "")
    cargo_tracking_link = pkg.get("cargoTrackingLink", "")

    text = f"🆕 <b>YENİ SİPARİŞ</b>\n"
    text += f"Sipariş No: <b>{order_number}</b>\n"
    if customer:
        text += f"Müşteri: {customer}\n"
    text += f"Tutar: {total} TL\n"
    if cargo_tracking_number:
        text += f"Kargo Takip No: <b>{cargo_tracking_number}</b>"
        if cargo_provider:
            text += f" ({cargo_provider})"
        text += "\n"
        if cargo_tracking_link:
            text += f"Takip Linki: {cargo_tracking_link}\n"
    else:
        text += "Kargo Takip No: henüz atanmamış\n"
    text += "\n<b>Ürünler:</b>\n"
    for line in lines:
        name = line.get("productName", "Ürün")
        size = line.get("productSize") or line.get("productColor") or ""
        qty = line.get("quantity", 1)
        barcode = line.get("barcode", "")
        text += f"• {name} {('(' + size + ')') if size else ''} — <b>{qty} adet</b>\n"
        if barcode:
            text += f"   Barkod: {barcode}\n"
    return text


def format_claim_message(claim):
    """İade / ürün değişikliği talebi bildirimi."""
    claim_id = claim.get("id", "—")
    order_number = claim.get("orderNumber", "—")
    items = claim.get("items", [])

    text = f"🔁 <b>ÜRÜN DEĞİŞİKLİĞİ / İADE TALEBİ</b>\n"
    text += f"Talep No: {claim_id}\n"
    text += f"Sipariş No: {order_number}\n\n"
    for item in items:
        reason = item.get("claimItems", [{}])[0].get("reason", {}).get("name", "Belirtilmemiş")
        product = item.get("orderLine", {}).get("productName", "Ürün")
        text += f"• {product}\n  Sebep: {reason}\n"
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


def format_question_message(q, cfg=None):
    product = q.get("productName", "Ürün")
    question_text = q.get("text", "")

    text = f"❓ <b>YENİ MÜŞTERİ SORUSU</b>\n"
    text += f"Ürün: <b>{product}</b>\n"
    text += f"Soru: {question_text}\n"

    # Soru metninde 6+ haneli bir sipariş numarası var mı diye bak.
    # Varsa, o siparişi Trendyol'dan otomatik çekip altına ekle.
    order_number = extract_order_number(question_text)
    if order_number and cfg is not None:
        order = find_order_by_number(cfg, order_number)
        if order:
            text += f"\n📦 <b>Eşleşen Sipariş Bulundu — No: {order_number}</b>\n"
            customer = f"{order.get('customerFirstName','')} {order.get('customerLastName','')}".strip()
            if customer:
                text += f"Müşteri: {customer}\n"
            cargo_tracking_number = order.get("cargoTrackingNumber", "")
            cargo_provider = order.get("cargoProviderName", "")
            if cargo_tracking_number:
                text += f"Kargo Takip No: <b>{cargo_tracking_number}</b>"
                if cargo_provider:
                    text += f" ({cargo_provider})"
                text += "\n"
            else:
                text += "Kargo Takip No: henüz atanmamış\n"
            for line in order.get("lines", []):
                name = line.get("productName", "Ürün")
                size = line.get("productSize") or line.get("productColor") or ""
                qty = line.get("quantity", 1)
                barcode = line.get("barcode", "")
                text += f"• {name} {('(' + size + ')') if size else ''} — <b>{qty} adet</b>\n"
                if barcode:
                    text += f"   Barkod: {barcode}\n"
        else:
            text += f"\n⚠️ Soruda {order_number} numaralı bir sipariş numarası geçiyor gibi görünüyor, "
            text += "ama sistemde bu numarayla eşleşen bir sipariş bulunamadı — elle kontrol etmen gerekebilir.\n"

    return text, q.get("imageUrl")


def format_unshipped_reminder(order, now):
    order_number = order.get("orderNumber", "—")
    lines = order.get("lines", [])
    cargo_tracking_number = order.get("cargoTrackingNumber", "")
    cargo_provider = order.get("cargoProviderName", "")
    cargo_tracking_link = order.get("cargoTrackingLink", "")

    text = f"⏰ <b>KARGOLANMAMIŞ SİPARİŞ UYARISI</b> ({now.strftime('%H:%M')})\n"
    text += f"Sipariş No: <b>{order_number}</b>\n"
    if cargo_tracking_number:
        text += f"Kargo Takip No: <b>{cargo_tracking_number}</b>"
        if cargo_provider:
            text += f" ({cargo_provider})"
        text += "\n"
        if cargo_tracking_link:
            text += f"Takip Linki: {cargo_tracking_link}\n"
    else:
        text += "Kargo Takip No: henüz atanmamış\n"
    text += "\n"
    for line in lines:
        name = line.get("productName", "Ürün")
        size = line.get("productSize") or line.get("productColor") or ""
        qty = line.get("quantity", 1)
        text += f"• {name} {('(' + size + ')') if size else ''} — <b>{qty} adet</b>\n"
    text += "\nKargoya verdiysen aşağıdaki butona basman yeterli 👇"
    return text


# ---------------------------------------------------------------------------
# Kargolanmamış sipariş takibi + Telegram'dan "kargo çıktı" onayını okuma
# ---------------------------------------------------------------------------

def fetch_unshipped_orders(cfg):
    """Bugün için henüz kargoya verilmemiş (Created/Picking/Invoiced durumundaki)
    siparişleri çeker."""
    seller_id = cfg["trendyol"]["seller_id"]
    url = f"{TRENDYOL_BASE}/order/sellers/{seller_id}/orders"

    start_of_day = datetime.combine(datetime.now().date(), datetime.min.time())
    start_ts = int(start_of_day.timestamp() * 1000)
    end_ts = int(datetime.now().timestamp() * 1000)

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


def current_reminder_interval_minutes(now):
    """Şu anki güne/saate göre hatırlatma sıklığını (dakika) döner.
    Pazar günleri hiç hatırlatma gönderilmez.
    Diğer günlerde sadece 08:00-16:30 arası hatırlatma gönderilir.
    15:00-16:00 arası 5 dk, geri kalan saatlerde (08:00-15:00 ve 16:00-16:30) 20 dk."""
    if now.weekday() == 6:  # Python'da Pazartesi=0 ... Pazar=6
        return None

    t = now.time()
    t_08 = datetime.strptime("08:00", "%H:%M").time()
    t_15 = datetime.strptime("15:00", "%H:%M").time()
    t_16 = datetime.strptime("16:00", "%H:%M").time()
    t_1630 = datetime.strptime("16:30", "%H:%M").time()

    if t < t_08 or t >= t_1630:
        return None
    if t_15 <= t < t_16:
        return 5
    return 20


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
            cq_chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            if cq_chat_id == group_chat_id and data.startswith("shipped:"):
                order_number = data.split("shipped:", 1)[1].strip()
                if order_number:
                    confirmed.add(order_number)
                    who = cq.get("from", {}).get("first_name", "")
                    answer_callback_query(
                        cfg, cq.get("id"),
                        f"✅ {order_number} kargoya verildi olarak işaretlendi"
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
    """Şu anki saate göre gerekiyorsa, kargoya verilmemiş siparişler için
    hatırlatma gönderir. Her sipariş için ayrı ayrı, en son ne zaman
    hatırlatıldığını takip eder."""
    now = datetime.now()
    interval = current_reminder_interval_minutes(now)
    if interval is None:
        return 0  # saat 08:00-16:30 aralığı dışında, hatırlatma yok

    # Önce Telegram grubunda yeni "Kargoya Verdim" onayı var mı diye bak
    updates = get_telegram_updates(cfg, state)
    process_shipped_confirmations(cfg, state, updates)

    confirmed = set(str(x) for x in state.get("shipped_confirmed_orders", []))
    last_reminders = state.get("last_unshipped_reminder", {})

    unshipped = fetch_unshipped_orders(cfg)
    sent_count = 0

    for order in unshipped:
        order_number = str(order.get("orderNumber", ""))
        if not order_number or order_number in confirmed:
            continue

        last_str = last_reminders.get(order_number)
        due = True
        if last_str:
            try:
                last_dt = datetime.fromisoformat(last_str)
                due = (now - last_dt) >= timedelta(minutes=interval)
            except Exception:
                due = True

        if due:
            send_telegram_message(
                cfg,
                format_unshipped_reminder(order, now),
                reply_markup=build_shipped_button(order_number),
            )
            last_reminders[order_number] = now.isoformat()
            sent_count += 1
            time.sleep(1)

    state["last_unshipped_reminder"] = last_reminders
    return sent_count


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

    print("[HATA AYIKLAMA] Ortam değişkenleri durumu (gerçek değerler gösterilmiyor):")
    print(f"  TRENDYOL_SELLER_ID  dolu mu: {bool(env_seller)}")
    print(f"  TRENDYOL_API_KEY    dolu mu: {bool(env_key)}")
    print(f"  TRENDYOL_API_SECRET dolu mu: {bool(env_secret)}")
    print(f"  TELEGRAM_BOT_TOKEN  dolu mu: {bool(env_bot)}")
    print(f"  TELEGRAM_CHAT_ID    dolu mu: {bool(env_chat)}")

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
            },
        }

    if not os.path.exists(CONFIG_PATH):
        print("HATA: Ne ortam değişkenleri (GitHub Secrets) ne de config.json bulundu.")
        sys.exit(1)

    return load_json(CONFIG_PATH, {})


def main():
    cfg = load_config()
    state = load_json(STATE_PATH, {})

    try:
        new_orders = fetch_new_orders(cfg, state)
        for pkg in new_orders:
            send_telegram_message(cfg, format_order_message(pkg))
            time.sleep(1)  # Telegram rate limit'e takılmamak için

        new_claims = fetch_new_claims(cfg, state)
        for claim in new_claims:
            send_telegram_message(cfg, format_claim_message(claim))
            time.sleep(1)

        new_questions = fetch_new_questions(cfg, state)
        for q in new_questions:
            text, image_url = format_question_message(q, cfg)
            send_telegram_message(cfg, text, photo_url=image_url)
            time.sleep(1)

        unshipped_reminder_count = send_unshipped_reminders(cfg, state)

        print(f"[{datetime.now()}] Kontrol tamamlandı. "
              f"{len(new_orders)} yeni sipariş, {len(new_claims)} yeni talep, "
              f"{len(new_questions)} yeni soru, "
              f"{unshipped_reminder_count} kargolanmamış sipariş hatırlatması bulundu.")

    finally:
        save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
