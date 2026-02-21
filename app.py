"""
TCSM THAI STOCK PRO v4.0 — SET STOCK SCANNER (PRODUCTION GRADE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIXES from v3.2:
  ✅ eventlet.monkey_patch() FIRST before ALL imports
  ✅ Removed delisted tickers: BTSG, MAKRO, INTUCH (merged/delisted)
  ✅ Fixed Flask application context in background scanner thread
  ✅ Fixed socket shutdown / Bad file descriptor errors
  ✅ Proper error boundaries for all emit calls
  ✅ Graceful handling of Yahoo 401/404 per-ticker
  ✅ Ultimate professional UI with dark glassmorphism
  ✅ Enhanced real-time WebSocket with auto-recovery
  ✅ Mobile-responsive design

Deploy: gunicorn --worker-class eventlet -w 1 -b 0.0.0.0:$PORT app:app
"""
# ═══════════════════════════════════════════════════════════════════════════════
#  CRITICAL: eventlet monkey_patch MUST be the VERY FIRST thing
# ═══════════════════════════════════════════════════════════════════════════════
import eventlet
eventlet.monkey_patch(all=True)

# NOW import everything else
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit
from datetime import datetime, timezone, timedelta
from functools import wraps
import pandas as pd
import numpy as np
import hashlib, secrets, time, json, os, traceback, logging, math, threading, io
import requests as req_lib

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('TCSM-TH')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

socketio = SocketIO(
    app, cors_allowed_origins="*", async_mode='eventlet',
    ping_timeout=120, ping_interval=25,
    logger=False, engineio_logger=False, always_connect=True,
    manage_session=False
)

# ═══════════════════════════════════════════════════════════════════════════════
#  TIME UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════
ICT = timezone(timedelta(hours=7))
def now(): return datetime.now(ICT)
def ts(f="%H:%M:%S"): return now().strftime(f)
def dts(): return now().strftime("%Y-%m-%d %H:%M:%S")

def sanitize(obj):
    if obj is None: return None
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return 0.0 if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, (np.bool_,)): return bool(obj)
    if isinstance(obj, np.ndarray): return obj.tolist()
    if isinstance(obj, pd.Timestamp): return str(obj)
    if isinstance(obj, dict): return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)): return [sanitize(i) for i in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj): return 0.0
    return obj

def safe_emit(event, data, **kwargs):
    """Emit with full error protection — no crashes on bad fd or context"""
    try:
        socketio.emit(event, sanitize(data), **kwargs)
    except (BrokenPipeError, OSError, IOError):
        pass  # Client disconnected — safe to ignore
    except RuntimeError as e:
        if 'Working outside' not in str(e):
            logger.error(f"Emit '{event}' RuntimeError: {e}")
    except Exception as e:
        logger.warning(f"Emit '{event}' failed: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG — Removed delisted: BTSG, MAKRO, INTUCH (all confirmed delisted/merged)
# ═══════════════════════════════════════════════════════════════════════════════
class C:
    SCAN_INTERVAL = 90
    BATCH_SIZE = 5

    STOCH_RSI_K=3; STOCH_RSI_D=3; RSI_PD=14; STOCH_PD=14
    ENH_K=21; ENH_D=3; ENH_SLOW=5
    MACD_F=12; MACD_S=26; MACD_SIG=9; MACD_NLB=500
    OB=80.0; OS=20.0; NEU_HI=60.0; NEU_LO=40.0
    SIG_TH=38; CHG_TH=14; MOM_SM=3; SWING_LB=5
    RSI_W=18; MACD_W=18; STOCH_W=14; CCI_W=14; WPR_W=12; ADX_W=12; MOM_W=12
    CCI_PD=20; WPR_PD=14; ADX_PD=14; MOM_PD=14
    TREND_MA=50; MIN_MOM=0.4; STRONG_STR=52.0
    ATR_PD=14; ATR_SL=1.5; RR=2.0
    DIV_BARS=20; VOL_PD=20

    # Thai stocks — VERIFIED active tickers on SET (Feb 2026)
    # Removed: BTSG (merged→BTS), MAKRO (merged→CPALL/Lotus's), INTUCH (merged→ADVANC)
    STOCKS = [
        "PTT","PTTEP","PTTGC","TOP","IRPC","BANPU","OR",
        "KBANK","BBL","SCB","KTB","TTB","TISCO","KKP",
        "ADVANC","TRUE","DELTA","HANA","KCE",
        "AOT","BTS","BEM",
        "CPALL","CPF","TU","CBG","GLOBAL",
        "CPN","CRC","LH","AP","SIRI","SPALI","QH","ORI",
        "BDMS","BH","BCH",
        "SCC","SCGP","IVL","MTC",
        "GULF","EGCO","GPSC","RATCH","EA","BGRIM",
        "AWC","CENTEL","MINT","HMPRO",
        "SAWAD","JMT","JMART","KTC","TIDLOR",
        "COM7","PLANB","VGI","WHA",
        "BAM","OSP","AMATA",
        "GGC","BAFS","BCP",
        "NER","GFPT",
        "RS","MAJOR","STGT",
        "ITC","ITD","CK",
        "MEGA","CHG",
    ]
    STOCKS = list(dict.fromkeys(STOCKS))

    # Tickers that have failed — skip them automatically after N failures
    DELIST_THRESHOLD = 3

    SECTOR_MAP = {}
    _e = {"PTT","PTTEP","PTTGC","TOP","IRPC","BANPU","OR","GULF","EGCO","GPSC","RATCH","EA","BGRIM","GGC","BAFS","BCP"}
    _b = {"KBANK","BBL","SCB","KTB","TTB","TISCO","KKP","BAM"}
    _t = {"ADVANC","TRUE","DELTA","HANA","KCE","COM7","PLANB"}
    _p = {"CPN","CRC","LH","AP","SIRI","SPALI","QH","ORI","AWC","CENTEL","MINT","HMPRO","AMATA","WHA"}
    _h = {"BDMS","BH","BCH","CHG","MEGA","STGT"}
    _c = {"CPALL","CPF","TU","CBG","GLOBAL","NER","GFPT","OSP","MAJOR"}
    _tr = {"AOT","BTS","BEM"}
    _i = {"SCC","SCGP","IVL","MTC","ITC","ITD","CK"}
    _f = {"SAWAD","JMT","JMART","KTC","TIDLOR","RS","VGI"}
    for s in _e: SECTOR_MAP[s]="Energy"
    for s in _b: SECTOR_MAP[s]="Banking"
    for s in _t: SECTOR_MAP[s]="Tech"
    for s in _p: SECTOR_MAP[s]="Property"
    for s in _h: SECTOR_MAP[s]="Health"
    for s in _c: SECTOR_MAP[s]="Consumer"
    for s in _tr: SECTOR_MAP[s]="Transport"
    for s in _i: SECTOR_MAP[s]="Industry"
    for s in _f: SECTOR_MAP[s]="Finance"

    @staticmethod
    def get_sector(sym): return C.SECTOR_MAP.get(sym, "Other")
    @staticmethod
    def yf_sym(sym): return f"{sym}.BK"


# ═══════════════════════════════════════════════════════════════════════════════
#  YAHOO FINANCE CLIENT — Cloud-safe with auto-delist detection
# ═══════════════════════════════════════════════════════════════════════════════
class YFClient:
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
    }

    def __init__(self):
        self._cache = {}
        self._cache_time = {}
        self._lock = threading.Lock()
        self._cookie = None
        self._crumb = None
        self._crumb_time = 0
        self._session = None
        self._fail_count = {}
        self._dead_tickers = set()  # Auto-detected delisted tickers

    def _get_session(self):
        if self._session is None:
            self._session = req_lib.Session()
            self._session.headers.update(self.HEADERS)
        return self._session

    def _get_cookie_crumb(self):
        if self._crumb and time.time() - self._crumb_time < 300:
            return self._cookie, self._crumb
        try:
            s = self._get_session()
            s.get('https://fc.yahoo.com', timeout=10, allow_redirects=True)
            r2 = s.get('https://query1.finance.yahoo.com/v1/test/getcrumb', timeout=10)
            if r2.status_code == 200 and r2.text and 'Too Many' not in r2.text:
                self._crumb = r2.text.strip()
                self._cookie = s.cookies
                self._crumb_time = time.time()
                logger.info(f"Got Yahoo crumb: {self._crumb[:8]}...")
                return self._cookie, self._crumb
        except Exception as e:
            logger.warning(f"Cookie/crumb failed: {e}")
        return None, None

    def is_dead(self, symbol):
        return symbol in self._dead_tickers

    def _mark_dead(self, symbol, reason=""):
        fc = self._fail_count.get(symbol, 0) + 1
        self._fail_count[symbol] = fc
        if fc >= C.DELIST_THRESHOLD:
            self._dead_tickers.add(symbol)
            logger.warning(f"☠ {symbol}.BK marked as dead/delisted after {fc} failures ({reason})")
            return True
        return False

    def _fetch_direct(self, symbol, period="3mo", interval="1h"):
        yf_sym = C.yf_sym(symbol)
        try:
            s = self._get_session()
            cookie, crumb = self._get_cookie_crumb()
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yf_sym}"
            params = {'range': period, 'interval': interval,
                      'includePrePost': 'false', 'events': 'div,splits'}
            if crumb:
                params['crumb'] = crumb

            r = s.get(url, params=params, timeout=15,
                      cookies=cookie if cookie else None)

            if r.status_code == 200:
                data = r.json()
                chart = data.get('chart', {}).get('result', [])
                if not chart:
                    err = data.get('chart', {}).get('error', {})
                    if err and 'No data found' in str(err.get('description', '')):
                        self._mark_dead(symbol, "No data found from API")
                    return None
                result = chart[0]
                timestamps = result.get('timestamp', [])
                quote = result.get('indicators', {}).get('quote', [{}])[0]
                if timestamps and quote:
                    df = pd.DataFrame({
                        'open': quote.get('open', []),
                        'high': quote.get('high', []),
                        'low': quote.get('low', []),
                        'close': quote.get('close', []),
                        'volume': quote.get('volume', []),
                    }, index=pd.to_datetime(timestamps, unit='s'))
                    df = df.dropna(subset=['close'])
                    df = df[df['close'] > 0]
                    if len(df) >= 20:
                        return df
            elif r.status_code == 404:
                self._mark_dead(symbol, "404 Not Found")
            elif r.status_code == 429:
                logger.warning(f"Direct API 429 for {yf_sym}, backing off")
                eventlet.sleep(8)
            elif r.status_code in (401, 403):
                logger.warning(f"Direct API {r.status_code} for {yf_sym}")
        except Exception as e:
            logger.warning(f"Direct API {yf_sym}: {e}")
        return None

    def _fetch_yfinance(self, symbol, period="3mo", interval="1h"):
        yf_sym = C.yf_sym(symbol)
        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_sym)
            df = ticker.history(period=period, interval=interval)
            if df is not None and len(df) >= 20:
                df.columns = [c.lower() for c in df.columns]
                df = df.rename(columns={'stock splits': 'splits'})
                for col in ['open','high','low','close','volume']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.dropna(subset=['close'])
                df = df[df['close'] > 0]
                if len(df) >= 20:
                    return df
        except Exception as e:
            es = str(e)
            if 'possibly delisted' in es or 'No data found' in es:
                self._mark_dead(symbol, es[:80])
            elif '401' in es or '404' in es:
                self._mark_dead(symbol, es[:80])
            else:
                logger.warning(f"yfinance {yf_sym}: {e}")
        return None

    def _fetch_yf_download(self, symbol, period="3mo", interval="1h"):
        yf_sym = C.yf_sym(symbol)
        try:
            import yfinance as yf
            df = yf.download(yf_sym, period=period, interval=interval,
                             progress=False, timeout=15)
            if df is not None and len(df) >= 20:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
                else:
                    df.columns = [str(c).lower() for c in df.columns]
                for col in ['open','high','low','close','volume']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.dropna(subset=['close'])
                df = df[df['close'] > 0]
                if len(df) >= 20:
                    return df
        except Exception as e:
            es = str(e)
            if 'possibly delisted' in es or 'No data found' in es:
                self._mark_dead(symbol, es[:80])
            else:
                logger.warning(f"yf.download {yf_sym}: {e}")
        return None

    def get_history(self, symbol, period="3mo", interval="1h"):
        if symbol in self._dead_tickers:
            return None

        cache_key = f"{symbol}_{period}_{interval}"
        with self._lock:
            if cache_key in self._cache and time.time() - self._cache_time.get(cache_key, 0) < 55:
                return self._cache[cache_key]

        df = None
        methods = [
            ("direct", self._fetch_direct),
            ("yfinance", self._fetch_yfinance),
            ("yf_download", self._fetch_yf_download),
        ]

        for name, method in methods:
            if symbol in self._dead_tickers:
                return None
            try:
                df = method(symbol, period, interval)
                if df is not None and len(df) >= 20:
                    with self._lock:
                        self._cache[cache_key] = df
                        self._cache_time[cache_key] = time.time()
                    self._fail_count[symbol] = 0
                    logger.info(f"✓ {symbol}.BK via {name} ({len(df)} rows)")
                    return df
            except Exception as e:
                logger.warning(f"{name} failed for {symbol}: {e}")
            eventlet.sleep(0.5)

        if symbol not in self._dead_tickers:
            self._mark_dead(symbol, "all methods failed")
        return None

    def get_daily(self, symbol):
        if symbol in self._dead_tickers:
            return None
        return self.get_history(symbol, period="6mo", interval="1d")

yfc = YFClient()


# ═══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════
def sma(s, n): return s.rolling(n, min_periods=1).mean()
def ema(s, n): return s.ewm(span=n, adjust=False, min_periods=1).mean()
def rma(s, n): return s.ewm(alpha=1.0/n, adjust=False, min_periods=1).mean()

def calc_rsi(close, period=14):
    d = close.diff()
    g = d.where(d > 0, 0.0).ewm(alpha=1.0/period, adjust=False, min_periods=1).mean()
    l = (-d.where(d < 0, 0.0)).ewm(alpha=1.0/period, adjust=False, min_periods=1).mean()
    return 100.0 - 100.0 / (1.0 + g / (l + 1e-10))

def calc_stoch(close, high, low, period):
    ll = low.rolling(period, min_periods=1).min()
    hh = high.rolling(period, min_periods=1).max()
    return ((close - ll) / (hh - ll + 1e-10)) * 100.0

def calc_cci(close, high, low, period):
    tp = (high + low + close) / 3.0
    ma = tp.rolling(period, min_periods=1).mean()
    md = tp.rolling(period, min_periods=1).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - ma) / (0.015 * md + 1e-10)

def calc_wpr(close, high, low, period):
    hh = high.rolling(period, min_periods=1).max()
    ll = low.rolling(period, min_periods=1).min()
    return ((hh - close) / (hh - ll + 1e-10)) * -100.0

def calc_atr(high, low, close, period):
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()

def calc_adx(high, low, close, period):
    up = high.diff(); dn = -low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=high.index)
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    atr_s = rma(tr, period)
    pdi = 100.0 * rma(pdm, period) / (atr_s + 1e-10)
    mdi = 100.0 * rma(mdm, period) / (atr_s + 1e-10)
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
    return rma(dx, period), pdi, mdi

def calc_obv(close, volume):
    return (volume * np.sign(close.diff())).cumsum()


# ═══════════════════════════════════════════════════════════════════════════════
#  THAI MARKET SESSION
# ═══════════════════════════════════════════════════════════════════════════════
def get_market_session():
    n = now(); h = n.hour; m = n.minute; wd = n.weekday(); t = h * 60 + m
    if wd >= 5: return "วันหยุด WEEKEND 🔒", False
    if t < 570:   return "ก่อนเปิดตลาด PRE-MARKET", False
    elif t < 600: return "เปิดจับคู่ PRE-OPEN ☀️", True
    elif t < 660: return "เช้า MORNING ⭐⭐", True
    elif t < 750: return "เช้า MORNING ⭐", True
    elif t < 870: return "พักเที่ยง LUNCH 🍜", False
    elif t < 930: return "บ่าย AFTERNOON ⭐", True
    elif t < 990: return "ช่วงปิด CLOSING ⭐⭐", True
    elif t < 1020: return "ปิดจับคู่ CLOSING", False
    else: return "ปิดตลาด CLOSED 🔒", False


# ═══════════════════════════════════════════════════════════════════════════════
#  TCSM ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_stock(symbol):
    try:
        dec = 2; sector = C.get_sector(symbol)
        df = yfc.get_history(symbol, "3mo", "1h")
        if df is None or len(df) < 30:
            return None
        close = df['close']; high = df['high']; low = df['low']; volume = df['volume']
        price = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else price
        chg = price - prev
        chg_pct = (chg / prev * 100) if prev else 0
        if price < 1: dec = 4
        elif price < 10: dec = 3
        else: dec = 2

        df_daily = yfc.get_daily(symbol)

        # ENGINE A: TRIPLE CONFLUENCE
        rv = calc_rsi(close, C.RSI_PD)
        rh = rv.rolling(C.STOCH_PD, min_periods=1).max()
        rl = rv.rolling(C.STOCH_PD, min_periods=1).min()
        srK = sma(((rv - rl) / (rh - rl + 1e-10)) * 100.0, C.STOCH_RSI_K)
        srD = sma(srK, C.STOCH_RSI_D)
        ehh = high.rolling(C.ENH_K, min_periods=1).max()
        ell = low.rolling(C.ENH_K, min_periods=1).min()
        enK = sma(((close - ell) / (ehh - ell + 1e-10)) * 100.0, C.ENH_SLOW)
        enD = sma(enK, C.ENH_D)
        ml = ema(close, C.MACD_F) - ema(close, C.MACD_S)
        sl = ema(ml, C.MACD_SIG)
        lb = min(len(df), C.MACD_NLB)
        rH = pd.concat([ml.rolling(lb, min_periods=1).max(), sl.rolling(lb, min_periods=1).max()], axis=1).max(axis=1)
        rL = pd.concat([ml.rolling(lb, min_periods=1).min(), sl.rolling(lb, min_periods=1).min()], axis=1).min(axis=1)
        rng = rH - rL; pH = rH + rng * 0.1; pL = rL - rng * 0.1
        mnA = ((ml - pL) / (pH - pL + 1e-10) * 100.0).clip(0, 100)
        msA = ((sl - pL) / (pH - pL + 1e-10) * 100.0).clip(0, 100)
        sr_k = float(srK.iloc[-1]); sr_d = float(srD.iloc[-1])
        en_k = float(enK.iloc[-1]); en_d = float(enD.iloc[-1])
        mn_a = float(mnA.iloc[-1]); ms_a = float(msA.iloc[-1])

        def xo(a, b): return len(a) >= 2 and a.iloc[-1] > b.iloc[-1] and a.iloc[-2] <= b.iloc[-2]
        def xu(a, b): return len(a) >= 2 and a.iloc[-1] < b.iloc[-1] and a.iloc[-2] >= b.iloc[-2]
        def ir(v): return v > C.OS and v < C.OB

        bx = sum([xo(srK, srD) and ir(sr_k), xo(enK, enD) and ir(en_k), xo(mnA, msA) and ir(mn_a)])
        sx = sum([xu(srK, srD) and ir(sr_k), xu(enK, enD) and ir(en_k), xu(mnA, msA) and ir(mn_a)])
        bull_zn = sum([sr_k > C.NEU_HI, en_k > C.NEU_HI, mn_a > C.NEU_HI])
        bear_zn = sum([sr_k < C.NEU_LO, en_k < C.NEU_LO, mn_a < C.NEU_LO])
        ema20 = float(ema(close, 20).iloc[-1])
        ema50 = float(ema(close, 50).iloc[-1])
        above_ma = price > ema50
        avg_mom_a = float((srK.diff().abs().iloc[-1] + enK.diff().abs().iloc[-1] + mnA.diff().abs().iloc[-1]) / 3.0)
        conf_buy = bool(bx >= 2 and above_ma and avg_mom_a >= C.MIN_MOM)
        conf_sell = bool(sx >= 2 and not above_ma and avg_mom_a >= C.MIN_MOM)

        def recent_xo(a, b, bars=3):
            for i in range(1, min(bars + 1, len(a))):
                if abs(-i - 1) < len(a) and a.iloc[-i] > b.iloc[-i] and a.iloc[-i-1] <= b.iloc[-i-1]:
                    return True
            return False
        def recent_xu(a, b, bars=3):
            for i in range(1, min(bars + 1, len(a))):
                if abs(-i - 1) < len(a) and a.iloc[-i] < b.iloc[-i] and a.iloc[-i-1] >= b.iloc[-i-1]:
                    return True
            return False

        rbx = sum([recent_xo(srK, srD), recent_xo(enK, enD), recent_xo(mnA, msA)])
        rsx = sum([recent_xu(srK, srD), recent_xu(enK, enD), recent_xu(mnA, msA)])
        if not conf_buy and rbx >= 2 and above_ma:
            conf_buy = True; bx = max(bx, rbx)
        if not conf_sell and rsx >= 2 and not above_ma:
            conf_sell = True; sx = max(sx, rsx)
        bx = int(bx); sx = int(sx); bull_zn = int(bull_zn); bear_zn = int(bear_zn)

        # ENGINE B: SMARTMOMENTUM
        rsi_v = float(calc_rsi(close, C.RSI_PD).iloc[-1])
        rsi_sc = float((rsi_v - 50) * 2.0)
        atr_v = float(calc_atr(high, low, close, C.ATR_PD).iloc[-1])
        if pd.isna(atr_v) or atr_v <= 0:
            atr_v = float((high - low).mean())
        ml_v = float(ml.iloc[-1]); sl_v = float(sl.iloc[-1])
        macd_nb = float(np.clip((ml_v / max(atr_v, 1e-10)) * 50.0, -100, 100))
        macd_sc = float(np.clip(macd_nb + (15 if ml_v > sl_v else -15), -100, 100))
        stv = calc_stoch(close, high, low, C.STOCH_PD)
        st_sig = sma(stv, 3)
        stoch_sc = float(np.clip((stv.iloc[-1] - 50) * 2.0 + (10 if stv.iloc[-1] > st_sig.iloc[-1] else -10), -100, 100))
        cci_sc = float(np.clip(float(calc_cci(close, high, low, C.CCI_PD).iloc[-1]) / 2.0, -100, 100))
        wpr_sc = float((float(calc_wpr(close, high, low, C.WPR_PD).iloc[-1]) + 50) * 2.0)
        adx_v, pdi, mdi = calc_adx(high, low, close, C.ADX_PD)
        adx_val = float(adx_v.iloc[-1]); pdi_v = float(pdi.iloc[-1]); mdi_v = float(mdi.iloc[-1])
        adx_sc = float(np.clip(adx_val * 2.0, 0, 100) if pdi_v > mdi_v else np.clip(-adx_val * 2.0, -100, 0))
        mom_pd = min(C.MOM_PD, len(close) - 1)
        mom_pct = float((close.iloc[-1] - close.iloc[-mom_pd]) / close.iloc[-mom_pd] * 100) if mom_pd > 0 and float(close.iloc[-mom_pd]) != 0 else 0.0
        mom_sc = float(np.clip(mom_pct * 10.0, -100, 100))

        wts = [C.RSI_W, C.MACD_W, C.STOCH_W, C.CCI_W, C.WPR_W, C.ADX_W, C.MOM_W]
        scs = [rsi_sc, macd_sc, stoch_sc, cci_sc, wpr_sc, adx_sc, mom_sc]
        tw = sum(wts)
        momentum = round(sum(s * w for s, w in zip(scs, wts)) / tw, 2) if tw > 0 else 0.0

        div = "NONE"
        try:
            if len(df) > C.DIV_BARS:
                rl_ = low.iloc[-C.DIV_BARS:]; rs_ = stv.iloc[-C.DIV_BARS:]
                if low.iloc[-1] < rl_.min() * 1.001 and stv.iloc[-1] > rs_.min():
                    div = "BULL"
                rh_ = high.iloc[-C.DIV_BARS:]
                if high.iloc[-1] > rh_.max() * 0.999 and stv.iloc[-1] < rs_.max():
                    div = "BEAR"
        except:
            pass

        va = float(volume.rolling(C.VOL_PD, min_periods=1).mean().iloc[-1])
        vr = round(float(volume.iloc[-1]) / va, 2) if va > 0 else 1.0
        obv = calc_obv(close, volume); obv_sma = sma(obv, 20)
        obv_bullish = bool(obv.iloc[-1] > obv_sma.iloc[-1])

        # MTF
        mtf = {}
        h_score = 50 + (rsi_v - 50) * 0.8
        mtf['1h'] = {'score': round(h_score, 1), 'summary': 'Buy' if h_score > 55 else ('Sell' if h_score < 45 else 'Neutral')}
        if df_daily is not None and len(df_daily) >= 20:
            dc = df_daily['close']; dh = df_daily['high']; dl = df_daily['low']
            d_rsi = float(calc_rsi(dc, 14).iloc[-1])
            d_ema20 = float(ema(dc, 20).iloc[-1])
            d_ema50 = float(ema(dc, 50).iloc[-1])
            d_score = float(50 + np.clip((d_rsi - 50) * 0.6 + (25 if float(dc.iloc[-1]) > d_ema20 else -25) + (15 if float(dc.iloc[-1]) > d_ema50 else -15), -50, 50))
            mtf['1D'] = {'score': round(d_score, 1), 'summary': 'Buy' if d_score > 55 else ('Sell' if d_score < 45 else 'Neutral')}
            if len(df_daily) >= 30:
                wc = dc.iloc[-5:]
                wt = float((wc.iloc[-1] - wc.iloc[0]) / wc.iloc[0] * 100)
                ws = float(50 + np.clip(wt * 10, -40, 40))
                mtf['1W'] = {'score': round(ws, 1), 'summary': 'Buy' if ws > 55 else ('Sell' if ws < 45 else 'Neutral')}
            if len(close) >= 80:
                h4c = close.iloc[::4]
                h4r = float(calc_rsi(h4c, 14).iloc[-1]) if len(h4c) >= 15 else 50.0
                h4s = float(50 + (h4r - 50) * 0.7)
                mtf['4h'] = {'score': round(h4s, 1), 'summary': 'Buy' if h4s > 55 else ('Sell' if h4s < 45 else 'Neutral')}
        for tf in ['1h', '4h', '1D', '1W']:
            if tf not in mtf:
                mtf[tf] = {'score': 50.0, 'summary': 'Neutral'}

        mtf_buy = sum(1 for v in mtf.values() if v['score'] > 55)
        mtf_sell = sum(1 for v in mtf.values() if v['score'] < 45)
        if mtf_buy >= 3:      mtf_align = "★ ALL BUY"
        elif mtf_sell >= 3:   mtf_align = "★ ALL SELL"
        elif mtf_buy >= 2:    mtf_align = "↗ Most Buy"
        elif mtf_sell >= 2:   mtf_align = "↘ Most Sell"
        else:                 mtf_align = "Mixed"
        htf_score = float(mtf.get('1D', {}).get('score', 50))
        ema_golden = bool(ema20 > ema50)
        trend_label = "UPTREND" if ema_golden and price > ema20 else ("DOWNTREND" if not ema_golden and price < ema20 else "SIDEWAYS")

        # Signal Strength
        am = abs(momentum); sig_str = 0.0
        sig_str += 20 if am >= C.SIG_TH else (12 if am >= C.CHG_TH else 5)
        sig_str += min(am * 0.35, 15)
        if (momentum > 0 and conf_buy) or (momentum < 0 and conf_sell): sig_str += 20
        if (momentum > 0 and bull_zn >= 2) or (momentum < 0 and bear_zn >= 2): sig_str += 10
        if (momentum > 0 and htf_score > 55) or (momentum < 0 and htf_score < 45): sig_str += 12
        if div == "BULL" and momentum > 0: sig_str += 8
        if div == "BEAR" and momentum < 0: sig_str += 8
        if vr > 1.5: sig_str += 10
        elif vr > 1.2: sig_str += 5
        if (momentum > 0 and obv_bullish) or (momentum < 0 and not obv_bullish): sig_str += 5
        if (momentum > 0 and ema_golden) or (momentum < 0 and not ema_golden): sig_str += 5
        sig_str = min(sig_str, 100)

        if momentum >= C.SIG_TH:     zone = "STRONG BUY"
        elif momentum >= C.CHG_TH:   zone = "BULLISH"
        elif momentum <= -C.SIG_TH:  zone = "STRONG SELL"
        elif momentum <= -C.CHG_TH:  zone = "BEARISH"
        else:                         zone = "NEUTRAL"

        session_name, in_session = get_market_session()

        # SIGNAL GENERATION
        signal = None; reasons = []; direction = None; sig_type = None
        st_val = float(stv.iloc[-1])
        bull_rev = st_val < 35 and momentum > -C.CHG_TH
        bear_rev = st_val > 65 and momentum < C.CHG_TH

        strong_buy = ((momentum >= C.SIG_TH) or bull_rev or conf_buy) and sig_str >= C.STRONG_STR
        strong_sell = ((momentum <= -C.SIG_TH) or bear_rev or conf_sell) and sig_str >= C.STRONG_STR
        c_buy = conf_buy and not strong_buy
        c_sell = conf_sell and not strong_sell
        w_buy = (momentum >= C.CHG_TH or bull_rev or (bull_zn >= 2 and above_ma)) and not strong_buy and not c_buy
        w_sell = (momentum <= -C.CHG_TH or bear_rev or (bear_zn >= 2 and not above_ma)) and not strong_sell and not c_sell

        if strong_buy:   direction = "BUY";  sig_type = "STRONG"; reasons.append("🔥 Strong Buy สัญญาณซื้อแรง")
        elif strong_sell: direction = "SELL"; sig_type = "STRONG"; reasons.append("🔥 Strong Sell สัญญาณขายแรง")
        elif c_buy:      direction = "BUY";  sig_type = "CONF";   reasons.append(f"◆ Confluence ซื้อ ({bx}/3)")
        elif c_sell:     direction = "SELL"; sig_type = "CONF";   reasons.append(f"◆ Confluence ขาย ({sx}/3)")
        elif w_buy:      direction = "BUY";  sig_type = "WEAK";   reasons.append("◇ Weak Buy สัญญาณซื้อเบา")
        elif w_sell:     direction = "SELL"; sig_type = "WEAK";   reasons.append("◇ Weak Sell สัญญาณขายเบา")

        if direction:
            if above_ma and direction == "BUY":           reasons.append("✅ เหนือ EMA50")
            if not above_ma and direction == "SELL":      reasons.append("✅ ต่ำกว่า EMA50")
            if ema_golden and direction == "BUY":         reasons.append("✅ Golden Cross")
            if not ema_golden and direction == "SELL":    reasons.append("✅ Death Cross")
            if bull_zn >= 2 and direction == "BUY":       reasons.append(f"✅ Bull Zone {bull_zn}/3")
            if bear_zn >= 2 and direction == "SELL":      reasons.append(f"✅ Bear Zone {bear_zn}/3")
            if div != "NONE":                             reasons.append(f"✅ {div} Divergence")
            if vr > 1.5:                                  reasons.append(f"✅ Vol {vr:.1f}x")
            if obv_bullish and direction == "BUY":        reasons.append("✅ OBV ขาขึ้น")
            if not obv_bullish and direction == "SELL":   reasons.append("✅ OBV ขาลง")
            if in_session:                                reasons.append(f"✅ {session_name}")

            if direction == "BUY":
                sl_ = round(price - atr_v * C.ATR_SL, dec); risk = price - sl_
                tp1_ = round(price + risk * 1.5, dec)
                tp2_ = round(price + risk * C.RR, dec)
                tp3_ = round(price + risk * 3.0, dec)
            else:
                sl_ = round(price + atr_v * C.ATR_SL, dec); risk = sl_ - price
                tp1_ = round(price - risk * 1.5, dec)
                tp2_ = round(price - risk * C.RR, dec)
                tp3_ = round(price - risk * 3.0, dec)
            risk_pct = round(risk / price * 100, 2) if price > 0 else 0.0
            rr = round(abs(price - tp2_) / max(risk, 1e-10), 2)

            signal = {
                'id': f"{symbol}_{direction}_{now().strftime('%H%M%S')}",
                'symbol': symbol, 'sector': sector, 'direction': direction, 'type': sig_type,
                'entry': round(price, dec), 'sl': float(sl_),
                'tp1': float(tp1_), 'tp2': float(tp2_), 'tp3': float(tp3_),
                'momentum': float(momentum), 'strength': round(sig_str),
                'conf': int(bx if direction == "BUY" else sx),
                'srK': round(sr_k, 1), 'srD': round(sr_d, 1),
                'enK': round(en_k, 1), 'enD': round(en_d, 1),
                'mnA': round(mn_a, 1), 'msA': round(ms_a, 1),
                'rsi_v': round(rsi_v, 1),
                'rsi_sc': round(rsi_sc, 1), 'macd_sc': round(macd_sc, 1),
                'stoch_sc': round(stoch_sc, 1), 'cci_sc': round(cci_sc, 1),
                'wpr_sc': round(wpr_sc, 1), 'adx_sc': round(adx_sc, 1),
                'mom_sc': round(mom_sc, 1),
                'trend': trend_label, 'bull_zn': int(bull_zn), 'bear_zn': int(bear_zn),
                'div': div, 'vol': float(vr), 'htf': round(htf_score, 1),
                'mtf_1h': mtf.get('1h', {}).get('summary', '?'),
                'mtf_4h': mtf.get('4h', {}).get('summary', '?'),
                'mtf_1d': mtf.get('1D', {}).get('summary', '?'),
                'mtf_1w': mtf.get('1W', {}).get('summary', '?'),
                'mtf_align': mtf_align, 'session': session_name,
                'atr': round(atr_v, dec + 2),
                'risk_pct': float(risk_pct), 'rr': float(rr),
                'reasons': reasons,
                'timestamp': dts(), 'ts_unix': time.time(),
            }

        analysis = {
            'symbol': symbol, 'cat': sector, 'price': round(price, dec),
            'prev': round(prev, dec), 'change': round(chg, dec + 1),
            'change_pct': round(chg_pct, 3),
            'momentum': float(momentum), 'strength': round(sig_str), 'zone': zone,
            'session': session_name, 'trend': trend_label,
            'srK': round(sr_k, 1), 'enK': round(en_k, 1), 'mnA': round(mn_a, 1),
            'bull_zn': int(bull_zn), 'bear_zn': int(bear_zn),
            'bx': int(bx), 'sx': int(sx),
            'conf_buy': bool(conf_buy), 'conf_sell': bool(conf_sell),
            'above_ma': bool(above_ma),
            'ema20': round(ema20, dec), 'ema50': round(ema50, dec),
            'rsi_v': round(rsi_v, 1), 'adx_v': round(adx_val, 1),
            'rsi_sc': round(rsi_sc, 1), 'macd_sc': round(macd_sc, 1),
            'stoch_sc': round(stoch_sc, 1), 'cci_sc': round(cci_sc, 1),
            'wpr_sc': round(wpr_sc, 1), 'adx_sc': round(adx_sc, 1),
            'mom_sc': round(mom_sc, 1),
            'div': div, 'vol': float(vr), 'atr': round(atr_v, dec + 2),
            'obv_bull': bool(obv_bullish),
            'mtf': mtf, 'mtf_align': mtf_align, 'time': ts(),
        }
        return analysis, signal
    except Exception as e:
        logger.error(f"Analyze {symbol}: {e}\n{traceback.format_exc()}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  STORE & AUTH
# ═══════════════════════════════════════════════════════════════════════════════
class Store:
    def __init__(self):
        self.prices = {}; self.signals = {}; self.analysis = {}; self.history = []
        self.last_scan = "Never"; self.scan_count = 0; self.connected = 0
        self.status = "STARTING"; self.errors = []; self.first_scan_done = False
        self.stats = {'buy': 0, 'sell': 0, 'strong': 0, 'conf': 0, 'weak': 0}
        self.dead_tickers = []
        self.ok_count = 0; self.fail_count = 0

    def err(self, m):
        self.errors.insert(0, f"[{ts()}] {m}")
        self.errors = self.errors[:50]

    def get_state(self):
        return sanitize({
            'prices': self.prices, 'signals': self.signals,
            'analysis': self.analysis, 'history': self.history[:100],
            'scan_count': self.scan_count, 'last_scan': self.last_scan,
            'status': self.status, 'stats': self.stats,
            'dead_tickers': self.dead_tickers,
            'ok_count': self.ok_count, 'fail_count': self.fail_count,
        })

store = Store()

class Users:
    def __init__(self):
        self.f = 'users.json'; self.u = self._load()

    def _load(self):
        try:
            if os.path.exists(self.f):
                with open(self.f) as f:
                    return json.load(f)
        except:
            pass
        d = {'admin': {'pw': hashlib.sha256(b'admin123_tcsm').hexdigest(),
                       'role': 'admin', 'name': 'Admin', 'on': True}}
        self._save(d); return d

    def _save(self, u=None):
        try:
            with open(self.f, 'w') as f:
                json.dump(u or self.u, f, indent=2)
        except:
            pass

    def verify(self, u, p):
        if u not in self.u: return False
        return self.u[u].get('on', True) and self.u[u]['pw'] == hashlib.sha256(f"{p}_tcsm".encode()).hexdigest()

users = Users()

def login_req(f):
    @wraps(f)
    def d(*a, **k):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*a, **k)
    return d


# ═══════════════════════════════════════════════════════════════════════════════
#  SCANNER — Runs inside Flask app context to prevent RuntimeError
# ═══════════════════════════════════════════════════════════════════════════════
def scanner():
    """Background scanner — wrapped in app.app_context() to fix context errors"""
    eventlet.sleep(8)
    logger.info("🚀 TCSM Thai Stock Pro v4.0 Scanner started (production-grade)")
    store.status = "RUNNING"

    with app.app_context():
        while True:
            try:
                store.scan_count += 1
                store.last_scan = ts()
                t0 = time.time(); ok = 0; fail = 0
                stocks = [s for s in C.STOCKS if not yfc.is_dead(s)]
                store.dead_tickers = list(yfc._dead_tickers)

                for i in range(0, len(stocks), C.BATCH_SIZE):
                    batch = stocks[i:i+C.BATCH_SIZE]
                    for symbol in batch:
                        try:
                            result = analyze_stock(symbol)
                            if result:
                                analysis, signal = result; ok += 1
                                analysis = sanitize(analysis)
                                store.analysis[symbol] = analysis
                                store.prices[symbol] = {
                                    'symbol': symbol, 'cat': analysis['cat'],
                                    'price': analysis['price'],
                                    'change': analysis['change'],
                                    'change_pct': analysis['change_pct'],
                                    'time': analysis['time']
                                }
                                safe_emit('price_update', {
                                    'symbol': symbol,
                                    'data': store.prices[symbol],
                                    'analysis': analysis
                                })
                                if signal:
                                    signal = sanitize(signal)
                                    old = store.signals.get(symbol)
                                    is_new = (not old
                                              or old.get('direction') != signal['direction']
                                              or time.time() - old.get('ts_unix', 0) > 1800)
                                    if is_new:
                                        store.signals[symbol] = signal
                                        store.history.insert(0, signal)
                                        store.history = store.history[:500]
                                        if signal['direction'] == "BUY":
                                            store.stats['buy'] += 1
                                        else:
                                            store.stats['sell'] += 1
                                        st = signal['type'].lower()
                                        store.stats[st] = store.stats.get(st, 0) + 1
                                        safe_emit('new_signal', {'symbol': symbol, 'signal': signal})
                                        logger.info(f"🎯 {signal['type']} {signal['direction']} {symbol} @ ฿{signal['entry']} Str:{signal['strength']}%")
                            else:
                                fail += 1
                        except Exception as e:
                            fail += 1; store.err(f"{symbol}: {e}")
                        eventlet.sleep(0.8)
                    eventlet.sleep(2)

                dur = time.time() - t0
                store.first_scan_done = True
                store.ok_count = ok; store.fail_count = fail
                store.dead_tickers = list(yfc._dead_tickers)

                safe_emit('scan_update', sanitize({
                    'scan_count': store.scan_count, 'last_scan': store.last_scan,
                    'stats': store.stats, 'status': store.status,
                    'ok': ok, 'fail': fail, 'dur': round(dur, 1),
                    'signals': len(store.signals),
                    'dead': len(yfc._dead_tickers)
                }))
                safe_emit('full_sync', store.get_state())
                logger.info(f"✅ Scan #{store.scan_count}: {ok}/{ok+fail} ({dur:.1f}s) | "
                            f"Signals: {len(store.signals)} | Dead: {len(yfc._dead_tickers)} | "
                            f"Clients: {store.connected}")
            except Exception as e:
                logger.error(f"Scanner: {e}\n{traceback.format_exc()}")
            eventlet.sleep(C.SCAN_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════════
LOGIN_HTML = '''<!DOCTYPE html><html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TCSM Thai Stock Pro v4.0</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
@keyframes gradient{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
body{background:linear-gradient(-45deg,#020617,#0f172a,#020617,#1e1b4b);background-size:400% 400%;animation:gradient 20s ease infinite;overflow:hidden}
.card{background:rgba(15,23,42,.7);backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.06);box-shadow:0 25px 50px -12px rgba(0,0,0,.5)}
.gb{position:relative;overflow:hidden;border-radius:12px;transition:all .3s}.gb:hover{transform:translateY(-1px);box-shadow:0 8px 30px rgba(234,179,8,.25)}
.gb::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:conic-gradient(from 0deg,transparent,rgba(234,179,8,.4),transparent 30%);animation:spin 3s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.gb>span{position:relative;z-index:1;display:block;padding:14px;background:linear-gradient(135deg,#92400e,#d97706);border-radius:11px;font-weight:700;letter-spacing:.5px}
.input-glow:focus{box-shadow:0 0 0 3px rgba(234,179,8,.15),0 0 20px rgba(234,179,8,.08)}
.particles{position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;overflow:hidden}
.particle{position:absolute;width:2px;height:2px;background:rgba(234,179,8,.3);border-radius:50%;animation:drift 8s infinite}
@keyframes drift{0%{transform:translateY(100vh) scale(0);opacity:0}50%{opacity:1}100%{transform:translateY(-10vh) scale(1);opacity:0}}
</style></head>
<body class="min-h-screen flex items-center justify-center p-4">
<div class="particles">
<div class="particle" style="left:10%;animation-delay:0s;animation-duration:6s"></div>
<div class="particle" style="left:20%;animation-delay:1s;animation-duration:8s"></div>
<div class="particle" style="left:40%;animation-delay:2s;animation-duration:7s"></div>
<div class="particle" style="left:60%;animation-delay:0.5s;animation-duration:9s"></div>
<div class="particle" style="left:75%;animation-delay:3s;animation-duration:6.5s"></div>
<div class="particle" style="left:90%;animation-delay:1.5s;animation-duration:8.5s"></div>
</div>
<div class="card rounded-3xl w-full max-w-md p-10 relative z-10">
<div class="text-center mb-10">
<div class="text-7xl mb-5" style="animation:float 3s ease-in-out infinite">🇹🇭</div>
<h1 class="text-4xl font-black bg-clip-text text-transparent bg-gradient-to-r from-yellow-300 via-amber-400 to-orange-400 tracking-tight">TCSM Pro</h1>
<p class="text-gray-500 text-sm mt-2 font-medium tracking-wide">Thai Stock Scanner v4.0</p>
<div class="flex justify-center gap-2 mt-4">
<span class="text-[10px] bg-green-500/10 text-green-400 px-3 py-1 rounded-full border border-green-500/20 font-semibold">SET 70+</span>
<span class="text-[10px] bg-cyan-500/10 text-cyan-400 px-3 py-1 rounded-full border border-cyan-500/20 font-semibold">Dual Engine</span>
<span class="text-[10px] bg-violet-500/10 text-violet-400 px-3 py-1 rounded-full border border-violet-500/20 font-semibold">Real-Time</span>
</div></div>
{% with m=get_flashed_messages(with_categories=true) %}{% if m %}{% for c,msg in m %}
<div class="mb-5 p-4 rounded-xl text-sm backdrop-blur-sm {% if c=='error' %}bg-red-500/10 text-red-300 border border-red-500/20{% else %}bg-green-500/10 text-green-300 border border-green-500/20{% endif %}">{{msg}}</div>
{% endfor %}{% endif %}{% endwith %}
<form method="POST" class="space-y-6">
<div><label class="block text-gray-400 text-[11px] font-semibold mb-2 uppercase tracking-widest">Username</label>
<input type="text" name="u" required class="input-glow w-full px-5 py-3.5 bg-slate-900/60 border border-slate-700/40 rounded-xl text-white placeholder-slate-600 focus:border-amber-500/40 focus:outline-none transition-all" placeholder="ชื่อผู้ใช้"></div>
<div><label class="block text-gray-400 text-[11px] font-semibold mb-2 uppercase tracking-widest">Password</label>
<input type="password" name="p" required class="input-glow w-full px-5 py-3.5 bg-slate-900/60 border border-slate-700/40 rounded-xl text-white placeholder-slate-600 focus:border-amber-500/40 focus:outline-none transition-all" placeholder="รหัสผ่าน"></div>
<div class="gb"><span class="text-center text-white cursor-pointer"><button type="submit" class="w-full text-base">🔓 เข้าสู่ระบบ</button></span></div>
</form>
<p class="mt-8 text-center text-slate-600 text-xs">Default: admin / admin123</p>
</div></body></html>'''


DASH_HTML = '''<!DOCTYPE html><html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🇹🇭 TCSM Thai Stock Pro v4.0</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
*{scrollbar-width:thin;scrollbar-color:#1e293b transparent}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-thumb{background:#334155;border-radius:4px}
@keyframes pulse2{0%,100%{opacity:1}50%{opacity:.35}}.pulse2{animation:pulse2 1.5s infinite}
@keyframes glow{0%,100%{box-shadow:0 0 12px rgba(234,179,8,.15)}50%{box-shadow:0 0 30px rgba(234,179,8,.35)}}.glow{animation:glow 2.5s infinite}
@keyframes slideUp{from{transform:translateY(12px);opacity:0}to{transform:translateY(0);opacity:1}}.slideUp{animation:slideUp .3s ease-out}
@keyframes loading{0%{transform:translateX(-100%)}50%{transform:translateX(0%)}100%{transform:translateX(100%)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}.fadeIn{animation:fadeIn .4s}
.glass{background:rgba(15,23,42,.65);backdrop-filter:blur(16px);border:1px solid rgba(51,65,85,.3)}
.gc{background:rgba(15,23,42,.5);backdrop-filter:blur(8px);border:1px solid rgba(51,65,85,.25);transition:all .2s}
.gc:hover{border-color:rgba(234,179,8,.15);background:rgba(15,23,42,.7)}
.sc{background:rgba(15,23,42,.5);backdrop-filter:blur(8px);border:1px solid rgba(51,65,85,.2)}
.sb{border-left:3px solid #22c55e;background:linear-gradient(135deg,rgba(22,101,52,.06),transparent)}
.ss{border-left:3px solid #ef4444;background:linear-gradient(135deg,rgba(127,29,29,.06),transparent)}
.tag{display:inline-flex;align-items:center;padding:2px 8px;border-radius:6px;font-size:9px;font-weight:600;letter-spacing:.3px}
body{background:#020617;color:#e2e8f0}
.stat-card{position:relative;overflow:hidden}.stat-card::after{content:'';position:absolute;top:0;right:0;width:40px;height:40px;border-radius:0 0 0 40px;opacity:.05}
.strength-ring{width:48px;height:48px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px}
</style></head>
<body class="min-h-screen text-[13px]">
<div class="max-w-[1800px] mx-auto px-3 py-3">

<!-- HEADER -->
<div class="flex flex-wrap justify-between items-center mb-3 gap-2">
<div>
<div class="flex items-center gap-3">
<h1 class="text-lg md:text-xl font-black bg-clip-text text-transparent bg-gradient-to-r from-yellow-300 via-amber-400 to-orange-400">🇹🇭 TCSM Thai Stock Pro</h1>
<span class="tag bg-amber-500/10 text-amber-400 border border-amber-500/20">v4.0</span>
<span class="tag bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">PRODUCTION</span>
</div>
<p class="text-slate-600 text-[10px] mt-0.5 font-medium">Dual Engine • 7-Indicator SmartMomentum • MTF Analysis • Volume/OBV/Divergence</p>
</div>
<div class="flex items-center gap-3">
<div id="clk" class="glass px-4 py-2 rounded-xl font-mono text-sm font-bold text-amber-400 tracking-wider">--:--:--</div>
<span id="st" class="text-red-400 text-sm font-medium">● Offline</span>
<button onclick="doRefresh()" class="px-4 py-2 bg-blue-600/50 hover:bg-blue-500/70 rounded-xl text-xs transition font-semibold border border-blue-500/20">🔄 รีเฟรช</button>
<a href="/logout" class="px-4 py-2 bg-red-600/40 hover:bg-red-500/60 rounded-xl text-xs transition font-semibold border border-red-500/20">ออก</a>
</div></div>

<!-- STATS ROW -->
<div class="grid grid-cols-4 md:grid-cols-8 gap-2 mb-3">
<div class="sc stat-card rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">รอบสแกน</div><div id="scc" class="font-black text-xl mt-1">0</div></div>
<div class="sc stat-card rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">สัญญาณ</div><div id="si" class="font-black text-xl text-amber-400 mt-1">0</div></div>
<div class="sc stat-card rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">Strong</div><div id="st2" class="font-black text-xl text-amber-300 mt-1">0</div></div>
<div class="sc stat-card rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">ซื้อ</div><div id="bu" class="font-black text-xl text-emerald-400 mt-1">0</div></div>
<div class="sc stat-card rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">ขาย</div><div id="se" class="font-black text-xl text-red-400 mt-1">0</div></div>
<div class="sc stat-card rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">ช่วงเวลา</div><div id="ses" class="text-amber-400 font-bold text-[11px] mt-1.5">—</div></div>
<div class="sc stat-card rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">สแกนล่าสุด</div><div id="ls" class="font-mono text-xs mt-1.5">--:--</div></div>
<div class="sc stat-card rounded-xl p-3 text-center"><div class="text-slate-500 text-[10px] uppercase font-semibold tracking-wider">สถานะ</div><div id="ss2" class="text-cyan-400 font-bold text-xs mt-1.5">INIT</div></div>
</div>

<!-- ALERT BANNER -->
<div id="al" class="hidden mb-3"><div class="bg-gradient-to-r from-amber-950/50 to-orange-950/30 border border-amber-600/40 rounded-2xl p-5 glow">
<div class="flex items-center gap-4"><span class="text-4xl" id="ai">🎯</span>
<div class="flex-1"><div id="at" class="font-bold text-amber-200 text-base"></div><div id="ax" class="text-amber-300/60 text-sm mt-1"></div></div>
<button onclick="this.closest('#al').classList.add('hidden')" class="text-amber-400/40 hover:text-white text-2xl transition">✕</button></div></div></div>

<!-- MAIN GRID -->
<div class="grid grid-cols-1 xl:grid-cols-12 gap-3">

<!-- LEFT: STOCK LIST -->
<div class="xl:col-span-3 space-y-3"><div class="glass rounded-2xl p-3">
<div class="flex justify-between items-center mb-3">
<h2 class="font-bold text-sm flex items-center gap-2">💹 หุ้นไทย <span id="ps" class="text-[9px] text-slate-600 font-normal">กำลังโหลด</span></h2>
<input id="search" type="text" placeholder="ค้นหาหุ้น..." class="bg-slate-900/50 border border-slate-700/30 rounded-xl px-3 py-1.5 text-xs w-28 focus:outline-none focus:border-amber-500/40 transition-all" oninput="rP()">
</div>
<div class="flex flex-wrap gap-1 mb-3 text-[10px]">
<button onclick="fP('all')" class="px-2.5 py-1 rounded-lg pf bg-amber-600/80 transition font-semibold" data-c="all">ทั้งหมด</button>
<button onclick="fP('Energy')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Energy">พลังงาน</button>
<button onclick="fP('Banking')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Banking">ธนาคาร</button>
<button onclick="fP('Tech')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Tech">เทคโนฯ</button>
<button onclick="fP('Property')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Property">อสังหา</button>
<button onclick="fP('Health')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Health">สุขภาพ</button>
<button onclick="fP('Consumer')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Consumer">อุปโภค</button>
<button onclick="fP('Transport')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Transport">ขนส่ง</button>
<button onclick="fP('Finance')" class="px-2.5 py-1 rounded-lg pf bg-slate-700/60 transition font-semibold" data-c="Finance">การเงิน</button>
</div>
<div id="pr" class="space-y-1.5 max-h-[620px] overflow-y-auto pr-1"></div>
</div></div>

<!-- CENTER: SIGNALS -->
<div class="xl:col-span-6 space-y-3">
<div class="flex justify-between items-center">
<h2 class="font-bold text-sm">🎯 สัญญาณซื้อขาย</h2>
<div class="flex gap-1 text-[10px]">
<button onclick="fS('all')" class="px-2.5 py-1 rounded-lg sf bg-amber-600/80 transition font-semibold" data-c="all">ทั้งหมด</button>
<button onclick="fS('STRONG')" class="px-2.5 py-1 rounded-lg sf bg-slate-700/60 transition font-semibold" data-c="STRONG">🔥 แรง</button>
<button onclick="fS('BUY')" class="px-2.5 py-1 rounded-lg sf bg-slate-700/60 transition font-semibold" data-c="BUY">🟢 ซื้อ</button>
<button onclick="fS('SELL')" class="px-2.5 py-1 rounded-lg sf bg-slate-700/60 transition font-semibold" data-c="SELL">🔴 ขาย</button>
</div></div>
<div id="sg" class="space-y-2.5">
<div class="glass rounded-2xl p-10 text-center text-slate-500">
<div class="text-5xl mb-4">📡</div>
<div class="text-base font-semibold">กำลังสแกนหุ้นไทย SET...</div>
<div class="text-xs mt-2 text-slate-600">Dual Engine • MTF • Volume • OBV • Divergence</div>
<div class="mt-6 flex justify-center"><div class="w-56 h-1.5 bg-slate-800 rounded-full overflow-hidden"><div class="h-full bg-gradient-to-r from-amber-600 to-yellow-400 rounded-full" style="animation:loading 2s ease-in-out infinite;width:30%"></div></div></div>
</div></div></div>

<!-- RIGHT: PANELS -->
<div class="xl:col-span-3 space-y-3">
<div class="glass rounded-2xl p-3"><h3 class="font-bold text-[11px] text-amber-400 mb-2.5 flex items-center gap-1.5">📊 Top Momentum</h3>
<div id="tm" class="space-y-1 max-h-[200px] overflow-y-auto"></div></div>
<div class="glass rounded-2xl p-3"><h3 class="font-bold text-[11px] text-emerald-400 mb-2.5 flex items-center gap-1.5">📈 กลุ่มอุตสาหกรรม</h3>
<div id="hm" class="grid grid-cols-2 gap-1.5 text-[10px]"></div></div>
<div class="glass rounded-2xl p-3"><h3 class="font-bold text-[11px] text-violet-400 mb-2.5 flex items-center gap-1.5">📡 Live Log</h3>
<div id="lg" class="h-36 overflow-y-auto font-mono text-[9px] space-y-0.5 leading-relaxed"></div></div>
</div></div>

<!-- HISTORY TABLE -->
<div class="glass rounded-2xl overflow-hidden mt-3">
<div class="flex justify-between items-center p-4 border-b border-slate-800/40">
<h2 class="font-bold text-sm">📜 ประวัติสัญญาณ</h2>
<span class="text-[10px] text-slate-500 font-medium" id="hc">0</span></div>
<div class="overflow-x-auto">
<table class="w-full text-[11px]">
<thead class="bg-slate-900/50"><tr class="text-slate-500 uppercase text-[9px] tracking-wider font-semibold">
<th class="px-3 py-2.5 text-left">เวลา</th><th class="px-3 py-2.5">หุ้น</th><th class="px-3 py-2.5">ประเภท</th><th class="px-3 py-2.5">ทิศทาง</th>
<th class="px-3 py-2.5 text-right">Entry</th><th class="px-3 py-2.5 text-right">SL</th>
<th class="px-3 py-2.5 text-right">TP1</th><th class="px-3 py-2.5 text-right">TP2</th>
<th class="px-3 py-2.5">R:R</th><th class="px-3 py-2.5">Str%</th><th class="px-3 py-2.5">Mom</th><th class="px-3 py-2.5">MTF</th>
</tr></thead>
<tbody id="ht"><tr><td colspan="12" class="py-8 text-center text-slate-600">รอสัญญาณ...</td></tr></tbody>
</table></div></div>

<div class="mt-4 text-center text-slate-700 text-[9px] pb-6">⚠️ เพื่อการศึกษาเท่านั้น ไม่ใช่คำแนะนำการลงทุน | TCSM Thai Stock Pro v4.0 Production</div>
</div>

<script>
const P={},A={},S={},H=[];
let cn=false,pf_='all',sf_='all';
const so=io({transports:['websocket','polling'],reconnection:true,reconnectionDelay:1000,reconnectionDelayMax:5000,reconnectionAttempts:Infinity,timeout:30000,forceNew:false});

function uc(){
const d=new Date(new Date().toLocaleString("en-US",{timeZone:"Asia/Bangkok"}));
document.getElementById('clk').textContent=d.toLocaleTimeString('en-GB',{hour12:false})+' ICT';
const h=d.getHours(),m=d.getMinutes(),wd=d.getDay(),t=h*60+m;let s='ปิดตลาด 🔒';
if(wd===0||wd===6)s='วันหยุด 🔒';else if(t<570)s='ก่อนเปิดตลาด';else if(t<600)s='เปิดจับคู่ ☀️';
else if(t<660)s='เช้า ⭐⭐';else if(t<750)s='เช้า ⭐';else if(t<870)s='พักเที่ยง 🍜';
else if(t<930)s='บ่าย ⭐';else if(t<990)s='ช่วงปิด ⭐⭐';else if(t<1020)s='ปิดจับคู่';
document.getElementById('ses').textContent=s;
}
setInterval(uc,1000);uc();

function lo(m,t='info'){
const e=document.getElementById('lg');
const c={signal:'text-emerald-400',error:'text-red-400',info:'text-slate-400',sys:'text-violet-400',warn:'text-amber-400'};
const d=document.createElement('div');d.className=(c[t]||'text-slate-400')+' leading-snug';
d.textContent=`[${new Date().toLocaleTimeString('en-GB',{timeZone:'Asia/Bangkok',hour12:false})}] ${m}`;
e.insertBefore(d,e.firstChild);while(e.children.length>80)e.removeChild(e.lastChild);
}

function fP(c){pf_=c;document.querySelectorAll('.pf').forEach(b=>{b.classList.toggle('bg-amber-600/80',b.dataset.c===c);b.classList.toggle('bg-slate-700/60',b.dataset.c!==c)});rP();}
function fS(c){sf_=c;document.querySelectorAll('.sf').forEach(b=>{b.classList.toggle('bg-amber-600/80',b.dataset.c===c);b.classList.toggle('bg-slate-700/60',b.dataset.c!==c)});rS();}
function sf(v,d=0){return typeof v==='number'&&isFinite(v)?v:d;}
function fp(p){if(p<1)return p.toFixed(4);if(p<10)return p.toFixed(3);return p.toFixed(2);}
function zc(z){return z.includes('BUY')?'text-emerald-400':z.includes('SELL')?'text-red-400':z.includes('BULL')?'text-emerald-500/80':z.includes('BEAR')?'text-red-500/80':'text-slate-500';}

function rP(){
let h='',n=0;const q=(document.getElementById('search')?.value||'').toUpperCase();
const syms=Object.keys(A).sort((a,b)=>Math.abs(sf(A[b]?.momentum))-Math.abs(sf(A[a]?.momentum)));
for(const s of syms){const a=A[s];if(!a)continue;if(pf_!=='all'&&a.cat!==pf_)continue;if(q&&!s.includes(q))continue;n++;
const cc=sf(a.change_pct)>=0?'text-emerald-400':'text-red-400';
const mc=sf(a.momentum)>=0?'text-emerald-400':'text-red-400';
const hs=S[s]?`<span class="w-2 h-2 rounded-full inline-block ${S[s].direction==='BUY'?'bg-emerald-400':'bg-red-400'} pulse2"></span>`:'';
const bw=Math.min(Math.abs(sf(a.momentum)),100);
h+=`<div class="gc rounded-xl p-2.5 ${S[s]?'border-amber-600/20':''}"><div class="flex justify-between items-center">
<div class="flex items-center gap-1.5">${hs}<span class="font-bold text-sm">${s}</span><span class="text-[9px] px-1.5 py-0.5 rounded-md bg-slate-800/50 text-slate-500 font-medium">${a.cat||''}</span></div>
<div class="text-right"><span class="font-bold">฿${fp(sf(a.price))}</span> <span class="${cc} text-[10px] font-semibold">${sf(a.change_pct)>=0?'+':''}${sf(a.change_pct).toFixed(2)}%</span></div></div>
<div class="flex justify-between mt-1.5 text-[9px]"><span class="${mc} font-semibold">Mom:${sf(a.momentum).toFixed(1)}</span>
<span class="${zc(a.zone||'')} font-semibold">${a.zone||''}</span><span class="text-slate-400">Str:${sf(a.strength)}%</span><span class="text-slate-600">${a.trend||''}</span></div>
<div class="mt-1.5 bg-slate-800/40 rounded-full h-1 overflow-hidden"><div class="${sf(a.momentum)>=0?'bg-gradient-to-r from-emerald-600 to-emerald-400':'bg-gradient-to-r from-red-600 to-red-400'} h-full rounded-full transition-all duration-500" style="width:${bw}%"></div></div></div>`;
}
if(!n)h='<div class="text-slate-600 text-center py-8 text-[11px]">กำลังสแกน...</div>';
document.getElementById('pr').innerHTML=h;
document.getElementById('ps').innerHTML=n>0?`<span class="text-emerald-400 pulse2">● ${n} ตัว</span>`:'กำลังโหลด';
}

function rT(){
const arr=Object.values(A).sort((a,b)=>Math.abs(sf(b.momentum))-Math.abs(sf(a.momentum))).slice(0,10);
document.getElementById('tm').innerHTML=arr.map(a=>{const mc=sf(a.momentum)>=0?'text-emerald-400':'text-red-400';
return`<div class="flex justify-between items-center py-1.5 border-b border-slate-800/20"><span class="font-bold text-[11px]">${a.symbol}</span>
<span class="${mc} font-mono font-bold text-[11px]">${sf(a.momentum)>=0?'+':''}${sf(a.momentum).toFixed(1)}</span>
<span class="text-[9px] ${zc(a.zone||'')} font-medium">${a.zone||''}</span></div>`}).join('')||'<div class="text-slate-600 text-xs py-4 text-center">รอข้อมูล...</div>';
}

function rHM(){
const sectors={};
const sT={'Energy':'พลังงาน','Banking':'ธนาคาร','Tech':'เทคโนฯ','Property':'อสังหา','Health':'สุขภาพ','Consumer':'อุปโภค','Transport':'ขนส่ง','Industry':'อุตฯ','Finance':'การเงิน','Other':'อื่นๆ'};
Object.values(A).forEach(a=>{const cat=a.cat||'Other';if(!sectors[cat])sectors[cat]={sum:0,cnt:0,buy:0,sell:0};
sectors[cat].sum+=sf(a.momentum);sectors[cat].cnt++;if(S[a.symbol]){S[a.symbol].direction==='BUY'?sectors[cat].buy++:sectors[cat].sell++;}});
document.getElementById('hm').innerHTML=Object.entries(sectors).map(([k,v])=>{const avg=(v.sum/v.cnt).toFixed(1);
const c=avg>=0?'from-emerald-950/40 to-emerald-900/20 border-emerald-700/20 text-emerald-400':'from-red-950/40 to-red-900/20 border-red-700/20 text-red-400';
return`<div class="bg-gradient-to-br ${c} border rounded-xl p-2.5"><div class="font-bold text-[11px]">${sT[k]||k}</div>
<div class="text-xs font-mono font-bold">${avg>=0?'+':''}${avg}</div><div class="text-[9px] text-slate-500 mt-0.5">${v.cnt} ตัว • ${v.buy}B/${v.sell}S</div></div>`}).join('');
}

function rS(){
let list=Object.values(S).sort((a,b)=>sf(b.strength)-sf(a.strength));
if(sf_==='STRONG')list=list.filter(s=>s.type==='STRONG');
else if(sf_==='BUY')list=list.filter(s=>s.direction==='BUY');
else if(sf_==='SELL')list=list.filter(s=>s.direction==='SELL');
if(!list.length){document.getElementById('sg').innerHTML=`<div class="glass rounded-2xl p-10 text-center text-slate-500"><div class="text-4xl mb-3">📡</div><div class="text-sm font-medium">${Object.keys(S).length?'ไม่มีสัญญาณตรงตัวกรอง':'กำลังสแกน...'}</div></div>`;document.getElementById('si').textContent=Object.keys(S).length;return;}
document.getElementById('si').textContent=Object.keys(S).length;
document.getElementById('sg').innerHTML=list.slice(0,20).map(s=>{const ib=s.direction==='BUY';const dc=ib?'text-emerald-400':'text-red-400';
const cls=ib?'sb':'ss';
const tc=s.type==='STRONG'?'bg-gradient-to-r from-amber-500 to-orange-500':s.type==='CONF'?'bg-gradient-to-r from-cyan-500 to-blue-500':'bg-slate-600';
const sw=Math.min(sf(s.strength),100);
const scc=sw>=75?'bg-gradient-to-r from-emerald-500 to-emerald-400':sw>=50?'bg-gradient-to-r from-amber-500 to-yellow-400':'bg-slate-600';
const dT=ib?'ซื้อ BUY':'ขาย SELL';
const strColor=sw>=75?'text-emerald-400 border-emerald-500/30 bg-emerald-500/10':sw>=50?'text-amber-400 border-amber-500/30 bg-amber-500/10':'text-slate-400 border-slate-500/30 bg-slate-500/10';

return`<div class="glass ${cls} rounded-2xl p-4 slideUp"><div class="flex justify-between items-start mb-3"><div>
<div class="flex items-center gap-2">
<span class="text-lg font-black ${dc}">${ib?'🟢':'🔴'} ${s.symbol}</span>
<span class="px-2.5 py-0.5 rounded-lg text-[10px] font-bold text-white ${tc} shadow-sm">${s.type} ${dT}</span>
<span class="tag bg-slate-800/60 text-slate-400 border border-slate-700/30">${s.sector||''}</span></div>
<div class="text-[10px] text-slate-500 mt-1 space-x-2"><span>Mom:${sf(s.momentum).toFixed(1)}</span><span>•</span><span>Conf:${sf(s.conf)}/3</span><span>•</span><span>${s.trend||''}</span><span>•</span><span>RSI:${sf(s.rsi_v,'?')}</span></div></div>
<div class="text-right"><div class="strength-ring ${strColor} border text-[13px]">${sf(s.strength)}%</div>
<div class="text-[9px] text-slate-600 mt-1">${s.timestamp||''}</div></div></div>
<div class="grid grid-cols-5 gap-2 mb-3">
<div class="bg-slate-900/50 rounded-xl p-2.5 text-center border border-slate-800/30"><div class="text-[9px] text-slate-500 font-medium">จุดเข้า</div><div class="font-bold text-sm mt-0.5">฿${fp(sf(s.entry))}</div></div>
<div class="bg-red-950/30 rounded-xl p-2.5 text-center border border-red-800/20"><div class="text-[9px] text-red-400 font-medium">SL</div><div class="font-bold text-sm text-red-400 mt-0.5">฿${fp(sf(s.sl))}</div></div>
<div class="bg-emerald-950/20 rounded-xl p-2.5 text-center border border-emerald-800/20"><div class="text-[9px] text-emerald-400 font-medium">TP1</div><div class="font-bold text-sm text-emerald-400 mt-0.5">฿${fp(sf(s.tp1))}</div></div>
<div class="bg-emerald-950/25 rounded-xl p-2.5 text-center border border-emerald-700/25"><div class="text-[9px] text-emerald-300 font-medium">TP2</div><div class="font-bold text-sm text-emerald-300 mt-0.5">฿${fp(sf(s.tp2))}</div></div>
<div class="bg-emerald-950/30 rounded-xl p-2.5 text-center border border-emerald-600/30"><div class="text-[9px] text-emerald-200 font-medium">TP3</div><div class="font-bold text-sm text-emerald-200 mt-0.5">฿${fp(sf(s.tp3))}</div></div></div>
<div class="flex items-center gap-3 mb-2.5"><span class="text-[10px] text-slate-500 font-medium">ความแรง</span>
<div class="flex-1 bg-slate-800/40 rounded-full h-2 overflow-hidden"><div class="${scc} h-full rounded-full transition-all duration-500" style="width:${sw}%"></div></div>
<span class="text-slate-400 text-[10px]">R:R <b class="text-slate-200">${sf(s.rr)}:1</b></span>
<span class="text-slate-400 text-[10px]">Risk <b class="text-slate-200">${sf(s.risk_pct)}%</b></span>
<span class="text-[10px] font-bold ${(s.mtf_align||'').includes('BUY')?'text-emerald-400':(s.mtf_align||'').includes('SELL')?'text-red-400':'text-slate-500'}">${s.mtf_align||''}</span></div>
<div class="flex flex-wrap gap-1">${(s.reasons||[]).slice(0,10).map(r=>`<span class="tag bg-slate-800/60 text-slate-300 border border-slate-700/25">${r}</span>`).join('')}</div></div>`;}).join('');
}

function rH(){
if(!H.length){document.getElementById('ht').innerHTML='<tr><td colspan="12" class="py-8 text-center text-slate-600">รอสัญญาณ...</td></tr>';return;}
document.getElementById('hc').textContent=H.length+' สัญญาณ';
document.getElementById('ht').innerHTML=H.slice(0,80).map(s=>{
const dc=s.direction==='BUY'?'text-emerald-400 bg-emerald-500/10':'text-red-400 bg-red-500/10';
const tc=s.type==='STRONG'?'text-amber-400':s.type==='CONF'?'text-cyan-400':'text-slate-400';
const dT=s.direction==='BUY'?'ซื้อ':'ขาย';
return`<tr class="border-t border-slate-800/20 hover:bg-slate-800/15 transition-colors">
<td class="px-3 py-2 text-slate-500 text-[10px]">${s.timestamp||''}</td>
<td class="px-3 py-2 font-bold text-center">${s.symbol}</td>
<td class="px-3 py-2 text-center ${tc} font-bold">${s.type}</td>
<td class="px-3 py-2 text-center"><span class="px-2 py-0.5 rounded-md ${dc} font-bold text-[10px]">${dT}</span></td>
<td class="px-3 py-2 text-right font-mono">฿${fp(sf(s.entry))}</td>
<td class="px-3 py-2 text-right font-mono text-red-400">฿${fp(sf(s.sl))}</td>
<td class="px-3 py-2 text-right font-mono text-emerald-400">฿${fp(sf(s.tp1))}</td>
<td class="px-3 py-2 text-right font-mono text-emerald-300">฿${fp(sf(s.tp2))}</td>
<td class="px-3 py-2 font-bold text-center">${sf(s.rr)}:1</td>
<td class="px-3 py-2 text-center"><span class="px-1.5 py-0.5 rounded-md ${sf(s.strength)>=75?'bg-emerald-500/15 text-emerald-400':sf(s.strength)>=50?'bg-amber-500/15 text-amber-400':'bg-slate-700/50 text-slate-400'} font-bold">${sf(s.strength)}%</span></td>
<td class="px-3 py-2 text-center font-mono font-bold ${sf(s.momentum)>=0?'text-emerald-400':'text-red-400'}">${sf(s.momentum)>=0?'+':''}${sf(s.momentum).toFixed(1)}</td>
<td class="px-3 py-2 text-center text-[10px] font-bold ${(s.mtf_align||'').includes('BUY')?'text-emerald-400':(s.mtf_align||'').includes('SELL')?'text-red-400':'text-slate-500'}">${s.mtf_align||''}</td></tr>`}).join('');
}

function sA(s){
const dT=s.direction==='BUY'?'ซื้อ':'ขาย';
document.getElementById('ai').textContent=s.direction==='BUY'?'🟢':'🔴';
document.getElementById('at').textContent=`${s.type} ${dT} — ${s.symbol} @ ฿${fp(sf(s.entry))}`;
document.getElementById('ax').textContent=`SL:฿${fp(sf(s.sl))} TP2:฿${fp(sf(s.tp2))} Str:${sf(s.strength)}% Mom:${sf(s.momentum).toFixed(1)}`;
document.getElementById('al').classList.remove('hidden');
try{const c=new(window.AudioContext||window.webkitAudioContext)();const o=c.createOscillator();const g=c.createGain();o.connect(g);g.connect(c.destination);o.frequency.value=s.direction==='BUY'?880:660;g.gain.value=0.06;o.start();o.stop(c.currentTime+0.12);}catch(e){}
setTimeout(()=>document.getElementById('al').classList.add('hidden'),12000);
}

function loadState(d){
if(!d)return;
if(d.prices)Object.assign(P,d.prices);
if(d.signals){for(const[k,v]of Object.entries(d.signals)){S[k]=v;}}
if(d.analysis){for(const[k,v]of Object.entries(d.analysis)){A[k]=v;}}
if(d.history){const ids=new Set(H.map(h=>h.id));const ni=(d.history||[]).filter(h=>!ids.has(h.id));H.unshift(...ni);if(H.length===0&&d.history.length>0)H.push(...d.history);}
if(d.stats){document.getElementById('bu').textContent=d.stats.buy||0;document.getElementById('se').textContent=d.stats.sell||0;document.getElementById('st2').textContent=d.stats.strong||0;}
document.getElementById('scc').textContent=d.scan_count||0;
rP();rS();rH();rT();rHM();
}

function doRefresh(){
lo('รีเฟรช...','sys');
fetch('/api/signals').then(r=>r.json()).then(d=>{
if(d.signals){for(const[k,v]of Object.entries(d.signals)){S[k]=v;}}
if(d.history){H.length=0;H.push(...d.history);}
if(d.stats){document.getElementById('bu').textContent=d.stats.buy||0;document.getElementById('se').textContent=d.stats.sell||0;document.getElementById('st2').textContent=d.stats.strong||0;}
rS();rH();lo(`รีเฟรชแล้ว: ${Object.keys(S).length} สัญญาณ`,'sys');
}).catch(e=>lo('ล้มเหลว: '+e,'error'));
}

// Auto-sync if empty
setInterval(()=>{if(Object.keys(A).length===0){lo('Auto-sync...','sys');doRefresh();}},30000);

// Socket events
so.on('connect',()=>{cn=true;document.getElementById('st').innerHTML='<span class="text-emerald-400 pulse2 font-semibold">● LIVE</span>';lo('เชื่อมต่อแล้ว','sys');});
so.on('disconnect',()=>{cn=false;document.getElementById('st').innerHTML='<span class="text-red-400 font-semibold">● Offline</span>';lo('ขาดการเชื่อมต่อ','error');});
so.on('reconnect',()=>{lo('เชื่อมต่อใหม่','sys');so.emit('request_state');});
so.on('init',(d)=>{lo(`Init: ${Object.keys(d.analysis||{}).length} หุ้น`,'sys');loadState(d);fP('all');});
so.on('full_sync',(d)=>{lo(`Sync: ${Object.keys(d.analysis||{}).length} หุ้น, ${Object.keys(d.signals||{}).length} สัญญาณ`,'sys');loadState(d);});
so.on('price_update',(d)=>{if(!d?.symbol)return;P[d.symbol]=d.data;if(d.analysis)A[d.symbol]=d.analysis;rP();rT();rHM();});
so.on('new_signal',(d)=>{if(!d?.signal)return;S[d.symbol]=d.signal;if(!H.find(h=>h.id===d.signal.id)){H.unshift(d.signal);}rS();rH();sA(d.signal);
const dT=d.signal.direction==='BUY'?'ซื้อ':'ขาย';lo(`🎯 ${d.signal.type} ${dT} ${d.symbol} @฿${fp(sf(d.signal.entry))} Str:${sf(d.signal.strength)}%`,'signal');});
so.on('scan_update',(d)=>{if(!d)return;document.getElementById('scc').textContent=d.scan_count||0;document.getElementById('ls').textContent=d.last_scan||'--';document.getElementById('ss2').textContent=d.status||'OK';
if(d.stats){document.getElementById('bu').textContent=d.stats.buy||0;document.getElementById('se').textContent=d.stats.sell||0;document.getElementById('st2').textContent=d.stats.strong||0;}});
</script></body></html>'''


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return redirect(url_for('login') if 'user' not in session else url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        u = request.form.get('u', '').strip().lower()
        p = request.form.get('p', '')
        if users.verify(u, p):
            session.permanent = True
            session['user'] = u
            return redirect(url_for('dashboard'))
        flash('ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง', 'error')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_req
def dashboard():
    return render_template_string(DASH_HTML)

@app.route('/health')
def health():
    return jsonify(sanitize({
        'status': 'ok', 'version': '4.0', 'time': dts(),
        'scans': store.scan_count, 'stocks': len(C.STOCKS),
        'active_signals': len(store.signals),
        'connected': store.connected,
        'first_scan_done': store.first_scan_done,
        'dead_tickers': list(yfc._dead_tickers),
        'ok': store.ok_count, 'fail': store.fail_count,
    }))

@app.route('/api/signals')
@login_req
def api_signals():
    return jsonify(sanitize({
        'signals': store.signals,
        'history': store.history[:100],
        'stats': store.stats
    }))

@app.route('/api/refresh')
@login_req
def api_refresh():
    return jsonify(store.get_state())


# ═══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET HANDLERS — with error protection
# ═══════════════════════════════════════════════════════════════════════════════
@socketio.on('connect')
def ws_conn():
    store.connected += 1
    try:
        emit('init', store.get_state())
    except Exception as e:
        logger.warning(f"WS init emit: {e}")

@socketio.on('disconnect')
def ws_disc():
    store.connected = max(0, store.connected - 1)

@socketio.on('request_state')
def ws_req():
    try:
        emit('full_sync', store.get_state())
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════════════════════
port = int(os.environ.get('PORT', 8000))
eventlet.spawn(scanner)

if __name__ == '__main__':
    print("=" * 65)
    print("  🇹🇭 TCSM THAI STOCK PRO v4.0 — PRODUCTION GRADE")
    print("  📊 Yahoo Finance: Direct API + yfinance + Auto-delist")
    print(f"  🕐 Time: {dts()} ICT | Stocks: {len(C.STOCKS)}")
    print(f"  🌐 Port: {port} | Login: admin / admin123")
    print("=" * 65)
    socketio.run(app, host='0.0.0.0', port=port, debug=False, use_reloader=False)
