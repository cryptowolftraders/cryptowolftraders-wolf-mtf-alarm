#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🐺 WOLF HABER — kriptoyu etkileyen YÜKSEK ÖNEMLİ küresel haberler
------------------------------------------------------------------
Sadece haber atar; işlem açmaz, komut dinlemez (getUpdates YOK -> diğer
botlarla 409 çakışması yaratmaz).

İKİ KAYNAK:
  1) EKONOMİK TAKVİM (ForexFactory haftalık JSON)
     - Sadece ABD (USD) + impact "High" olaylar (FOMC faiz, CPI, NFP, PCE, GDP...)
     - Her sabah 09:00 (UTC+3) günün özeti
     - Her olaydan 30 dk önce hatırlatma (aynı saatteki olaylar tek mesaj)
     - Açıklanınca gerçekleşen rakam: TradingView takviminden denenir
       (ulaşılamazsa sadece hatırlatma kalır)
  2) KRİPTO / MAKRO HABER (CoinDesk, Cointelegraph, The Block, Decrypt RSS)
     - Her başlık puanlanır: kritik kelimeler (Fed, faiz, CPI, ETF onayı, hack,
       iflas, çekim durdurma, delist, tarife, savaş, yaptırım...) + büyük tutarlar
     - Aynı haber 2+ farklı kaynakta çıkarsa ekstra puan (teyit)
     - Sadece eşiği geçenler atılır; günlük üst sınır var

ENV (Railway -> Variables):
  TELEGRAM_BOT_TOKEN   (zorunlu)  - aynı bot (${{worker.TELEGRAM_BOT_TOKEN}} referansı)
  TELEGRAM_GROUP_ID    (zorunlu)  - Crypto Wolf HP grubu
  HABER_KONU           (vars 31)  - 📰 Haberler konusu
  HABER_ESIK           (vars 6)   - haber puan eşiği (yükselt = daha az haber)
  HABER_GUNLUK_MAX     (vars 8)   - günde en fazla kaç haber
  HABER_POLL_SN        (vars 180) - RSS kontrol aralığı (sn)
  TAKVIM_HATIRLATMA_DK (vars 30)
  SABAH_OZET_SAAT      (vars 9)   - UTC+3
  DRY_RUN              (vars false) - true: Telegram'a atmaz, sadece loglar
"""

import os
import re
import time
import html
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_GROUP   = os.getenv("TELEGRAM_GROUP_ID", "").strip()
HABER_KONU = os.getenv("HABER_KONU", "31").strip()
ESIK       = float(os.getenv("HABER_ESIK", "6"))
GUNLUK_MAX = int(os.getenv("HABER_GUNLUK_MAX", "8"))
POLL_SN    = int(os.getenv("HABER_POLL_SN", "180"))
HATIRLATMA_DK = int(os.getenv("TAKVIM_HATIRLATMA_DK", "30"))
SABAH_SAAT = int(os.getenv("SABAH_OZET_SAAT", "9"))
DRY_RUN    = os.getenv("DRY_RUN", "false").lower() == "true"

TZ3 = timezone(timedelta(hours=3))
UA = "Mozilla/5.0 (compatible; wolf-haber/1.0)"
S = requests.Session()
S.headers.update({"User-Agent": UA})

FEEDS = {
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "The Block":     "https://www.theblock.co/rss.xml",
    "Decrypt":       "https://decrypt.co/feed",
}
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
TV_URL = "https://economic-calendar.tradingview.com/events"


def log(m):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {m}", flush=True)


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send(text, preview=False):
    if DRY_RUN or not TG_TOKEN or not TG_GROUP:
        log("DRY/eksik env — mesaj:\n" + text)
        return True
    body = {"chat_id": TG_GROUP, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": not preview}
    if HABER_KONU:
        body["message_thread_id"] = int(HABER_KONU)
    try:
        r = S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=body, timeout=15)
        if r.status_code != 200:
            log(f"Telegram hata {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log(f"Telegram gönderim hatası: {e}")
        return False


# ─────────────────────────────────────────────
# HABER PUANLAMA
# ─────────────────────────────────────────────
# (desen, puan) — başlıkta küçük harfle aranır
KRITIK = [
    (r"\bfomc\b|\bfed\b|federal reserve|powell", 3),
    (r"rate (cut|hike|decision)|interest rate|cuts rates|hikes rates|holds rates", 3),
    (r"\bcpi\b|inflation|jobs report|nonfarm|non-farm|payrolls|\bpce\b|\bgdp\b|recession", 3),
    (r"tariff|trade war|sanction|\bwar\b|missile|strikes? on|invasion|ceasefire", 3),
    (r"etf (approv|reject|launch|denied)|approves? .*etf|spot .*etf|etf inflow|etf outflow", 3),
    (r"\bhack|exploit|drained|stolen|breach|attack(er|ed)?\b", 3),
    (r"bankrupt|insolven|collapse|halts? withdrawals|suspends? withdrawals|freezes? withdrawals", 4),
    (r"delist|depeg|de-peg|liquidat", 3),
    (r"strategic (bitcoin )?reserve|executive order|signs? .*(bill|law)|ban(s|ned)? (crypto|bitcoin)", 3),
    (r"\bsec\b|\bcftc\b|doj|indict|arrest|charged|lawsuit|sues\b", 2),
    (r"blackrock|microstrategy|\bstrategy\b buys|saylor|treasury (department|secretary)|white house|trump", 2),
    (r"all[- ]time high|record high|crash(es|ed)?|plunge|tumble|soar|surge", 2),
]
KONU = [
    (r"bitcoin|\bbtc\b", 1), (r"ethereum|\beth\b", 1), (r"binance|coinbase|kraken|okx|bybit", 1),
    (r"tether|usdt|usdc|circle|stablecoin", 1), (r"congress|senate|regulat|clarity act", 1),
]
ZAYIF = [
    r"price prediction", r"analyst(s)? (say|predict)", r"\bcould\b", r"\bmight\b", r"podcast",
    r"digest", r"weekly", r"opinion", r"how to", r"\?$", r"explained", r"what (is|are)",
    r"interview", r"sponsored", r"guide",
]
DURAK = set("""a an the of to in on for and or with as at by from is are be its it this that
    after over into amid says said report reports new more than about up down vs via""".split())


def kw_puan(title):
    t = title.lower()
    p = 0.0
    for pat, w in KRITIK:
        if re.search(pat, t):
            p += w
    for pat, w in KONU:
        if re.search(pat, t):
            p += w
    m = re.search(r"\$\s?([\d.,]+)\s?(billion|bn|b)\b", t)
    if m:
        p += 2
    else:
        m = re.search(r"\$\s?([\d.,]+)\s?(million|m)\b", t)
        if m:
            try:
                if float(m.group(1).replace(",", "")) >= 100:
                    p += 1
            except ValueError:
                pass
    for pat in ZAYIF:
        if re.search(pat, t):
            p -= 2
    return p


def kelimeler(title):
    w = re.findall(r"[a-z0-9$]+", title.lower())
    return {x for x in w if x not in DURAK and len(x) > 2}


def benzer(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ─────────────────────────────────────────────
# RSS
# ─────────────────────────────────────────────
def rss_cek(ad, url):
    out = []
    try:
        r = S.get(url, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            guid = (it.findtext("guid") or link or title).strip()
            pd = it.findtext("pubDate")
            try:
                ts = parsedate_to_datetime(pd).astimezone(timezone.utc) if pd else None
            except Exception:
                ts = None
            if title:
                out.append({"src": ad, "title": html.unescape(title), "link": link,
                            "id": f"{ad}|{guid}", "ts": ts or datetime.now(timezone.utc)})
    except Exception as e:
        log(f"RSS hata {ad}: {e}")
    return out


class HaberMotoru:
    def __init__(self):
        self.gorulen = set()       # tüm item id'leri
        self.eski = set()          # açılışta var olanlar (tek başına atılmaz)
        self.kumeler = []          # [{"items": [...], "words": set, "posted": bool}]
        self.gun = None
        self.gun_sayac = 0

    def _kumele(self, item):
        w = kelimeler(item["title"])
        for k in self.kumeler:
            if benzer(w, k["words"]) >= 0.35:
                k["items"].append(item)
                k["words"] |= w
                return k
        k = {"items": [item], "words": w, "posted": False}
        self.kumeler.append(k)
        return k

    def kume_puan(self, k):
        kaynaklar = {i["src"] for i in k["items"]}
        p = max(kw_puan(i["title"]) for i in k["items"])
        if len(kaynaklar) >= 2:
            p += 3
        if len(kaynaklar) >= 3:
            p += 1
        return p, kaynaklar

    def tur(self, ilk=False):
        simdi = datetime.now(timezone.utc)
        yeni = []
        for ad, url in FEEDS.items():
            for it in rss_cek(ad, url):
                if it["id"] in self.gorulen:
                    continue
                if simdi - it["ts"] > timedelta(hours=6):
                    self.gorulen.add(it["id"])
                    continue
                self.gorulen.add(it["id"])
                if ilk:
                    self.eski.add(it["id"])
                yeni.append(it)
        dokunulan = []
        for it in yeni:
            k = self._kumele(it)
            if k not in dokunulan:
                dokunulan.append(k)
        # 8 saatten eski kümeleri bırak
        self.kumeler = [k for k in self.kumeler
                        if simdi - max(i["ts"] for i in k["items"]) < timedelta(hours=8)]
        if ilk:
            for k in self.kumeler:
                k["posted"] = True if all(i["id"] in self.eski for i in k["items"]) else k["posted"]
            log(f"Haber ısınma: {len(self.gorulen)} başlık görüldü, hiçbiri atılmadı")
            return

        bugun = datetime.now(TZ3).date()
        if self.gun != bugun:
            self.gun, self.gun_sayac = bugun, 0

        for k in dokunulan:
            if k["posted"]:
                continue
            if all(i["id"] in self.eski for i in k["items"]):
                continue
            p, kaynaklar = self.kume_puan(k)
            log(f"puan {p:.0f} [{', '.join(sorted(kaynaklar))}] {k['items'][0]['title'][:90]}")
            if p < ESIK:
                continue
            if self.gun_sayac >= GUNLUK_MAX:
                log("Günlük haber sınırı doldu — atlanıyor")
                continue
            if self.haber_at(k, p, kaynaklar):
                k["posted"] = True
                self.gun_sayac += 1

    def haber_at(self, k, p, kaynaklar):
        ana = max(k["items"], key=lambda i: kw_puan(i["title"]))
        seviye = "🔴 <b>ÇOK ÖNEMLİ</b>" if p >= ESIK + 4 else "🟠 <b>ÖNEMLİ</b>"
        saat = ana["ts"].astimezone(TZ3).strftime("%H:%M")
        satirlar = [f"📰 {seviye} · {saat} (UTC+3)", "", f"<b>{html.escape(ana['title'])}</b>", ""]
        linkler = []
        goruldu = set()
        for i in k["items"]:
            if i["src"] in goruldu:
                continue
            goruldu.add(i["src"])
            linkler.append(f'<a href="{html.escape(i["link"], quote=True)}">{html.escape(i["src"])}</a>')
        satirlar.append("Kaynak: " + " · ".join(linkler))
        if len(kaynaklar) >= 2:
            satirlar.append(f"✅ {len(kaynaklar)} kaynakta teyitli")
        return send("\n".join(satirlar))


# ─────────────────────────────────────────────
# EKONOMİK TAKVİM
# ─────────────────────────────────────────────
class Takvim:
    def __init__(self):
        self.olaylar = []          # [{"title","ts","forecast","previous"}]
        self.son_cek = 0.0
        self.hatirlatilan = set()
        self.sonuc_bakilan = set()
        self.ozet_gun = None

    def cek(self):
        if time.time() - self.son_cek < 1800 and self.olaylar:
            return
        try:
            r = S.get(FF_URL, timeout=20)
            r.raise_for_status()
            data = r.json()
            ol = []
            for d in data:
                if d.get("country") != "USD" or d.get("impact") != "High":
                    continue
                try:
                    ts = datetime.fromisoformat(d["date"]).astimezone(timezone.utc)
                except Exception:
                    continue
                ol.append({"title": d.get("title", ""), "ts": ts,
                           "forecast": d.get("forecast", ""), "previous": d.get("previous", "")})
            self.olaylar = sorted(ol, key=lambda x: x["ts"])
            self.son_cek = time.time()
            log(f"Takvim: bu hafta {len(self.olaylar)} yüksek etkili ABD olayı")
        except Exception as e:
            log(f"Takvim hata: {e}")
            self.son_cek = time.time() - 1500   # 5 dk sonra tekrar dene

    @staticmethod
    def _satir(o):
        ek = []
        if o["forecast"]:
            ek.append(f"Beklenti: {html.escape(o['forecast'])}")
        if o["previous"]:
            ek.append(f"Önceki: {html.escape(o['previous'])}")
        return f"• 🇺🇸 <b>{html.escape(o['title'])}</b>" + (f"  ({' · '.join(ek)})" if ek else "")

    def tur(self):
        self.cek()
        simdi = datetime.now(timezone.utc)
        yerel = simdi.astimezone(TZ3)

        # Sabah özeti
        if yerel.hour >= SABAH_SAAT and self.ozet_gun != yerel.date():
            self.ozet_gun = yerel.date()
            bugun = [o for o in self.olaylar if o["ts"].astimezone(TZ3).date() == yerel.date()]
            if bugun:
                st = ["🗓 <b>BUGÜN — Yüksek etkili ABD verileri</b>", ""]
                for o in bugun:
                    st.append(f"{o['ts'].astimezone(TZ3).strftime('%H:%M')}  " + self._satir(o)[2:])
                st += ["", "Bu saatlerde volatilite artabilir."]
                send("\n".join(st))

        # Hatırlatma (aynı saatteki olaylar tek mesaj)
        gruplar = {}
        for o in self.olaylar:
            gruplar.setdefault(o["ts"], []).append(o)
        for ts, grup in gruplar.items():
            kalan = (ts - simdi).total_seconds() / 60
            if 0 < kalan <= HATIRLATMA_DK and ts not in self.hatirlatilan:
                self.hatirlatilan.add(ts)
                st = [f"⏰ <b>{int(round(kalan))} dk sonra</b> · {ts.astimezone(TZ3).strftime('%H:%M')} (UTC+3)", ""]
                st += [self._satir(o) for o in grup]
                st += ["", "⚠️ Açıklama anında sert fitil riski — pozisyon/stop kontrol."]
                send("\n".join(st))
            # Sonuç: açıklandıktan 1-25 dk sonra TradingView'dan dene
            if -25 <= kalan <= -1 and ts not in self.sonuc_bakilan:
                if self.sonuc_at(ts, grup):
                    self.sonuc_bakilan.add(ts)
            elif kalan < -25:
                self.sonuc_bakilan.add(ts)

    def sonuc_at(self, ts, grup):
        try:
            r = S.get(TV_URL, params={
                "from": (ts - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "to": (ts + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "countries": "US"},
                headers={"Origin": "https://www.tradingview.com",
                         "Referer": "https://www.tradingview.com/"}, timeout=15)
            if r.status_code != 200:
                log(f"TV takvim {r.status_code}")
                return False
            res = [e for e in r.json().get("result", [])
                   if e.get("importance", 0) >= 1 and e.get("actual") not in (None, "")]
            if not res:
                return False
            st = [f"📊 <b>AÇIKLANDI</b> · {ts.astimezone(TZ3).strftime('%H:%M')} (UTC+3)", ""]
            for e in res:
                unit = e.get("unit") or ""
                def f(v):
                    return "—" if v in (None, "") else f"{v}{unit}"
                st.append(f"• 🇺🇸 <b>{html.escape(e.get('title',''))}</b>: {f(e.get('actual'))}  "
                          f"(Beklenti {f(e.get('forecast'))} · Önceki {f(e.get('previous'))})")
            return send("\n".join(st))
        except Exception as e:
            log(f"TV sonuç hata: {e}")
            return False


# ─────────────────────────────────────────────
# ANA DÖNGÜ
# ─────────────────────────────────────────────
def main():
    log(f"🐺 Wolf Haber başladı · grup {TG_GROUP} · konu {HABER_KONU} · eşik {ESIK:g} · "
        f"günlük max {GUNLUK_MAX} · DRY_RUN={DRY_RUN}")
    if not TG_TOKEN or not TG_GROUP:
        log("⚠ TELEGRAM_BOT_TOKEN / TELEGRAM_GROUP_ID eksik — sadece log.")

    motor = HaberMotoru()
    takvim = Takvim()
    motor.tur(ilk=True)
    takvim.cek()
    # açılışta, zamanı geçmiş hatırlatma/özetleri tekrar atma
    simdi = datetime.now(timezone.utc)
    for o in takvim.olaylar:
        if (o["ts"] - simdi).total_seconds() / 60 <= HATIRLATMA_DK:
            takvim.hatirlatilan.add(o["ts"])
            takvim.sonuc_bakilan.add(o["ts"])
    if simdi.astimezone(TZ3).hour >= SABAH_SAAT:
        takvim.ozet_gun = simdi.astimezone(TZ3).date()

    son_haber = time.time()
    while True:
        try:
            takvim.tur()
        except Exception as e:
            log(f"Takvim döngü hata: {e}")
        if time.time() - son_haber >= POLL_SN:
            son_haber = time.time()
            try:
                motor.tur()
            except Exception as e:
                log(f"Haber döngü hata: {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()
