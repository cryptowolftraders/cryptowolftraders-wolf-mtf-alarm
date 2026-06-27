#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🐺 WOLF MTF + FUNDING ALARM
---------------------------------------------------------------
Wolf MTF Scanner'ın LONG/SHORT mantığını (Pearson TI, 4H+1H) +
funding yönü filtresini sunucu tarafında çalıştırır ve eşleşen
coinleri Telegram'a atar. Hiçbir yerde İŞLEM AÇMAZ — sadece haber.

Zamanlama: her 4H kapanıştan 20 dk önce
  UTC kapanışlar: 00 / 04 / 08 / 12 / 16 / 20
  Çalışma (UTC):  23:40 / 03:40 / 07:40 / 11:40 / 15:40 / 19:40
  Çalışma (UTC+3): 02:40 / 06:40 / 10:40 / 14:40 / 18:40 / 22:40

Filtre (OB / FVG / fibo DİKKATE ALINMAZ):
  • LONG  sinyali + funding YEŞİL (fr < -threshold)  -> listeye
  • SHORT sinyali + funding KIRMIZI (fr > +threshold) -> listeye

ENV değişkenleri (Railway -> Variables):
  TELEGRAM_BOT_TOKEN   (zorunlu)
  TELEGRAM_CHAT_ID     (zorunlu)
  TI_LEN               (vars 12)   - TI korelasyon uzunluğu
  UPPER_BAND           (vars 88)   - SHORT eşiği
  LOWER_BAND           (vars 12)   - LONG eşiği
  FUNDING_THRESHOLD    (vars 0.01) - % cinsinden yeşil/kırmızı eşiği
  UNIVERSE             (vars 0)     - 0=tüm coin, >0 = hacme göre TOP N
  FILTER_10PCT         (vars true)  - 24s |değişim| >= %10 olanları ele
  RUN_NOW              (vars false) - true ise açılışta hemen bir tara
"""

import os
import sys
import math
import time
import html
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
FUNDING_THRESHOLD = float(os.getenv("FUNDING_THRESHOLD", "0.01"))   # %
UNIVERSE          = int(os.getenv("UNIVERSE", "0"))                 # 0 = tümü
FILTER_10PCT      = os.getenv("FILTER_10PCT", "true").lower() == "true"
RUN_NOW           = os.getenv("RUN_NOW", "false").lower() == "true"

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()

EXCLUDE = {"BTCDOMUSDT", "DEFIUSDT", "BLUEBIRDUSDT", "BTCSTUSDT"}
KLINE_LIMIT = TI_LEN + 8          # TI için fazlasıyla yeter (canlı mum dahil)
WORKERS = 8                       # paralel kline isteği (Binance ban riskine karşı ölçülü)
TZ3 = timezone(timedelta(hours=3))

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "wolf-mtf-alarm/1.0"})


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


# ─────────────────────────────────────────────
# TI / SİNYAL — MTF Scanner JS portu (birebir)
# ─────────────────────────────────────────────
def pearson_corr(y, length):
    """corr(close, bar_index, length) — JS pearsonCorr ile aynı."""
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
        return -1   # SHORT
    if ti <= lower:
        return 1    # LONG
    return 0        # nötr


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
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.2 * (attempt + 1))
    return None


def fetch_universe():
    """24h ticker -> hacme göre sıralı USDT-perp coin listesi (+ fiyat/değişim)."""
    data = get("/fapi/v1/ticker/24hr")
    coins = [
        d for d in data
        if d["symbol"].endswith("USDT") and d["symbol"] not in EXCLUDE
    ]
    coins.sort(key=lambda d: float(d.get("quoteVolume", 0)), reverse=True)
    if FILTER_10PCT:
        coins = [d for d in coins if abs(float(d.get("priceChangePercent", 0))) < 10]
    if UNIVERSE > 0:
        coins = coins[:UNIVERSE]
    return {
        d["symbol"]: {
            "price": float(d["lastPrice"]),
            "change24h": float(d["priceChangePercent"]),
            "volume": float(d["quoteVolume"]),
        }
        for d in coins
    }


def fetch_funding_all():
    """premiumIndex (symbol'süz) -> tüm coinlerin son funding oranı (% cinsi)."""
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
    # k[4] = close. Son (oluşmakta olan) mum DAHİL — MTF Scanner ile aynı:
    # 4H kapanışa 20 dk kala forming mumun anlık değeriyle değerlendirilir.
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
        return None  # funding teyidi olmadan alma

    # Funding yön filtresi — karttaki renk mantığıyla aynı
    if signal == "long" and fr < -FUNDING_THRESHOLD:        # yeşil funding
        pass
    elif signal == "short" and fr > FUNDING_THRESHOLD:      # kırmızı funding
        pass
    else:
        return None

    return {
        "symbol": symbol,
        "signal": signal,
        "ti4h": round(ti4h, 1),
        "ti1h": round(ti1h, 1),
        "funding": fr,
        "price": meta["price"],
        "change24h": meta["change24h"],
        "volume": meta["volume"],
    }


def fmt_price(p):
    if p >= 1000:
        return f"${p:,.1f}"
    if p >= 1:
        return f"${p:,.3f}"
    return f"${p:.6f}"


def run_scan():
    log("Tarama başlıyor…")
    try:
        universe = fetch_universe()
        funding = fetch_funding_all()
    except Exception as e:
        log(f"Veri çekme hatası: {e}")
        send_telegram(f"🐺 WOLF MTF ALARM — veri hatası: {html.escape(str(e))}")
        return

    log(f"{len(universe)} coin · {len(funding)} funding kaydı")

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

    log(f"Sonuç: LONG {len(longs)} · SHORT {len(shorts)}")
    send_results(len(universe), longs, shorts)


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        log("⚠ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID yok — mesaj atlanıyor")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # Telegram limiti 4096 karakter — uzunsa parçala
    for chunk in _split(text, 3900):
        try:
            resp = SESSION.post(url, json={
                "chat_id": TG_CHAT,
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
            f"4H:{h['ti4h']} 1H:{h['ti1h']}  "
            f"fund:{fr:+.4f}%")


def send_results(scanned, longs, shorts):
    now3 = datetime.now(TZ3).strftime("%d.%m %H:%M")
    head = (f"🐺 <b>WOLF MTF + FUNDING ALARM</b>\n"
            f"🕐 4H kapanışa 20 dk · {now3} (UTC+3)\n"
            f"Taranan: {scanned} · ▲ LONG: {len(longs)} · ▼ SHORT: {len(shorts)}")

    if not longs and not shorts:
        send_telegram(head + "\n\n— Şartı sağlayan coin yok.")
        return

    parts = [head]
    if longs:
        parts.append("\n▲ <b>LONG</b> (yeşil funding)")
        parts += [_row(h) for h in longs]
    if shorts:
        parts.append("\n▼ <b>SHORT</b> (kırmızı funding)")
        parts += [_row(h) for h in shorts]
    send_telegram("\n".join(parts))


# ─────────────────────────────────────────────
# ZAMANLAMA
# ─────────────────────────────────────────────
def next_run(now_utc):
    """Bir sonraki '4H kapanış − 20 dk' zamanını (UTC) döndürür."""
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
        f"FILTER_10PCT={FILTER_10PCT}")
    if not TG_TOKEN or not TG_CHAT:
        log("⚠ Telegram env değişkenleri eksik — sinyaller sadece log'a yazılır.")

    if RUN_NOW:
        run_scan()

    while True:
        now = datetime.now(timezone.utc)
        nxt = next_run(now)
        wait = (nxt - now).total_seconds()
        log(f"Sonraki tarama: {nxt.astimezone(TZ3).strftime('%d.%m %H:%M')} (UTC+3) "
            f"— {int(wait)}sn sonra")
        time.sleep(max(1, wait))
        try:
            run_scan()
        except Exception as e:
            log(f"Tarama beklenmedik hata: {e}")
        time.sleep(60)  # aynı pencerede tekrar tetiklenmeyi önle


if __name__ == "__main__":
    main()
