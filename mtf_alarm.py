#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🐺 WOLF MTF + FUNDING ALARM  (+ grup içi elle tarama komutu)
---------------------------------------------------------------
Wolf MTF Scanner'ın LONG/SHORT mantığını (Pearson TI, 4H+1H) +
funding yönü filtresini sunucu tarafında çalıştırır ve eşleşen
coinleri Telegram'a atar. Hiçbir yerde İŞLEM AÇMAZ — sadece haber.

İKİ ÇALIŞMA ŞEKLİ:
  1) OTOMATİK: her 4H kapanıştan 20 dk önce -> senin DM'ine (TELEGRAM_CHAT_ID)
  2) ELLE:     Wolf Signals Pro grubunda komutla -> sonuç GRUBA düşer
       /tara       -> hızlı tarama (TOP 150)
       /taratümü   -> tam tarama (~tüm perp, 10-15 dk)
     * Komut SADECE grupta (TELEGRAM_GROUP_ID) çalışır; DM/başka sohbet yok sayılır.
     * Grupta yazabilen zaten üyedir -> "sadece üyeler" otomatik sağlanır.
     * Cooldown + tek-çalışma kilidi ile spam engellenir.

Zamanlama (otomatik):
  UTC kapanışlar: 00/04/08/12/16/20 · Çalışma (UTC+3): 02:40/06:40/10:40/14:40/18:40/22:40

ENV (Railway -> Variables):
  TELEGRAM_BOT_TOKEN   (zorunlu)
  TELEGRAM_CHAT_ID     (zorunlu)   - otomatik alarmın gideceği DM
  TELEGRAM_GROUP_ID    (vars -5025422334) - komutların çalışacağı grup
  COOLDOWN_MIN         (vars 10)   - iki elle tarama arası min. dakika
  TI_LEN(12) UPPER_BAND(88) LOWER_BAND(12) FUNDING_THRESHOLD(0.01)
  UNIVERSE(0=tümü) FILTER_10PCT(true) RUN_NOW(false)
  FAST_UNIVERSE(150) - /tara hızlı taramada kaç coin
"""

import os
import math
import time
import html
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ─────────────────────────────────────────────
# AYARLAR
# ─────────────────────────────────────────────
BINANCE = "https://fapi.binance.com"

TI_LEN            = int(os.getenv("TI_LEN", "12"))
UPPER_BAND        = float(os.getenv("UPPER_BAND", "88"))
LOWER_BAND        = float(os.getenv("LOWER_BAND", "12"))
FUNDING_THRESHOLD = float(os.getenv("FUNDING_THRESHOLD", "0.01"))
UNIVERSE          = int(os.getenv("UNIVERSE", "0"))
FILTER_10PCT      = os.getenv("FILTER_10PCT", "false").lower() == "true"
RUN_NOW           = os.getenv("RUN_NOW", "false").lower() == "true"

FAST_UNIVERSE     = int(os.getenv("FAST_UNIVERSE", "150"))
COOLDOWN_MIN      = int(os.getenv("COOLDOWN_MIN", "10"))
POLL_TIMEOUT      = int(os.getenv("POLL_TIMEOUT", "50"))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_GROUP = os.getenv("TELEGRAM_GROUP_ID", "-1004439903866").strip()

EXCLUDE = {"BTCDOMUSDT", "DEFIUSDT", "BLUEBIRDUSDT", "BTCSTUSDT"}
KLINE_LIMIT = TI_LEN + 8
WORKERS = 8
TZ3 = timezone(timedelta(hours=3))

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "wolf-mtf-alarm/1.1"})
POLL_SESSION = requests.Session()  # komut dinleyici ayrı oturum
POLL_SESSION.headers.update({"User-Agent": "wolf-mtf-alarm-poll/1.1"})

SCAN_LOCK = threading.Lock()       # aynı anda tek tarama
_last_cmd_ts = 0.0                 # son elle tarama zamanı (cooldown)
_diag_sent = set()                 # yanlış-grup uyarısı bir kez

def group_matches(cid):
    """Süpergrup -100 öneki dahil ID eşleştirme (basic<->supergroup biçimi)."""
    if not TG_GROUP:
        return True
    cs = str(cid); g = str(TG_GROUP)
    cands = {g}
    if g.startswith("-100"):
        cands.add("-" + g[4:])        # -100X -> -X
    elif g.startswith("-"):
        cands.add("-100" + g[1:])     # -X -> -100X
    return cs in cands


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


# ─────────────────────────────────────────────
# TI / SİNYAL — MTF Scanner JS portu (birebir)
# ─────────────────────────────────────────────
def pearson_corr(y, length):
    if len(y) < length:
        return 0.0
    s = y[-length:]
    n = length
    mx = (n - 1) / 2.0
    my = sum(s) / n
    num = dx2 = dy2 = 0.0
    for i in range(n):
        xi = i - mx
        yi = s[i] - my
        num += xi * yi
        dx2 += xi * xi
        dy2 += yi * yi
    denom = math.sqrt(dx2 * dy2)
    return 0.0 if denom == 0 else num / denom


def calc_ti(closes, length):
    return (pearson_corr(closes, length) + 1.0) / 2.0 * 100.0


def f_dir(ti, upper, lower):
    if ti >= upper:
        return -1
    if ti <= lower:
        return 1
    return 0


# ─────────────────────────────────────────────
# BINANCE
# ─────────────────────────────────────────────
def get(path, params=None, retries=3):
    url = BINANCE + path
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=12)
            if r.status_code in (418, 429):
                wait = 30 * (attempt + 1)
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    wait = max(int(ra), 20)
                log(f"⛔ Binance {r.status_code} ({path}) — {wait}sn bekle")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.2 * (attempt + 1))
    return None


def fetch_valid_symbols():
    """exchangeInfo -> sadece AKTİF (TRADING) PERPETUAL USDT kontratları.
    Web MTF tarayıcısıyla birebir: delist/kapanan coini eler, yeni listeleneni dahil eder."""
    try:
        info = get("/fapi/v1/exchangeInfo")
        valid = {
            s["symbol"] for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        }
        return valid if valid else None
    except Exception as e:
        log(f"exchangeInfo hatası ({e}) — ham USDT listesine düşülüyor")
        return None


def fetch_universe(uni_limit=None):
    """24h ticker + exchangeInfo -> temiz, hacme göre sıralı USDT-perp evreni."""
    limit = UNIVERSE if uni_limit is None else uni_limit
    data = get("/fapi/v1/ticker/24hr")
    valid = fetch_valid_symbols()  # delist/yeni kontrolü (her taramada, hep taze)
    if valid is not None:
        coins = [d for d in data if d["symbol"] in valid and d["symbol"] not in EXCLUDE]
    else:
        coins = [d for d in data if d["symbol"].endswith("USDT") and d["symbol"] not in EXCLUDE]
    coins.sort(key=lambda d: float(d.get("quoteVolume", 0)), reverse=True)
    if FILTER_10PCT:
        coins = [d for d in coins if abs(float(d.get("priceChangePercent", 0))) < 10]
    if limit > 0:
        coins = coins[:limit]
    return {
        d["symbol"]: {
            "price": float(d["lastPrice"]),
            "change24h": float(d["priceChangePercent"]),
            "volume": float(d["quoteVolume"]),
        }
        for d in coins
    }


def fetch_funding_all():
    data = get("/fapi/v1/premiumIndex")
    out = {}
    for d in data:
        try:
            out[d["symbol"]] = float(d["lastFundingRate"]) * 100.0
        except (KeyError, TypeError, ValueError):
            pass
    return out


def fetch_closes(symbol, interval):
    data = get("/fapi/v1/klines",
               {"symbol": symbol, "interval": interval, "limit": KLINE_LIMIT})
    if not data:
        return None
    return [float(k[4]) for k in data]


# ─────────────────────────────────────────────
# COIN DEĞERLENDİR
# ─────────────────────────────────────────────
def evaluate(symbol, meta, funding):
    closes4h = fetch_closes(symbol, "4h")
    closes1h = fetch_closes(symbol, "1h")
    if not closes4h or not closes1h:
        return None
    if len(closes4h) < TI_LEN or len(closes1h) < TI_LEN:
        return None

    ti4h = calc_ti(closes4h, TI_LEN)
    ti1h = calc_ti(closes1h, TI_LEN)
    dir4h = f_dir(ti4h, UPPER_BAND, LOWER_BAND)
    dir1h = f_dir(ti1h, UPPER_BAND, LOWER_BAND)

    signal = "neutral"
    if dir4h == 1 and dir1h == 1:
        signal = "long"
    elif dir4h == -1 and dir1h == -1:
        signal = "short"
    if signal == "neutral":
        return None

    fr = funding.get(symbol)
    if fr is None:
        return None

    if signal == "long" and fr < -FUNDING_THRESHOLD:
        pass
    elif signal == "short" and fr > FUNDING_THRESHOLD:
        pass
    else:
        return None

    return {
        "symbol": symbol, "signal": signal,
        "ti4h": round(ti4h, 1), "ti1h": round(ti1h, 1),
        "funding": fr, "price": meta["price"],
        "change24h": meta["change24h"], "volume": meta["volume"],
    }


def fmt_price(p):
    if p >= 1000:
        return f"${p:,.1f}"
    if p >= 1:
        return f"${p:,.3f}"
    return f"${p:.6f}"


# ─────────────────────────────────────────────
# TARAMA (chat_id parametreli: DM veya grup)
# ─────────────────────────────────────────────
def run_scan(chat_id, uni_limit=None, manual=False, mode_label="", tag="sched"):
    with SCAN_LOCK:
        log(f"[{tag}] Tarama başlıyor…")
        try:
            universe = fetch_universe(uni_limit)
            funding = fetch_funding_all()
        except Exception as e:
            log(f"Veri çekme hatası: {e}")
            send_telegram(f"🐺 WOLF MTF ALARM — veri hatası: {html.escape(str(e))}", chat_id)
            return

        log(f"[{tag}] {len(universe)} coin · {len(funding)} funding kaydı")

        hits = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(evaluate, s, m, funding): s for s, m in universe.items()}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception:
                    r = None
                if r:
                    hits.append(r)

        longs = sorted([h for h in hits if h["signal"] == "long"],
                       key=lambda x: x["volume"], reverse=True)
        shorts = sorted([h for h in hits if h["signal"] == "short"],
                        key=lambda x: x["volume"], reverse=True)

        log(f"[{tag}] Sonuç: LONG {len(longs)} · SHORT {len(shorts)}")
        send_results(len(universe), longs, shorts, chat_id, manual, mode_label)


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(text, chat_id=None):
    cid = chat_id or TG_CHAT
    if not TG_TOKEN or not cid:
        log("⚠ TELEGRAM_BOT_TOKEN / chat_id yok — mesaj atlanıyor")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for chunk in _split(text, 3900):
        try:
            resp = SESSION.post(url, json={
                "chat_id": cid,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=12)
            if resp.status_code != 200:
                log(f"Telegram hata {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log(f"Telegram gönderim hatası: {e}")
        time.sleep(0.4)


def _split(text, n):
    lines = text.split("\n")
    buf, out = "", []
    for ln in lines:
        if len(buf) + len(ln) + 1 > n:
            out.append(buf)
            buf = ln
        else:
            buf = ln if not buf else buf + "\n" + ln
    if buf:
        out.append(buf)
    return out


def _row(h):
    fr = h["funding"]
    return (f"• <b>{h['symbol'].replace('USDT','')}</b>  {fmt_price(h['price'])}  "
            f"4H:{h['ti4h']} 1H:{h['ti1h']}  fund:{fr:+.4f}%")


def send_results(scanned, longs, shorts, chat_id=None, manual=False, mode_label=""):
    now3 = datetime.now(TZ3).strftime("%d.%m %H:%M")
    if manual:
        line2 = f"🕐 Elle tarama · {mode_label} · {now3} (UTC+3)"
    else:
        line2 = f"🕐 4H kapanışa 20 dk · {now3} (UTC+3)"
    head = (f"🐺 <b>WOLF MTF + FUNDING ALARM</b>\n{line2}\n"
            f"Taranan: {scanned} · ▲ LONG: {len(longs)} · ▼ SHORT: {len(shorts)}")

    if not longs and not shorts:
        send_telegram(head + "\n\n— Şartı sağlayan coin yok.", chat_id)
        return

    parts = [head]
    if longs:
        parts.append("\n▲ <b>LONG</b> (yeşil funding)")
        parts += [_row(h) for h in longs]
    if shorts:
        parts.append("\n▼ <b>SHORT</b> (kırmızı funding)")
        parts += [_row(h) for h in shorts]
    send_telegram("\n".join(parts), chat_id)


# ─────────────────────────────────────────────
# KOMUT DİNLEYİCİ (getUpdates long-poll)
# ─────────────────────────────────────────────
def handle_command(text, chat_id):
    global _last_cmd_ts
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    if "@" in cmd:                      # /tara@BotAdi -> /tara
        cmd = cmd.split("@", 1)[0]
    arg = parts[1].lower() if len(parts) > 1 else ""

    if cmd not in ("/tara", "/taratümü", "/taratumu"):
        return
    full = (cmd in ("/taratümü", "/taratumu")
            or arg in ("tümü", "tumu", "full", "all", "hepsi"))

    now = time.time()
    remain = COOLDOWN_MIN * 60 - (now - _last_cmd_ts)
    if remain > 0:
        send_telegram(f"⏳ Son taramadan bu yana {COOLDOWN_MIN} dk geçmedi. "
                      f"{int(remain // 60)}dk {int(remain % 60)}sn sonra tekrar dene.", chat_id)
        return
    if SCAN_LOCK.locked():
        send_telegram("⏳ Tarama zaten sürüyor — bitince buraya düşecek.", chat_id)
        return

    _last_cmd_ts = now
    mode_label = "TÜMÜ" if full else "Hızlı (TOP %d)" % FAST_UNIVERSE
    uni = 0 if full else FAST_UNIVERSE
    send_telegram(f"🔍 <b>Tarama başladı</b> — {mode_label}. "
                  f"Bitince sonuçlar buraya düşecek (birkaç dk).", chat_id)
    threading.Thread(
        target=run_scan,
        kwargs=dict(chat_id=chat_id, uni_limit=uni, manual=True,
                    mode_label=mode_label, tag="cmd"),
        daemon=True,
    ).start()


def poll_commands():
    if not TG_TOKEN:
        log("⚠ Token yok — komut dinleyici kapalı.")
        return
    # webhook varsa getUpdates çalışmaz; temizle
    try:
        POLL_SESSION.get(f"https://api.telegram.org/bot{TG_TOKEN}/deleteWebhook",
                         params={"drop_pending_updates": "true"}, timeout=10)
    except Exception:
        pass
    log(f"👂 Komut dinleyici aktif — grup {TG_GROUP} · /tara, /taratümü")
    offset = None
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT, "allowed_updates": '["message"]'}
            if offset is not None:
                params["offset"] = offset
            r = POLL_SESSION.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                                 params=params, timeout=POLL_TIMEOUT + 15)
            data = r.json()
            if not data.get("ok"):
                if r.status_code == 409 or data.get("error_code") == 409:
                    log("⚠ getUpdates 409 ÇAKIŞMA — bu botu dinleyen başka bir process var! "
                        "Komut dinleme çalışmaz. Diğer dinleyiciyi kapat.")
                    time.sleep(15)
                    continue
                log(f"getUpdates hata: {str(data)[:200]}")
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg:
                    continue
                txt = msg.get("text", "")
                if not txt or not txt.lstrip().startswith("/tara"):
                    continue
                cid = msg.get("chat", {}).get("id")
                # SADECE tanımlı grup; DM/başka sohbet yok say
                if not group_matches(cid):
                    if isinstance(cid, int) and cid < 0 and cid not in _diag_sent:
                        _diag_sent.add(cid)
                        send_telegram(
                            f"⚙️ Bu grubun gerçek ID'si: <code>{cid}</code>\n"
                            f"Ayarlı grup: <code>{TG_GROUP}</code>\n"
                            f"Eşleşmiyorsa Railway'de <b>TELEGRAM_GROUP_ID={cid}</b> yapıp yeniden deploy et.",
                            cid)
                    log(f"↩ Eşleşmeyen sohbet {cid} ({msg.get('chat',{}).get('title','')}) — yok sayıldı.")
                    continue
                log(f"✅ Komut alındı: {txt!r} · grup {cid}")
                handle_command(txt, cid)
        except Exception as e:
            log(f"Poll hata: {e}")
            time.sleep(5)


# ─────────────────────────────────────────────
# ZAMANLAMA (otomatik alarm)
# ─────────────────────────────────────────────
def next_run(now_utc):
    base = now_utc.replace(minute=0, second=0, microsecond=0)
    cands = []
    for day_off in (-1, 0, 1):
        for h in (0, 4, 8, 12, 16, 20):
            boundary = (base + timedelta(days=day_off)).replace(hour=h)
            cands.append(boundary - timedelta(minutes=20))
    return min(c for c in cands if c > now_utc)


def main():
    log("🐺 Wolf MTF + Funding Alarm başladı")
    log(f"Ayar: TI_LEN={TI_LEN} LOWER={LOWER_BAND} UPPER={UPPER_BAND} "
        f"FUND_THR={FUNDING_THRESHOLD} UNIVERSE={'ALL' if UNIVERSE==0 else UNIVERSE} "
        f"FILTER_10PCT={FILTER_10PCT} FAST={FAST_UNIVERSE} COOLDOWN={COOLDOWN_MIN}dk")
    if not TG_TOKEN or not TG_CHAT:
        log("⚠ Telegram env eksik — otomatik alarm sadece log'a yazılır.")

    # Komut dinleyiciyi arka planda başlat
    threading.Thread(target=poll_commands, daemon=True).start()

    if RUN_NOW:
        run_scan(TG_CHAT, tag="run_now")

    while True:
        now = datetime.now(timezone.utc)
        nxt = next_run(now)
        wait = (nxt - now).total_seconds()
        log(f"Sonraki otomatik tarama: {nxt.astimezone(TZ3).strftime('%d.%m %H:%M')} (UTC+3) "
            f"— {int(wait)}sn sonra")
        time.sleep(max(1, wait))
        try:
            run_scan(TG_CHAT, tag="sched")   # otomatik -> DM'e
        except Exception as e:
            log(f"Tarama beklenmedik hata: {e}")
        time.sleep(60)


if __name__ == "__main__":
    main()
