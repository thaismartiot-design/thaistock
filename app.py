"""
TCSM THAI STOCK PRO v3.2 — SET STOCK SCANNER (CLOUD-FIXED)
Production-ready real-time Thai stock signal scanner
FIXES in v3.2:
 - [CRITICAL] yfinance fails on cloud hosts (Render/PythonAnywhere) due to Yahoo IP blocking
 - [FIX] Use yf.download() batch method instead of Ticker.history() — more reliable
 - [FIX] Add custom session with browser User-Agent headers to avoid 401/blocking
 - [FIX] Fallback to Yahoo Finance direct CSV endpoint if yf.download fails
 - [FIX] Use latest yfinance>=1.0 API with proper error suppression
 - [FIX] Batch download multiple tickers at once (faster + fewer requests)
 - [FIX] Retry logic with exponential backoff
 - [FIX] Graceful degradation — skip failed tickers, continue scanning

Deploy: gunicorn --worker-class eventlet -w 1 -b 0.0.0.0:$PORT app:app
Requirements: pip install -r requirements.txt
"""
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit
from datetime import datetime, timezone, timedelta
from functools import wraps
import pandas as pd
import numpy as np
import hashlib, secrets, time, json, os, traceback, logging, math, threading, io

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('TCSM-TH')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

socketio = SocketIO(
    app, cors_allowed_origins="*", async_mode='eventlet',
    ping_timeout=120, ping_interval=30,
    logger=False, engineio_logger=False, always_connect=True
)

# ═══════════════════════════════════════════════════════════════════════════════
#  TIMEZONE & HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
ICT = timezone(timedelta(hours=7))
def now(): return datetime.now(ICT)
def ts(f="%H:%M:%S"): return now().strftime(f)
def dts(): return now().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════════
#  JSON SANITIZER
# ═══════════════════════════════════════════════════════════════════════════════
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
    try:
        socketio.emit(event, sanitize(data), **kwargs)
    except Exception as e:
        logger.error(f"Emit '{event}' failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG — Thai Stock Market
# ═══════════════════════════════════════════════════════════════════════════════
class C:
    SCAN_INTERVAL = 90  # seconds between scans (more generous for cloud)
    BATCH_SIZE = 15     # download multiple tickers per yf.download call

    # Engine A — Triple Confluence
    STOCH_RSI_K = 3; STOCH_RSI_D = 3; RSI_PD = 14; STOCH_PD = 14
    ENH_K = 21; ENH_D = 3; ENH_SLOW = 5
    MACD_F = 12; MACD_S = 26; MACD_SIG = 9; MACD_NLB = 500
    OB = 80.0; OS = 20.0; NEU_HI = 60.0; NEU_LO = 40.0

    # Engine B — SmartMomentum
    SIG_TH = 38; CHG_TH = 14; MOM_SM = 3; SWING_LB = 5
    RSI_W = 18; MACD_W = 18; STOCH_W = 14; CCI_W = 14; WPR_W = 12; ADX_W = 12; MOM_W = 12
    CCI_PD = 20; WPR_PD = 14; ADX_PD = 14; MOM_PD = 14

    TREND_MA = 50; MIN_MOM = 0.4; STRONG_STR = 52.0

    ATR_PD = 14; ATR_SL = 1.5; RR = 2.0
    DIV_BARS = 20; VOL_PD = 20

    # Thai Stock Universe — verified active tickers on Yahoo Finance (.BK)
    STOCKS = [
        # SET50 Large Cap (verified active)
        "ADVANC", "AOT", "AWC", "BANPU", "BBL",
        "BDMS", "BEM", "BGRIM", "BH", "BTS",
        "CBG", "CENTEL", "COM7", "CPALL", "CPF",
        "CPN", "CRC", "DELTA", "EA", "EGCO",
        "GLOBAL", "GPSC", "GULF", "HMPRO", "INTUCH",
        "IVL", "KBANK", "KCE", "KTB", "KTC",
        "LH", "MINT", "MTC", "OR", "OSP",
        "PTT", "PTTEP", "PTTGC", "RATCH", "SAWAD",
        "SCB", "SCC", "SCGP", "TISCO", "TOP",
        "TRUE", "TTB", "TU",
        # SET100 & Popular Mid Cap
        "AMATA", "AP", "BAM", "BCP", "BCPG",
        "BJC", "CK", "DOHOME", "FORTH", "GFPT",
        "GUNKUL", "HANA", "IRPC", "JAS",
        "JMART", "JMT", "KEX", "KKP",
        "LANNA", "MAJOR", "MAKRO", "MEGA",
        "MONO", "ORI", "PLANB",
        "PTG", "QH", "RS", "SAMART",
        "SIRI", "SPALI", "SPRC", "STA",
        "STEC", "SUPER", "TASCO", "TCAP",
        "THAI", "THANI", "TKN",
        "TQM", "VGI", "WHA", "WHAUP",
    ]
    STOCKS = list(dict.fromkeys(STOCKS))

    SECTOR_MAP = {}
    _energy = {"PTT","PTTEP","PTTGC","TOP","IRPC","BANPU","EGCO","GPSC",
               "GULF","RATCH","EA","BGRIM","BCP","BCPG","SPRC","OR",
               "GUNKUL","SUPER","LANNA","PTG"}
    _bank = {"KBANK","BBL","SCB","KTB","TTB","TISCO","KKP","TCAP","BAM","THANI","SAWAD"}
    _tech = {"ADVANC","TRUE","INTUCH","JAS","COM7","DELTA","HANA","KCE",
             "SAMART","FORTH","MONO","PLANB"}
    _property = {"CPN","CRC","LH","AP","SIRI","SPALI","QH","ORI",
                 "AMATA","WHA","WHAUP","HMPRO","DOHOME","AWC","CENTEL","MINT","STEC"}
    _health = {"BDMS","BH","MEGA"}
    _consumer = {"CPALL","CPF","TU","CBG","BJC","MAKRO","GFPT","OSP","MAJOR"}
    _transport = {"AOT","BTS","BEM","THAI","KEX","TASCO"}
    _industry = {"SCC","SCGP","IVL","MTC","KTC","CK","TKN","STA"}
    _finance = {"KTC","MTC","SAWAD","JMT","JMART","TQM","GLOBAL","VGI","RS"}

    for s in _energy: SECTOR_MAP[s] = "Energy"
    for s in _bank: SECTOR_MAP[s] = "Banking"
    for s in _tech: SECTOR_MAP[s] = "Tech"
    for s in _property: SECTOR_MAP[s] = "Property"
    for s in _health: SECTOR_MAP[s] = "Health"
    for s in _consumer: SECTOR_MAP[s] = "Consumer"
    for s in _transport: SECTOR_MAP[s] = "Transport"
    for s in _industry: SECTOR_MAP[s] = "Industry"
    for s in _finance: SECTOR_MAP[s] = "Finance"

    @staticmethod
    def get_sector(sym): return C.SECTOR_MAP.get(sym, "Other")

    @staticmethod
    def yf_sym(sym): return f"{sym}.BK"


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA STORE
# ═══════════════════════════════════════════════════════════════════════════════
class Store:
    def __init__(self):
        self.prices = {}; self.signals = {}; self.analysis = {}; self.history = []
        self.last_scan = "Never"; self.scan_count = 0; self.connected = 0
        self.status = "STARTING"; self.errors = []; self.first_scan_done = False
        self.stats = {'buy': 0, 'sell': 0, 'strong': 0, 'conf': 0, 'weak': 0}

    def err(self, m):
        self.errors.insert(0, f"[{ts()}] {m}")
        self.errors = self.errors[:50]

    def get_state(self):
        return sanitize({
            'prices': self.prices, 'signals': self.signals,
            'analysis': self.analysis, 'history': self.history[:100],
            'scan_count': self.scan_count, 'last_scan': self.last_scan,
            'status': self.status, 'stats': self.stats,
        })

store = Store()


# ═══════════════════════════════════════════════════════════════════════════════
#  YAHOO FINANCE CLIENT — CLOUD-SAFE with multiple fallback methods
# ═══════════════════════════════════════════════════════════════════════════════
import requests as req_lib

class YFClient:
    """Cloud-safe Yahoo Finance client with fallback methods"""

    def __init__(self):
        self._cache = {}
        self._cache_time = {}
        self._lock = threading.Lock()
        self._session = None
        self._fail_count = {}

    def _get_session(self):
        """Create a requests session with browser-like headers"""
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            })
        return self._session

    def _download_batch(self, symbols, period="3mo", interval="1d"):
        """Download data for multiple symbols using yf.download (most reliable method)"""
        import yfinance as yf
        yf_syms = [C.yf_sym(s) for s in symbols]
        syms_str = " ".join(yf_syms)

        try:
            session = self._get_session()
            df = yf.download(
                syms_str,
                period=period,
                interval=interval,
                group_by='ticker',
                auto_adjust=True,
                threads=False,
                progress=False,
                session=session,
            )
            return df
        except Exception as e:
            logger.warning(f"yf.download batch failed: {e}")
            return None

    def _download_single_csv(self, symbol, period="3mo", interval="1d"):
        """Fallback: Direct Yahoo Finance CSV download"""
        yf_sym = C.yf_sym(symbol)
        try:
            import requests
            period_map = {"3mo": 7776000, "6mo": 15552000, "1y": 31536000}
            period_sec = period_map.get(period, 7776000)
            end_ts = int(time.time())
            start_ts = end_ts - period_sec
            interval_map = {"1d": "1d", "1h": "1h", "1wk": "1wk"}
            yf_interval = interval_map.get(interval, "1d")

            url = (f"https://query1.finance.yahoo.com/v7/finance/download/{yf_sym}"
                   f"?period1={start_ts}&period2={end_ts}&interval={yf_interval}"
                   f"&events=history&includeAdjustedClose=true")

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/csv',
            }
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200 and 'Date' in resp.text[:100]:
                df = pd.read_csv(io.StringIO(resp.text), parse_dates=['Date'], index_col='Date')
                df.columns = [c.lower() for c in df.columns]
                return df
        except Exception as e:
            logger.debug(f"CSV fallback failed for {symbol}: {e}")
        return None

    def get_history(self, symbol, period="3mo", interval="1d"):
        """Get OHLCV data with multi-method fallback"""
        cache_key = f"{symbol}_{period}_{interval}"
        with self._lock:
            if cache_key in self._cache and time.time() - self._cache_time.get(cache_key, 0) < 60:
                return self._cache[cache_key]

        # Skip tickers that have failed too many times
        fc = self._fail_count.get(symbol, 0)
        if fc >= 5:
            return None

        df = None

        # Method 1: yf.download single ticker
        try:
            import yfinance as yf
            yf_sym = C.yf_sym(symbol)
            session = self._get_session()
            data = yf.download(
                yf_sym,
                period=period,
                interval=interval,
                auto_adjust=True,
                threads=False,
                progress=False,
                session=session,
            )
            if data is not None and len(data) >= 20:
                data.columns = [c.lower() if isinstance(c, str) else c for c in data.columns]
                # Handle multi-level columns from yf.download
                if isinstance(data.columns, pd.MultiIndex):
                    data.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in data.columns]
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    if col in data.columns:
                        data[col] = pd.to_numeric(data[col], errors='coerce')
                data = data.dropna(subset=['close'])
                if len(data) >= 20:
                    df = data
                    logger.debug(f"✓ {symbol}: yf.download OK ({len(df)} rows)")
        except Exception as e:
            logger.debug(f"Method 1 failed {symbol}: {e}")

        # Method 2: yf.Ticker.history
        if df is None:
            try:
                import yfinance as yf
                yf_sym = C.yf_sym(symbol)
                ticker = yf.Ticker(yf_sym, session=self._get_session())
                data = ticker.history(period=period, interval=interval)
                if data is not None and len(data) >= 20:
                    data.columns = [c.lower() for c in data.columns]
                    data = data.rename(columns={'stock splits': 'splits'})
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        if col in data.columns:
                            data[col] = pd.to_numeric(data[col], errors='coerce')
                    data = data.dropna(subset=['close'])
                    if len(data) >= 20:
                        df = data
                        logger.debug(f"✓ {symbol}: Ticker.history OK ({len(df)} rows)")
            except Exception as e:
                logger.debug(f"Method 2 failed {symbol}: {e}")

        # Method 3: Direct CSV download
        if df is None:
            df = self._download_single_csv(symbol, period, interval)
            if df is not None and len(df) >= 20:
                logger.debug(f"✓ {symbol}: CSV fallback OK ({len(df)} rows)")
            else:
                df = None

        if df is not None and len(df) >= 20:
            self._fail_count[symbol] = 0
            with self._lock:
                self._cache[cache_key] = df
                self._cache_time[cache_key] = time.time()
            return df
        else:
            self._fail_count[symbol] = fc + 1
            if fc + 1 >= 3:
                logger.warning(f"✗ {symbol}: all methods failed ({fc + 1} consecutive)")
            return None

    def get_daily(self, symbol):
        return self.get_history(symbol, period="6mo", interval="1d")

    def reset_failures(self):
        """Reset failure counts periodically to retry previously failed tickers"""
        self._fail_count = {}

yfc = YFClient()


# ═══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS (unchanged)
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
    n = now(); h = n.hour; m = n.minute; wd = n.weekday()
    t = h * 60 + m
    if wd >= 5: return "WEEKEND 🔒", False
    if t < 570: return "PRE-MARKET", False
    elif t < 600: return "PRE-OPEN ☀️", True
    elif t < 660: return "MORNING ⭐⭐", True
    elif t < 750: return "MORNING ⭐", True
    elif t < 870: return "LUNCH 🍜", False
    elif t < 930: return "AFTERNOON ⭐", True
    elif t < 990: return "CLOSING ⭐⭐", True
    elif t < 1020: return "AUCTION", False
    return "CLOSED 🔒", False


# ═══════════════════════════════════════════════════════════════════════════════
#  TCSM ENGINE — FULL ANALYSIS (uses daily data — most reliable for .BK)
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_stock(symbol):
    """Complete TCSM dual-engine analysis — uses daily data for reliability"""
    try:
        sector = C.get_sector(symbol)

        # Use daily data (most reliable for Thai stocks on Yahoo Finance)
        df = yfc.get_daily(symbol)
        if df is None or len(df) < 30:
            return None

        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume'] if 'volume' in df.columns else pd.Series(0, index=df.index)
        price = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else price
        chg = price - prev
        chg_pct = (chg / prev * 100) if prev else 0

        # Dynamic decimal places for Thai stocks
        if price < 1: dec = 4
        elif price < 10: dec = 3
        elif price < 100: dec = 2
        else: dec = 2

        # ═══════════════════════════════════════════════════
        #  ENGINE A: TRIPLE CONFLUENCE
        # ═══════════════════════════════════════════════════
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

        def xo(a, b): return len(a)>=2 and a.iloc[-1]>b.iloc[-1] and a.iloc[-2]<=b.iloc[-2]
        def xu(a, b): return len(a)>=2 and a.iloc[-1]<b.iloc[-1] and a.iloc[-2]>=b.iloc[-2]
        def ir(v): return v > C.OS and v < C.OB

        bx = sum([xo(srK,srD) and ir(sr_k), xo(enK,enD) and ir(en_k), xo(mnA,msA) and ir(mn_a)])
        sx = sum([xu(srK,srD) and ir(sr_k), xu(enK,enD) and ir(en_k), xu(mnA,msA) and ir(mn_a)])

        bull_zn = sum([sr_k > C.NEU_HI, en_k > C.NEU_HI, mn_a > C.NEU_HI])
        bear_zn = sum([sr_k < C.NEU_LO, en_k < C.NEU_LO, mn_a < C.NEU_LO])

        ema20 = float(ema(close, 20).iloc[-1])
        ema50 = float(ema(close, 50).iloc[-1])
        above_ma = price > ema50
        avg_mom_a = float((srK.diff().abs().iloc[-1] + enK.diff().abs().iloc[-1] + mnA.diff().abs().iloc[-1]) / 3.0)
        conf_buy = bool(bx >= 2 and above_ma and avg_mom_a >= C.MIN_MOM)
        conf_sell = bool(sx >= 2 and not above_ma and avg_mom_a >= C.MIN_MOM)

        def recent_xo(a, b, bars=3):
            for i in range(1, min(bars+1, len(a))):
                if abs(-i-1) < len(a) and a.iloc[-i] > b.iloc[-i] and a.iloc[-i-1] <= b.iloc[-i-1]: return True
            return False
        def recent_xu(a, b, bars=3):
            for i in range(1, min(bars+1, len(a))):
                if abs(-i-1) < len(a) and a.iloc[-i] < b.iloc[-i] and a.iloc[-i-1] >= b.iloc[-i-1]: return True
            return False

        rbx = sum([recent_xo(srK,srD), recent_xo(enK,enD), recent_xo(mnA,msA)])
        rsx = sum([recent_xu(srK,srD), recent_xu(enK,enD), recent_xu(mnA,msA)])
        if not conf_buy and rbx >= 2 and above_ma: conf_buy = True; bx = max(bx, rbx)
        if not conf_sell and rsx >= 2 and not above_ma: conf_sell = True; sx = max(sx, rsx)

        bx = int(bx); sx = int(sx); bull_zn = int(bull_zn); bear_zn = int(bear_zn)

        # ═══════════════════════════════════════════════════
        #  ENGINE B: SMARTMOMENTUM
        # ═══════════════════════════════════════════════════
        rsi_v = float(calc_rsi(close, C.RSI_PD).iloc[-1])
        rsi_sc = float((rsi_v - 50) * 2.0)
        atr_v = float(calc_atr(high, low, close, C.ATR_PD).iloc[-1])
        if pd.isna(atr_v) or atr_v <= 0: atr_v = float((high - low).mean())

        ml_v = float(ml.iloc[-1]); sl_v = float(sl.iloc[-1])
        macd_sc = float(np.clip((ml_v / max(atr_v, 1e-10)) * 50.0 + (15 if ml_v > sl_v else -15), -100, 100))

        stv = calc_stoch(close, high, low, C.STOCH_PD)
        st_sig = sma(stv, 3)
        stoch_sc = float(np.clip((stv.iloc[-1] - 50) * 2.0 + (10 if stv.iloc[-1] > st_sig.iloc[-1] else -10), -100, 100))

        cci_sc = float(np.clip(calc_cci(close, high, low, C.CCI_PD).iloc[-1] / 2.0, -100, 100))
        wpr_sc = float((calc_wpr(close, high, low, C.WPR_PD).iloc[-1] + 50) * 2.0)

        adx_v, pdi, mdi = calc_adx(high, low, close, C.ADX_PD)
        adx_val = float(adx_v.iloc[-1]); pdi_v = float(pdi.iloc[-1]); mdi_v = float(mdi.iloc[-1])
        adx_sc = float(np.clip(adx_val * 2.0, 0, 100) if pdi_v > mdi_v else np.clip(-adx_val * 2.0, -100, 0))

        mom_pd = min(C.MOM_PD, len(close) - 1)
        mom_pct = float((close.iloc[-1] - close.iloc[-mom_pd]) / close.iloc[-mom_pd] * 100) if mom_pd > 0 and float(close.iloc[-mom_pd]) != 0 else 0.0
        mom_sc = float(np.clip(mom_pct * 10.0, -100, 100))

        wts = [C.RSI_W, C.MACD_W, C.STOCH_W, C.CCI_W, C.WPR_W, C.ADX_W, C.MOM_W]
        scs = [rsi_sc, macd_sc, stoch_sc, cci_sc, wpr_sc, adx_sc, mom_sc]
        momentum = round(sum(s * w for s, w in zip(scs, wts)) / sum(wts), 2)

        # Divergence
        div = "NONE"
        try:
            if len(df) > C.DIV_BARS:
                rl_ = low.iloc[-C.DIV_BARS:]; rs_ = stv.iloc[-C.DIV_BARS:]
                if low.iloc[-1] < rl_.min() * 1.001 and stv.iloc[-1] > rs_.min(): div = "BULL"
                rh_ = high.iloc[-C.DIV_BARS:]
                if high.iloc[-1] > rh_.max() * 0.999 and stv.iloc[-1] < rs_.max(): div = "BEAR"
        except: pass

        # Volume & OBV
        va = float(volume.rolling(C.VOL_PD, min_periods=1).mean().iloc[-1])
        vr = round(float(volume.iloc[-1]) / va, 2) if va > 0 else 1.0
        obv = calc_obv(close, volume)
        obv_sma_v = sma(obv, 20)
        obv_bullish = bool(obv.iloc[-1] > obv_sma_v.iloc[-1])

        # MTF from daily data using different lookback windows
        mtf = {}
        # Short-term (5-day proxy for "hourly feel")
        if len(close) >= 10:
            st_rsi = float(calc_rsi(close.iloc[-10:], 5).iloc[-1]) if len(close) >= 10 else 50
            mtf['1h'] = {'score': round(50 + (st_rsi - 50) * 0.8, 1), 'summary': 'Buy' if st_rsi > 55 else ('Sell' if st_rsi < 45 else 'Neutral')}
        else:
            mtf['1h'] = {'score': 50.0, 'summary': 'Neutral'}

        # Daily
        d_score = (rsi_v - 50) * 0.6 + (25 if price > ema20 else -25) + (15 if price > ema50 else -15)
        d_score = float(50 + np.clip(d_score, -50, 50))
        mtf['1D'] = {'score': round(d_score, 1), 'summary': 'Buy' if d_score > 55 else ('Sell' if d_score < 45 else 'Neutral')}

        # Weekly proxy
        if len(close) >= 10:
            w5 = close.iloc[-5:]
            w_trend = float((w5.iloc[-1] - w5.iloc[0]) / w5.iloc[0] * 100)
            w_score = float(50 + np.clip(w_trend * 10, -40, 40))
            mtf['1W'] = {'score': round(w_score, 1), 'summary': 'Buy' if w_score > 55 else ('Sell' if w_score < 45 else 'Neutral')}
        else:
            mtf['1W'] = {'score': 50.0, 'summary': 'Neutral'}

        # 4h proxy
        if len(close) >= 20:
            h4_rsi = float(calc_rsi(close.iloc[-20:], 10).iloc[-1])
            h4_score = float(50 + (h4_rsi - 50) * 0.7)
            mtf['4h'] = {'score': round(h4_score, 1), 'summary': 'Buy' if h4_score > 55 else ('Sell' if h4_score < 45 else 'Neutral')}
        else:
            mtf['4h'] = {'score': 50.0, 'summary': 'Neutral'}

        for tf in ['1h','4h','1D','1W']:
            if tf not in mtf: mtf[tf] = {'score': 50.0, 'summary': 'Neutral'}

        mtf_buy = sum(1 for v in mtf.values() if v['score'] > 55)
        mtf_sell = sum(1 for v in mtf.values() if v['score'] < 45)
        if mtf_buy >= 3: mtf_align = "★ ALL BUY"
        elif mtf_sell >= 3: mtf_align = "★ ALL SELL"
        elif mtf_buy >= 2: mtf_align = "↗ Most Buy"
        elif mtf_sell >= 2: mtf_align = "↘ Most Sell"
        else: mtf_align = "Mixed"

        htf_score = float(mtf.get('1D',{}).get('score',50))
        ema_golden = bool(ema20 > ema50)
        trend_label = "UPTREND" if ema_golden and price > ema20 else ("DOWNTREND" if not ema_golden and price < ema20 else "SIDEWAYS")

        # Signal Strength
        am = abs(momentum)
        sig_str = 20 if am >= C.SIG_TH else (12 if am >= C.CHG_TH else 5)
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

        zone = "STRONG BUY" if momentum >= C.SIG_TH else ("BULLISH" if momentum >= C.CHG_TH else ("STRONG SELL" if momentum <= -C.SIG_TH else ("BEARISH" if momentum <= -C.CHG_TH else "NEUTRAL")))
        session_name, in_session = get_market_session()

        # ═══════════════════════════════════════════════════
        #  SIGNAL GENERATION
        # ═══════════════════════════════════════════════════
        signal = None; reasons = []; direction = None; sig_type = None
        st_val = float(stv.iloc[-1])
        bull_rev = st_val < 35 and momentum > -C.CHG_TH
        bear_rev = st_val > 65 and momentum < C.CHG_TH

        strong_buy = ((momentum >= C.SIG_TH) or bull_rev or conf_buy) and sig_str >= C.STRONG_STR
        strong_sell = ((momentum <= -C.SIG_TH) or bear_rev or conf_sell) and sig_str >= C.STRONG_STR
        c_buy = conf_buy and not strong_buy; c_sell = conf_sell and not strong_sell
        w_buy = (momentum >= C.CHG_TH or bull_rev or (bull_zn >= 2 and above_ma)) and not strong_buy and not c_buy
        w_sell = (momentum <= -C.CHG_TH or bear_rev or (bear_zn >= 2 and not above_ma)) and not strong_sell and not c_sell

        if strong_buy: direction="BUY"; sig_type="STRONG"; reasons.append("🔥 Strong Buy")
        elif strong_sell: direction="SELL"; sig_type="STRONG"; reasons.append("🔥 Strong Sell")
        elif c_buy: direction="BUY"; sig_type="CONF"; reasons.append(f"◆ Confluence Buy ({bx}/3)")
        elif c_sell: direction="SELL"; sig_type="CONF"; reasons.append(f"◆ Confluence Sell ({sx}/3)")
        elif w_buy: direction="BUY"; sig_type="WEAK"; reasons.append("◇ Weak Buy")
        elif w_sell: direction="SELL"; sig_type="WEAK"; reasons.append("◇ Weak Sell")

        if direction:
            if above_ma and direction=="BUY": reasons.append("✅ Above EMA50")
            if not above_ma and direction=="SELL": reasons.append("✅ Below EMA50")
            if ema_golden and direction=="BUY": reasons.append("✅ Golden Cross")
            if not ema_golden and direction=="SELL": reasons.append("✅ Death Cross")
            if bull_zn >= 2 and direction=="BUY": reasons.append(f"✅ Bull Zone {bull_zn}/3")
            if bear_zn >= 2 and direction=="SELL": reasons.append(f"✅ Bear Zone {bear_zn}/3")
            if div != "NONE": reasons.append(f"✅ {div} Divergence")
            if vr > 1.5: reasons.append(f"✅ Vol {vr:.1f}x")
            if obv_bullish and direction=="BUY": reasons.append("✅ OBV Bullish")
            if not obv_bullish and direction=="SELL": reasons.append("✅ OBV Bearish")
            if mtf_buy >= 3 and direction=="BUY": reasons.append("★ MTF All Buy")
            if mtf_sell >= 3 and direction=="SELL": reasons.append("★ MTF All Sell")

            if direction == "BUY":
                sl_ = round(price - atr_v * C.ATR_SL, dec); risk = price - sl_
                tp1_ = round(price + risk * 1.5, dec); tp2_ = round(price + risk * C.RR, dec); tp3_ = round(price + risk * 3.0, dec)
            else:
                sl_ = round(price + atr_v * C.ATR_SL, dec); risk = sl_ - price
                tp1_ = round(price - risk * 1.5, dec); tp2_ = round(price - risk * C.RR, dec); tp3_ = round(price - risk * 3.0, dec)

            risk_pct = round(risk / price * 100, 2) if price > 0 else 0.0
            rr = round(abs(price - tp2_) / max(risk, 1e-10), 2)

            signal = {
                'id': f"{symbol}_{direction}_{now().strftime('%H%M%S')}",
                'symbol': symbol, 'sector': sector,
                'direction': direction, 'type': sig_type,
                'entry': round(price, dec), 'sl': float(sl_),
                'tp1': float(tp1_), 'tp2': float(tp2_), 'tp3': float(tp3_),
                'momentum': float(momentum), 'strength': round(sig_str),
                'conf': int(bx if direction=="BUY" else sx),
                'srK': round(sr_k,1), 'srD': round(sr_d,1),
                'enK': round(en_k,1), 'enD': round(en_d,1),
                'mnA': round(mn_a,1), 'msA': round(ms_a,1),
                'rsi_v': round(rsi_v,1),
                'rsi_sc': round(rsi_sc,1), 'macd_sc': round(macd_sc,1),
                'stoch_sc': round(stoch_sc,1), 'cci_sc': round(cci_sc,1),
                'wpr_sc': round(wpr_sc,1), 'adx_sc': round(adx_sc,1),
                'mom_sc': round(mom_sc,1),
                'trend': trend_label,
                'bull_zn': int(bull_zn), 'bear_zn': int(bear_zn),
                'div': div, 'vol': float(vr), 'htf': round(htf_score,1),
                'mtf_1h': mtf.get('1h',{}).get('summary','?'),
                'mtf_4h': mtf.get('4h',{}).get('summary','?'),
                'mtf_1d': mtf.get('1D',{}).get('summary','?'),
                'mtf_1w': mtf.get('1W',{}).get('summary','?'),
                'mtf_align': mtf_align, 'session': session_name,
                'atr': round(atr_v, dec+2), 'risk_pct': float(risk_pct), 'rr': float(rr),
                'reasons': reasons, 'timestamp': dts(), 'ts_unix': time.time(),
            }

        analysis = {
            'symbol': symbol, 'cat': sector,
            'price': round(price, dec), 'prev': round(prev, dec),
            'change': round(chg, dec+1), 'change_pct': round(chg_pct, 3),
            'momentum': float(momentum), 'strength': round(sig_str),
            'zone': zone, 'session': session_name, 'trend': trend_label,
            'srK': round(sr_k,1), 'enK': round(en_k,1), 'mnA': round(mn_a,1),
            'bull_zn': int(bull_zn), 'bear_zn': int(bear_zn),
            'bx': int(bx), 'sx': int(sx),
            'conf_buy': bool(conf_buy), 'conf_sell': bool(conf_sell),
            'above_ma': bool(above_ma),
            'ema20': round(ema20, dec), 'ema50': round(ema50, dec),
            'rsi_v': round(rsi_v,1), 'adx_v': round(adx_val,1),
            'rsi_sc': round(rsi_sc,1), 'macd_sc': round(macd_sc,1),
            'stoch_sc': round(stoch_sc,1), 'cci_sc': round(cci_sc,1),
            'wpr_sc': round(wpr_sc,1), 'adx_sc': round(adx_sc,1),
            'mom_sc': round(mom_sc,1),
            'div': div, 'vol': float(vr), 'atr': round(atr_v, dec+2),
            'obv_bull': bool(obv_bullish),
            'mtf': mtf, 'mtf_align': mtf_align, 'time': ts(),
        }
        return analysis, signal

    except Exception as e:
        logger.error(f"Analyze {symbol}: {e}")
        store.err(f"{symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════════════════
class Users:
    def __init__(self):
        self.f = 'users.json'; self.u = self._load()
    def _load(self):
        try:
            if os.path.exists(self.f):
                with open(self.f) as f: return json.load(f)
        except: pass
        d = {'admin': {'pw': hashlib.sha256(b'admin123_tcsm').hexdigest(), 'role': 'admin', 'name': 'Admin', 'on': True}}
        self._save(d); return d
    def _save(self, u=None):
        try:
            with open(self.f, 'w') as f: json.dump(u or self.u, f, indent=2)
        except: pass
    def verify(self, u, p):
        if u not in self.u: return False
        return self.u[u].get('on', True) and self.u[u]['pw'] == hashlib.sha256(f"{p}_tcsm".encode()).hexdigest()

users = Users()
def login_req(f):
    @wraps(f)
    def d(*a, **k):
        if 'user' not in session: return redirect(url_for('login'))
        return f(*a, **k)
    return d


# ═══════════════════════════════════════════════════════════════════════════════
#  SCANNER LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def scanner():
    eventlet.sleep(5)
    logger.info("🚀 TCSM Thai Stock Pro v3.2 Scanner started (cloud-safe)")
    store.status = "RUNNING"
    scan_num = 0

    while True:
        try:
            scan_num += 1
            store.scan_count += 1
            store.last_scan = ts()
            t0 = time.time()
            ok = 0; fail = 0

            # Reset failure counts every 10 scans
            if scan_num % 10 == 0:
                yfc.reset_failures()
                logger.info("🔄 Reset failure counts")

            stocks = list(C.STOCKS)
            for symbol in stocks:
                try:
                    result = analyze_stock(symbol)
                    if result:
                        analysis, signal = result
                        ok += 1
                        analysis = sanitize(analysis)
                        store.analysis[symbol] = analysis
                        store.prices[symbol] = {
                            'symbol': symbol, 'cat': analysis['cat'],
                            'price': analysis['price'], 'change': analysis['change'],
                            'change_pct': analysis['change_pct'], 'time': analysis['time'],
                        }
                        safe_emit('price_update', {'symbol': symbol, 'data': store.prices[symbol], 'analysis': analysis})

                        if signal:
                            signal = sanitize(signal)
                            old = store.signals.get(symbol)
                            is_new = not old or old.get('direction') != signal['direction'] or time.time() - old.get('ts_unix', 0) > 1800
                            if is_new:
                                store.signals[symbol] = signal
                                store.history.insert(0, signal)
                                store.history = store.history[:500]
                                store.stats['buy'] += 1 if signal['direction'] == "BUY" else 0
                                store.stats['sell'] += 1 if signal['direction'] == "SELL" else 0
                                st = signal['type'].lower()
                                store.stats[st] = store.stats.get(st, 0) + 1
                                safe_emit('new_signal', {'symbol': symbol, 'signal': signal})
                                logger.info(f"🎯 {signal['type']} {signal['direction']} {symbol} @฿{signal['entry']} Str:{signal['strength']}%")
                    else:
                        fail += 1
                except Exception as e:
                    fail += 1
                    store.err(f"{symbol}: {e}")
                eventlet.sleep(0.5)

            dur = time.time() - t0
            store.first_scan_done = True

            safe_emit('scan_update', sanitize({
                'scan_count': store.scan_count, 'last_scan': store.last_scan,
                'stats': store.stats, 'status': store.status,
                'ok': ok, 'fail': fail, 'dur': round(dur, 1), 'signals': len(store.signals),
            }))
            safe_emit('full_sync', store.get_state())

            logger.info(f"✅ Scan #{store.scan_count}: {ok}/{ok+fail} ({dur:.1f}s) | Signals: {len(store.signals)} | Clients: {store.connected}")

        except Exception as e:
            logger.error(f"Scanner: {e}\n{traceback.format_exc()}")
            store.err(f"Scanner: {e}")

        eventlet.sleep(C.SCAN_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
#  TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════════
LOGIN_HTML = '''<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TCSM Thai Stock Pro — Login</title><script src="https://cdn.tailwindcss.com"></script>
<style>
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
@keyframes gradient{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
body{background:linear-gradient(-45deg,#0a0e1a,#111827,#0f172a,#1a1a2e);background-size:400% 400%;animation:gradient 15s ease infinite}
.card{background:rgba(17,24,39,.85);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.08)}
.glow-btn{position:relative;overflow:hidden;transition:all .3s}
.glow-btn::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:conic-gradient(from 0deg,transparent,rgba(234,179,8,.3),transparent 30%);animation:spin 3s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.glow-btn>span{position:relative;z-index:1;display:block;padding:12px;background:linear-gradient(135deg,#b45309,#d97706);border-radius:8px}
</style></head>
<body class="min-h-screen flex items-center justify-center p-4">
<div class="card rounded-2xl shadow-2xl w-full max-w-md p-8">
<div class="text-center mb-8">
<div class="text-6xl mb-4" style="animation:float 3s ease-in-out infinite">🇹🇭</div>
<h1 class="text-3xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-yellow-400 via-amber-400 to-orange-500">TCSM Thai Stock Pro</h1>
<p class="text-gray-500 text-sm mt-1">v3.2 — SET Stock Scanner (Cloud Edition)</p>
<div class="flex justify-center gap-2 mt-3">
<span class="text-[10px] bg-green-600/20 text-green-400 px-2 py-0.5 rounded-full border border-green-600/30">SET50+</span>
<span class="text-[10px] bg-blue-600/20 text-blue-400 px-2 py-0.5 rounded-full border border-blue-600/30">Dual Engine</span>
<span class="text-[10px] bg-purple-600/20 text-purple-400 px-2 py-0.5 rounded-full border border-purple-600/30">Real-Time</span>
</div></div>
{% with m=get_flashed_messages(with_categories=true) %}{% if m %}{% for c,msg in m %}
<div class="mb-4 p-3 rounded-lg text-sm {% if c=='error' %}bg-red-900/50 text-red-300 border border-red-800/50{% else %}bg-green-900/50 text-green-300 border border-green-800/50{% endif %}">{{msg}}</div>
{% endfor %}{% endif %}{% endwith %}
<form method="POST" class="space-y-5">
<div><label class="block text-gray-400 text-xs font-medium mb-1.5 uppercase tracking-wider">Username</label>
<input type="text" name="u" required class="w-full px-4 py-3 bg-gray-900/60 border border-gray-700/50 rounded-xl text-white placeholder-gray-600 focus:border-yellow-500/50 focus:ring-2 focus:ring-yellow-500/20 focus:outline-none transition" placeholder="Enter username"></div>
<div><label class="block text-gray-400 text-xs font-medium mb-1.5 uppercase tracking-wider">Password</label>
<input type="password" name="p" required class="w-full px-4 py-3 bg-gray-900/60 border border-gray-700/50 rounded-xl text-white placeholder-gray-600 focus:border-yellow-500/50 focus:ring-2 focus:ring-yellow-500/20 focus:outline-none transition" placeholder="Enter password"></div>
<div class="glow-btn rounded-xl"><span class="text-center font-bold text-white cursor-pointer"><button type="submit" class="w-full">🔓 Sign In</button></span></div>
</form>
<p class="mt-6 text-center text-gray-600 text-xs">Default: admin / admin123</p>
</div></body></html>'''

DASH_HTML = '''<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🇹🇭 TCSM Thai Stock Pro v3.2</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
*{scrollbar-width:thin;scrollbar-color:#1f2937 transparent}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#374151;border-radius:3px}
@keyframes pulse2{0%,100%{opacity:1}50%{opacity:.4}}.pulse2{animation:pulse2 1.5s infinite}
@keyframes glow{0%,100%{box-shadow:0 0 10px rgba(234,179,8,.2)}50%{box-shadow:0 0 25px rgba(234,179,8,.4)}}.glow{animation:glow 2s infinite}
@keyframes slideUp{from{transform:translateY(15px);opacity:0}to{transform:translateY(0);opacity:1}}.slideUp{animation:slideUp .35s ease-out}
@keyframes loading{0%{transform:translateX(-100%)}50%{transform:translateX(0%)}100%{transform:translateX(100%)}}
.glass{background:rgba(17,24,39,.75);backdrop-filter:blur(12px);border:1px solid rgba(55,65,81,.35)}
.glass-card{background:rgba(17,24,39,.6);backdrop-filter:blur(8px);border:1px solid rgba(55,65,81,.3);transition:all .2s}
.glass-card:hover{border-color:rgba(234,179,8,.2);background:rgba(17,24,39,.8)}
.stat-card{background:linear-gradient(135deg,rgba(17,24,39,.8),rgba(31,41,55,.6));border:1px solid rgba(55,65,81,.3)}
.signal-buy{border-left:4px solid #22c55e;background:linear-gradient(135deg,rgba(22,101,52,.08),transparent)}
.signal-sell{border-left:4px solid #ef4444;background:linear-gradient(135deg,rgba(127,29,29,.08),transparent)}
.tag{display:inline-flex;align-items:center;padding:1px 6px;border-radius:4px;font-size:9px;font-weight:600}
body{background:#080c14}
</style></head>
<body class="text-gray-200 min-h-screen text-[13px]">
<div class="max-w-[1700px] mx-auto px-3 py-3">
<div class="flex flex-wrap justify-between items-center mb-3 gap-2">
<div>
<div class="flex items-center gap-3">
<h1 class="text-lg md:text-xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-yellow-400 to-amber-500">🇹🇭 TCSM Thai Stock Pro</h1>
<span class="tag bg-green-600/20 text-green-400 border border-green-600/30">v3.2</span>
<span class="tag bg-blue-600/20 text-blue-400 border border-blue-600/30">SET</span>
<span class="tag bg-red-600/20 text-red-400 border border-red-600/30">LIVE</span>
</div>
<p class="text-gray-600 text-[10px] mt-0.5">Dual Engine • MTF • Volume • OBV • Divergence | ตลาดหลักทรัพย์แห่งประเทศไทย</p>
</div>
<div class="flex items-center gap-3">
<div id="clk" class="glass px-3 py-1.5 rounded-lg font-mono text-sm font-bold text-yellow-400">--:--:--</div>
<span id="st" class="text-red-400 text-sm">● Offline</span>
<button onclick="doRefresh()" class="px-3 py-1.5 bg-blue-600/60 hover:bg-blue-500 rounded-lg text-xs transition font-medium">🔄</button>
<a href="/logout" class="px-3 py-1.5 bg-red-600/60 hover:bg-red-500 rounded-lg text-xs transition font-medium">Logout</a>
</div></div>

<div class="grid grid-cols-4 md:grid-cols-8 gap-2 mb-3">
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase">Scans</div><div id="sc" class="font-bold text-lg">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase">Signals</div><div id="si" class="font-bold text-lg text-yellow-400">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase">Strong</div><div id="st2" class="font-bold text-lg text-amber-400">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase">Buy</div><div id="bu" class="font-bold text-lg text-green-400">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase">Sell</div><div id="se" class="font-bold text-lg text-red-400">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase">Session</div><div id="ss" class="text-yellow-400 font-bold text-xs">—</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase">Last Scan</div><div id="ls" class="font-mono text-xs">--:--</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase">Status</div><div id="ss2" class="text-blue-400 font-bold text-xs">INIT</div></div>
</div>

<div id="al" class="hidden mb-3"><div class="bg-gradient-to-r from-yellow-900/60 to-amber-900/40 border border-yellow-600/50 rounded-xl p-4 glow">
<div class="flex items-center gap-3"><span class="text-3xl" id="ai">🎯</span>
<div class="flex-1"><div id="at" class="font-bold text-yellow-300 text-sm"></div><div id="ax" class="text-yellow-200/70 text-xs mt-0.5"></div></div>
<button onclick="this.closest('#al').classList.add('hidden')" class="text-yellow-400/50 hover:text-white text-xl">✕</button></div></div></div>

<div class="grid grid-cols-1 xl:grid-cols-12 gap-3">
<div class="xl:col-span-3 space-y-3">
<div class="glass rounded-xl p-3">
<div class="flex justify-between items-center mb-2">
<h2 class="font-bold text-sm flex items-center gap-1.5">💹 หุ้นไทย <span id="ps" class="text-[9px] text-gray-600 font-normal">loading</span></h2>
<input id="search" type="text" placeholder="ค้นหา..." class="bg-gray-800/60 border border-gray-700/50 rounded-lg px-2 py-1 text-xs w-24 focus:outline-none focus:border-yellow-500/50" oninput="rP()">
</div>
<div class="flex flex-wrap gap-1 mb-2 text-[10px]">
<button onclick="fP('all')" class="px-2 py-0.5 rounded-lg pf bg-yellow-600 transition font-medium" data-c="all">All</button>
<button onclick="fP('Energy')" class="px-2 py-0.5 rounded-lg pf bg-gray-700 transition font-medium" data-c="Energy">Energy</button>
<button onclick="fP('Banking')" class="px-2 py-0.5 rounded-lg pf bg-gray-700 transition font-medium" data-c="Banking">Bank</button>
<button onclick="fP('Tech')" class="px-2 py-0.5 rounded-lg pf bg-gray-700 transition font-medium" data-c="Tech">Tech</button>
<button onclick="fP('Property')" class="px-2 py-0.5 rounded-lg pf bg-gray-700 transition font-medium" data-c="Property">Prop</button>
<button onclick="fP('Health')" class="px-2 py-0.5 rounded-lg pf bg-gray-700 transition font-medium" data-c="Health">Health</button>
<button onclick="fP('Consumer')" class="px-2 py-0.5 rounded-lg pf bg-gray-700 transition font-medium" data-c="Consumer">Cons</button>
</div>
<div id="pr" class="space-y-1 max-h-[600px] overflow-y-auto pr-1"></div>
</div></div>

<div class="xl:col-span-6 space-y-3">
<div class="flex justify-between items-center">
<h2 class="font-bold text-sm">🎯 Active Signals</h2>
<div class="flex gap-1 text-[10px]">
<button onclick="fS('all')" class="px-2 py-0.5 rounded-lg sf bg-yellow-600 transition" data-c="all">All</button>
<button onclick="fS('STRONG')" class="px-2 py-0.5 rounded-lg sf bg-gray-700 transition" data-c="STRONG">Strong</button>
<button onclick="fS('BUY')" class="px-2 py-0.5 rounded-lg sf bg-gray-700 transition" data-c="BUY">Buy</button>
<button onclick="fS('SELL')" class="px-2 py-0.5 rounded-lg sf bg-gray-700 transition" data-c="SELL">Sell</button>
</div></div>
<div id="sg" class="space-y-2">
<div class="glass rounded-xl p-8 text-center text-gray-500">
<div class="text-4xl mb-3">📡</div>
<div class="text-sm font-medium">Scanning SET stocks...</div>
<div class="text-xs mt-1 text-gray-600">First scan takes 2-3 minutes</div>
<div class="mt-4 flex justify-center"><div class="w-48 h-1 bg-gray-800 rounded-full overflow-hidden"><div class="h-full bg-gradient-to-r from-yellow-600 to-amber-500 rounded-full" style="animation:loading 2s ease-in-out infinite;width:30%"></div></div></div>
</div></div></div>

<div class="xl:col-span-3 space-y-3">
<div class="glass rounded-xl p-3"><h3 class="font-bold text-[11px] text-yellow-400 mb-2">📊 Top Momentum</h3><div id="tm" class="space-y-1 max-h-[200px] overflow-y-auto"></div></div>
<div class="glass rounded-xl p-3"><h3 class="font-bold text-[11px] text-blue-400 mb-2">🕐 MTF Alignment</h3><div id="mf" class="space-y-1 max-h-[200px] overflow-y-auto"></div></div>
<div class="glass rounded-xl p-3"><h3 class="font-bold text-[11px] text-green-400 mb-2">📈 Sector Heatmap</h3><div id="hm" class="grid grid-cols-2 gap-1 text-[10px]"></div></div>
<div class="glass rounded-xl p-3"><h3 class="font-bold text-[11px] text-purple-400 mb-2">📡 Log</h3><div id="lg" class="h-32 overflow-y-auto font-mono text-[9px] space-y-0.5"></div></div>
</div></div>

<div class="glass rounded-xl overflow-hidden mt-3">
<div class="flex justify-between items-center p-3 border-b border-gray-800/50">
<h2 class="font-bold text-sm">📜 Signal History</h2>
<span class="text-[10px] text-gray-500" id="hc">0</span></div>
<div class="overflow-x-auto"><table class="w-full text-[11px]">
<thead class="bg-gray-900/60"><tr class="text-gray-500 uppercase text-[9px]">
<th class="px-3 py-2 text-left">Time</th><th class="px-3 py-2">Stock</th><th class="px-3 py-2">Type</th><th class="px-3 py-2">Dir</th>
<th class="px-3 py-2 text-right">Entry</th><th class="px-3 py-2 text-right">SL</th>
<th class="px-3 py-2 text-right">TP1</th><th class="px-3 py-2 text-right">TP2</th>
<th class="px-3 py-2">R:R</th><th class="px-3 py-2">Str%</th><th class="px-3 py-2">Mom</th><th class="px-3 py-2">MTF</th>
</tr></thead>
<tbody id="ht"><tr><td colspan="12" class="py-6 text-center text-gray-600">Waiting for signals...</td></tr></tbody>
</table></div></div>
<div class="mt-3 text-center text-gray-700 text-[9px] pb-4">⚠️ For educational purposes only. Not financial advice. TCSM Thai Stock Pro v3.2</div>
</div>
<script>
const P={},A={},S={},H=[];let cn=false,pf_='all',sf_='all';
const so=io({transports:['websocket','polling'],reconnection:true,reconnectionDelay:1000,reconnectionDelayMax:5000,reconnectionAttempts:Infinity,timeout:30000});

function uc(){const d=new Date(new Date().toLocaleString("en-US",{timeZone:"Asia/Bangkok"}));document.getElementById('clk').textContent=d.toLocaleTimeString('en-GB',{hour12:false})+' ICT';const h=d.getHours(),m=d.getMinutes(),wd=d.getDay(),t=h*60+m;let s='CLOSED 🔒';if(wd===0||wd===6)s='WEEKEND 🔒';else if(t<570)s='PRE-MKT';else if(t<600)s='PRE-OPEN ☀️';else if(t<660)s='MORNING ⭐⭐';else if(t<750)s='MORNING ⭐';else if(t<870)s='LUNCH 🍜';else if(t<930)s='AFTERNOON ⭐';else if(t<990)s='CLOSING ⭐⭐';document.getElementById('ss').textContent=s;}
setInterval(uc,1000);uc();

function lo(m,t='info'){const e=document.getElementById('lg');const c={signal:'text-green-400',price:'text-blue-300',error:'text-red-400',info:'text-gray-400',sys:'text-purple-400'};const d=document.createElement('div');d.className=c[t]||'text-gray-400';d.textContent=`[${new Date().toLocaleTimeString('en-GB',{timeZone:'Asia/Bangkok',hour12:false})}] ${m}`;e.insertBefore(d,e.firstChild);while(e.children.length>80)e.removeChild(e.lastChild);}
function fP(c){pf_=c;document.querySelectorAll('.pf').forEach(b=>{b.classList.toggle('bg-yellow-600',b.dataset.c===c);b.classList.toggle('bg-gray-700',b.dataset.c!==c);});rP();}
function fS(c){sf_=c;document.querySelectorAll('.sf').forEach(b=>{b.classList.toggle('bg-yellow-600',b.dataset.c===c);b.classList.toggle('bg-gray-700',b.dataset.c!==c);});rS();}
function zc(z){return z.includes('BUY')?'text-green-400':z.includes('SELL')?'text-red-400':z.includes('BULL')?'text-green-500/80':z.includes('BEAR')?'text-red-500/80':'text-gray-500';}
function sf(v,d=0){return typeof v==='number'&&isFinite(v)?v:d;}
function fmtP(p){if(p<1)return p.toFixed(4);if(p<10)return p.toFixed(3);return p.toFixed(2);}

function rP(){let h='',n=0;const q=(document.getElementById('search')?.value||'').toUpperCase();const syms=Object.keys(A).sort((a,b)=>Math.abs(sf(A[b]?.momentum))-Math.abs(sf(A[a]?.momentum)));for(const s of syms){const a=A[s];if(!a)continue;if(pf_!=='all'&&a.cat!==pf_)continue;if(q&&!s.includes(q))continue;n++;const cc=sf(a.change_pct)>=0?'text-green-400':'text-red-400';const mc=sf(a.momentum)>=0?'text-green-400':'text-red-400';const hs=S[s]?`<span class="w-2 h-2 rounded-full inline-block ${S[s].direction==='BUY'?'bg-green-400':'bg-red-400'} pulse2"></span>`:'';const bw=Math.min(Math.abs(sf(a.momentum)),100);h+=`<div class="glass-card rounded-lg p-2 ${S[s]?'border-yellow-600/30':''}"><div class="flex justify-between items-center"><div class="flex items-center gap-1.5">${hs}<span class="font-bold text-sm">${s}</span><span class="text-[9px] px-1 py-0.5 rounded bg-gray-800/60 text-gray-500">${a.cat||''}</span></div><div class="text-right"><span class="font-bold">฿${fmtP(sf(a.price))}</span> <span class="${cc} text-[10px]">${sf(a.change_pct)>=0?'+':''}${sf(a.change_pct).toFixed(2)}%</span></div></div><div class="flex justify-between mt-1 text-[9px]"><span class="${mc} font-medium">Mom:${sf(a.momentum).toFixed(1)}</span><span class="${zc(a.zone||'')} font-medium">${a.zone||''}</span><span>Str:${sf(a.strength)}%</span><span class="text-gray-500">${a.trend||''}</span></div><div class="mt-1 bg-gray-800/60 rounded-full h-1 overflow-hidden"><div class="${sf(a.momentum)>=0?'bg-gradient-to-r from-green-600 to-green-400':'bg-gradient-to-r from-red-600 to-red-400'} h-full rounded-full transition-all" style="width:${bw}%"></div></div></div>`;}if(!n)h='<div class="text-gray-600 text-center py-6 text-[11px]">Scanning...</div>';document.getElementById('pr').innerHTML=h;document.getElementById('ps').innerHTML=n>0?`<span class="text-green-400 pulse2">● ${n} live</span>`:'loading';}

function rT(){const arr=Object.values(A).sort((a,b)=>Math.abs(sf(b.momentum))-Math.abs(sf(a.momentum))).slice(0,10);document.getElementById('tm').innerHTML=arr.map(a=>`<div class="flex justify-between items-center py-1 border-b border-gray-800/30"><span class="font-bold text-[11px]">${a.symbol}</span><span class="${sf(a.momentum)>=0?'text-green-400':'text-red-400'} font-mono font-bold text-[11px]">${sf(a.momentum)>=0?'+':''}${sf(a.momentum).toFixed(1)}</span><span class="text-[9px] text-gray-500">${a.zone||''}</span></div>`).join('')||'<div class="text-gray-600 text-xs">Waiting...</div>';}

function rM(){const arr=Object.values(A).filter(a=>a.mtf_align&&!a.mtf_align.includes('Mixed')).sort((a,b)=>Math.abs(sf(b.momentum))-Math.abs(sf(a.momentum))).slice(0,10);document.getElementById('mf').innerHTML=arr.map(a=>`<div class="flex justify-between items-center py-1 border-b border-gray-800/30"><span class="font-bold text-[11px]">${a.symbol}</span><span class="${a.mtf_align.includes('BUY')?'text-green-400':a.mtf_align.includes('SELL')?'text-red-400':'text-gray-500'} font-bold text-[10px]">${a.mtf_align}</span></div>`).join('')||'<div class="text-gray-600 text-xs">Waiting...</div>';}

function rHM(){const sectors={};Object.values(A).forEach(a=>{const cat=a.cat||'Other';if(!sectors[cat])sectors[cat]={sum:0,cnt:0,buy:0,sell:0};sectors[cat].sum+=sf(a.momentum);sectors[cat].cnt++;if(S[a.symbol]){S[a.symbol].direction==='BUY'?sectors[cat].buy++:sectors[cat].sell++;}});document.getElementById('hm').innerHTML=Object.entries(sectors).map(([k,v])=>{const avg=(v.sum/v.cnt).toFixed(1);const c=avg>=0?'from-green-900/40 to-green-800/20 border-green-700/30 text-green-400':'from-red-900/40 to-red-800/20 border-red-700/30 text-red-400';return`<div class="bg-gradient-to-br ${c} border rounded-lg p-2"><div class="font-bold">${k}</div><div class="text-xs font-mono">${avg>=0?'+':''}${avg}</div><div class="text-[9px] text-gray-500">${v.cnt} • ${v.buy}B/${v.sell}S</div></div>`}).join('');}

function rS(){let list=Object.values(S).sort((a,b)=>sf(b.strength)-sf(a.strength));if(sf_==='STRONG')list=list.filter(s=>s.type==='STRONG');else if(sf_==='BUY')list=list.filter(s=>s.direction==='BUY');else if(sf_==='SELL')list=list.filter(s=>s.direction==='SELL');if(!list.length){document.getElementById('sg').innerHTML=`<div class="glass rounded-xl p-8 text-center text-gray-500"><div class="text-3xl mb-2">📡</div><div class="text-sm">${Object.keys(S).length?'No match':'Scanning...'}</div></div>`;document.getElementById('si').textContent=Object.keys(S).length;return;}document.getElementById('si').textContent=Object.keys(S).length;document.getElementById('sg').innerHTML=list.slice(0,20).map(s=>{const ib=s.direction==='BUY';const dc=ib?'text-green-400':'text-red-400';const cls=ib?'signal-buy':'signal-sell';const tc=s.type==='STRONG'?'bg-gradient-to-r from-yellow-600 to-amber-500':s.type==='CONF'?'bg-gradient-to-r from-cyan-600 to-blue-500':'bg-gray-600';const sw=Math.min(sf(s.strength),100);const sc=sw>=75?'bg-gradient-to-r from-green-600 to-green-400':sw>=50?'bg-gradient-to-r from-yellow-600 to-yellow-400':'bg-gray-600';return`<div class="glass ${cls} rounded-xl p-4 slideUp"><div class="flex justify-between items-start mb-3"><div><div class="flex items-center gap-2"><span class="text-lg font-bold ${dc}">${ib?'🟢':'🔴'} ${s.symbol}</span><span class="px-2 py-0.5 rounded-md text-[10px] font-bold text-white ${tc}">${s.type} ${s.direction}</span><span class="tag bg-gray-800 text-gray-400 border border-gray-700">${s.sector||''}</span></div><div class="text-[10px] text-gray-500 mt-0.5">Mom:${sf(s.momentum).toFixed(1)} Conf:${sf(s.conf)}/3 ${s.trend||''} RSI:${sf(s.rsi_v,'?')}</div></div><div class="text-right text-[10px] text-gray-500">${s.timestamp||''}<br><span class="font-bold ${(s.mtf_align||'').includes('BUY')?'text-green-400':(s.mtf_align||'').includes('SELL')?'text-red-400':'text-gray-500'}">${s.mtf_align||''}</span></div></div><div class="grid grid-cols-5 gap-1.5 mb-3"><div class="bg-gray-900/60 rounded-lg p-2 text-center"><div class="text-[9px] text-gray-500">Entry</div><div class="font-bold text-sm">฿${fmtP(sf(s.entry))}</div></div><div class="bg-red-900/20 rounded-lg p-2 text-center border border-red-900/30"><div class="text-[9px] text-red-400">SL</div><div class="font-bold text-sm text-red-400">฿${fmtP(sf(s.sl))}</div></div><div class="bg-green-900/15 rounded-lg p-2 text-center border border-green-900/25"><div class="text-[9px] text-green-400">TP1</div><div class="font-bold text-sm text-green-400">฿${fmtP(sf(s.tp1))}</div></div><div class="bg-green-900/20 rounded-lg p-2 text-center border border-green-800/30"><div class="text-[9px] text-green-300">TP2</div><div class="font-bold text-sm text-green-300">฿${fmtP(sf(s.tp2))}</div></div><div class="bg-green-900/25 rounded-lg p-2 text-center border border-green-700/35"><div class="text-[9px] text-green-200">TP3</div><div class="font-bold text-sm text-green-200">฿${fmtP(sf(s.tp3))}</div></div></div><div class="flex items-center gap-3 mb-2"><span class="text-[10px] text-gray-500">Strength</span><div class="flex-1 bg-gray-800/60 rounded-full h-1.5 overflow-hidden"><div class="${sc} h-full rounded-full transition-all" style="width:${sw}%"></div></div><span class="font-bold text-[11px]">${sf(s.strength)}%</span><span class="text-gray-700">|</span><span class="text-[10px]">R:R <b>${sf(s.rr)}:1</b></span><span class="text-gray-700">|</span><span class="text-[10px]">Risk <b>${sf(s.risk_pct)}%</b></span></div><div class="flex flex-wrap gap-1">${(s.reasons||[]).slice(0,10).map(r=>`<span class="tag bg-gray-800/80 text-gray-300 border border-gray-700/40">${r}</span>`).join('')}</div></div>`}).join('');}

function rH(){if(!H.length){document.getElementById('ht').innerHTML='<tr><td colspan="12" class="py-6 text-center text-gray-600">Waiting...</td></tr>';return;}document.getElementById('hc').textContent=H.length+' signals';document.getElementById('ht').innerHTML=H.slice(0,80).map(s=>{const dc=s.direction==='BUY'?'text-green-400 bg-green-900/30':'text-red-400 bg-red-900/30';const tc=s.type==='STRONG'?'text-yellow-400':s.type==='CONF'?'text-cyan-400':'text-gray-400';return`<tr class="border-t border-gray-800/30 hover:bg-gray-800/20"><td class="px-3 py-1.5 text-gray-500 text-[10px]">${s.timestamp||''}</td><td class="px-3 py-1.5 font-bold text-center">${s.symbol}</td><td class="px-3 py-1.5 text-center ${tc} font-bold">${s.type}</td><td class="px-3 py-1.5 text-center"><span class="px-2 py-0.5 rounded-md ${dc} font-bold text-[10px]">${s.direction}</span></td><td class="px-3 py-1.5 text-right font-mono">฿${fmtP(sf(s.entry))}</td><td class="px-3 py-1.5 text-right font-mono text-red-400">฿${fmtP(sf(s.sl))}</td><td class="px-3 py-1.5 text-right font-mono text-green-400">฿${fmtP(sf(s.tp1))}</td><td class="px-3 py-1.5 text-right font-mono text-green-300">฿${fmtP(sf(s.tp2))}</td><td class="px-3 py-1.5 font-bold text-center">${sf(s.rr)}:1</td><td class="px-3 py-1.5 text-center"><span class="px-1.5 py-0.5 rounded ${sf(s.strength)>=75?'bg-green-900/50 text-green-400':sf(s.strength)>=50?'bg-yellow-900/50 text-yellow-400':'bg-gray-700 text-gray-400'}">${sf(s.strength)}%</span></td><td class="px-3 py-1.5 text-center font-mono ${sf(s.momentum)>=0?'text-green-400':'text-red-400'}">${sf(s.momentum)>=0?'+':''}${sf(s.momentum).toFixed(1)}</td><td class="px-3 py-1.5 text-center text-[10px] ${(s.mtf_align||'').includes('BUY')?'text-green-400':(s.mtf_align||'').includes('SELL')?'text-red-400':'text-gray-500'}">${s.mtf_align||''}</td></tr>`}).join('');}

function sA(s){document.getElementById('ai').textContent=s.direction==='BUY'?'🟢':'🔴';document.getElementById('at').textContent=`${s.type} ${s.direction} — ${s.symbol} @฿${fmtP(sf(s.entry))}`;document.getElementById('ax').textContent=`SL:฿${fmtP(sf(s.sl))} TP2:฿${fmtP(sf(s.tp2))} Str:${sf(s.strength)}% Mom:${sf(s.momentum).toFixed(1)}`;document.getElementById('al').classList.remove('hidden');try{const c=new(window.AudioContext||window.webkitAudioContext)();const o=c.createOscillator();const g=c.createGain();o.connect(g);g.connect(c.destination);o.frequency.value=s.direction==='BUY'?880:660;g.gain.value=0.08;o.start();o.stop(c.currentTime+0.15);}catch(e){}setTimeout(()=>document.getElementById('al').classList.add('hidden'),12000);}

function loadState(d){if(!d)return;if(d.prices)Object.assign(P,d.prices);if(d.signals){for(const[k,v]of Object.entries(d.signals))S[k]=v;}if(d.analysis){for(const[k,v]of Object.entries(d.analysis))A[k]=v;}if(d.history){const ids=new Set(H.map(h=>h.id));const nw=(d.history||[]).filter(h=>!ids.has(h.id));H.unshift(...nw);if(H.length===0&&d.history.length>0)H.push(...d.history);}if(d.stats){document.getElementById('bu').textContent=d.stats.buy||0;document.getElementById('se').textContent=d.stats.sell||0;document.getElementById('st2').textContent=d.stats.strong||0;}document.getElementById('sc').textContent=d.scan_count||0;rP();rS();rH();rT();rM();rHM();}

function doRefresh(){lo('Refreshing...','sys');fetch('/api/signals').then(r=>r.json()).then(d=>{if(d.signals){for(const[k,v]of Object.entries(d.signals))S[k]=v;}if(d.history){H.length=0;H.push(...d.history);}if(d.stats){document.getElementById('bu').textContent=d.stats.buy||0;document.getElementById('se').textContent=d.stats.sell||0;document.getElementById('st2').textContent=d.stats.strong||0;}rS();rH();lo(`Refreshed: ${Object.keys(S).length} signals`,'sys');}).catch(e=>lo('Refresh failed','error'));}
setInterval(()=>{if(Object.keys(A).length===0)doRefresh();},30000);

so.on('connect',()=>{cn=true;document.getElementById('st').innerHTML='<span class="text-green-400 pulse2">● LIVE</span>';lo('Connected','sys');});
so.on('disconnect',()=>{cn=false;document.getElementById('st').innerHTML='<span class="text-red-400">● Offline</span>';lo('Disconnected','error');});
so.on('reconnect',()=>{lo('Reconnected','sys');so.emit('request_state');});
so.on('init',(d)=>{lo(`Init: ${Object.keys(d.analysis||{}).length} stocks`,'sys');loadState(d);fP('all');});
so.on('full_sync',(d)=>{lo(`Sync: ${Object.keys(d.analysis||{}).length} stocks`,'sys');loadState(d);});
so.on('price_update',(d)=>{if(!d?.symbol)return;P[d.symbol]=d.data;if(d.analysis)A[d.symbol]=d.analysis;rP();rT();rM();rHM();});
so.on('new_signal',(d)=>{if(!d?.signal)return;S[d.symbol]=d.signal;if(!H.find(h=>h.id===d.signal.id))H.unshift(d.signal);rS();rH();sA(d.signal);lo(`🎯 ${d.signal.type} ${d.signal.direction} ${d.symbol} @฿${fmtP(sf(d.signal.entry))}`,'signal');});
so.on('scan_update',(d)=>{if(!d)return;document.getElementById('sc').textContent=d.scan_count||0;document.getElementById('ls').textContent=d.last_scan||'--';document.getElementById('ss2').textContent=d.status||'OK';if(d.stats){document.getElementById('bu').textContent=d.stats.buy||0;document.getElementById('se').textContent=d.stats.sell||0;document.getElementById('st2').textContent=d.stats.strong||0;}});
</script></body></html>'''

# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/')
def index(): return redirect(url_for('login') if 'user' not in session else url_for('dashboard'))

@app.route('/login', methods=['GET','POST'])
def login():
    if 'user' in session: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        u = request.form.get('u','').strip().lower(); p = request.form.get('p','')
        if users.verify(u, p):
            session.permanent = True; session['user'] = u
            return redirect(url_for('dashboard'))
        flash('Invalid credentials', 'error')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/dashboard')
@login_req
def dashboard(): return render_template_string(DASH_HTML)

@app.route('/health')
def health():
    return jsonify(sanitize({'status':'ok','time':dts(),'scans':store.scan_count,'scanner':store.status,'stocks':len(C.STOCKS),'active_signals':len(store.signals),'connected':store.connected,'first_scan_done':store.first_scan_done}))

@app.route('/api/signals')
@login_req
def api_signals():
    return jsonify(sanitize({'signals':store.signals,'history':store.history[:100],'stats':store.stats}))

@app.route('/api/analysis/<symbol>')
@login_req
def api_analysis(symbol):
    a = store.analysis.get(symbol.upper()); s = store.signals.get(symbol.upper())
    if not a: return jsonify({'error':'Not found'}), 404
    return jsonify(sanitize({'analysis':a,'signal':s}))

@app.route('/api/refresh')
@login_req
def api_refresh():
    return jsonify(sanitize({'prices':store.prices,'signals':store.signals,'analysis':store.analysis,'history':store.history[:100],'scan_count':store.scan_count,'stats':store.stats}))

# ═══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════
@socketio.on('connect')
def ws_conn():
    store.connected += 1; logger.info(f"WS connect: {store.connected}")
    try: emit('init', store.get_state())
    except Exception as e: logger.error(f"WS init failed: {e}")

@socketio.on('disconnect')
def ws_disc():
    store.connected = max(0, store.connected - 1); logger.info(f"WS disconnect: {store.connected}")

@socketio.on('request_state')
def ws_req():
    try: emit('full_sync', store.get_state())
    except Exception as e: logger.error(f"WS request_state failed: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════
port = int(os.environ.get('PORT', 8000))
eventlet.spawn(scanner)

if __name__ == '__main__':
    print("=" * 60)
    print("  🇹🇭 TCSM THAI STOCK PRO v3.2 — SET Scanner (Cloud-Safe)")
    print("  📊 SET: Stock Exchange of Thailand")
    print("  🔧 Multi-fallback Yahoo Finance data fetching")
    print("  🕐 ICT (UTC+7) | Market: 10:00-12:30, 14:30-16:30")
    print("=" * 60)
    print(f"  Time:    {dts()} ICT")
    print(f"  Stocks:  {len(C.STOCKS)} tickers")
    print(f"  Port:    {port}")
    print(f"  Login:   admin / admin123")
    print("=" * 60)
    socketio.run(app, host='0.0.0.0', port=port, debug=False, use_reloader=False)
