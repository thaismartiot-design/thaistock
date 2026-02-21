"""
TCSM THAI STOCK PRO v3.1 — SET STOCK SCANNER
Production-ready real-time Thai stock signal scanner
Uses: yfinance (free, .BK suffix) + dual TCSM engine + WebSocket
Deploy: gunicorn -k eventlet -w 1 -b 0.0.0.0:8000 app:app
Requirements: pip install flask flask-socketio eventlet yfinance pandas numpy requests

FEATURES:
 - SET50 + Popular Thai stocks (100+ tickers)
 - Thai market session detection (Morning/Lunch/Afternoon)
 - Dual TCSM Engine (Triple Confluence + SmartMomentum)
 - Multi-Timeframe Analysis (1h, 4h, 1D, 1W)
 - Volume, OBV, Divergence, ATR-based risk management
 - Real-time WebSocket + API fallback
 - Full state sync for late-connecting clients
 - NumPy/Pandas type sanitization for JSON safety
"""
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify
from flask_socketio import SocketIO, emit
from datetime import datetime, timezone, timedelta
from functools import wraps
import pandas as pd
import numpy as np
import hashlib, secrets, time, json, os, traceback, logging, math, threading

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
#  TIMEZONE & HELPERS — ICT (Indochina Time, UTC+7)
# ═══════════════════════════════════════════════════════════════════════════════
ICT = timezone(timedelta(hours=7))
def now(): return datetime.now(ICT)
def ts(f="%H:%M:%S"): return now().strftime(f)
def dts(): return now().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════════
#  JSON SANITIZER — Convert numpy/pandas types to native Python
# ═══════════════════════════════════════════════════════════════════════════════
def sanitize(obj):
    """Recursively convert numpy/pandas types to JSON-safe Python natives."""
    if obj is None:
        return None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        if math.isnan(v) or math.isinf(v):
            return 0.0
        return v
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, pd.Timestamp):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(i) for i in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
    return obj


def safe_emit(event, data, **kwargs):
    """Emit with sanitization and error handling"""
    try:
        clean = sanitize(data)
        socketio.emit(event, clean, **kwargs)
    except Exception as e:
        logger.error(f"Emit '{event}' failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG — Thai Stock Market
# ═══════════════════════════════════════════════════════════════════════════════
class C:
    SCAN_INTERVAL = 60
    BATCH_SIZE = 8  # slightly smaller batches for .BK tickers

    # Engine A — Triple Confluence
    STOCH_RSI_K = 3; STOCH_RSI_D = 3; RSI_PD = 14; STOCH_PD = 14
    ENH_K = 21; ENH_D = 3; ENH_SLOW = 5
    MACD_F = 12; MACD_S = 26; MACD_SIG = 9; MACD_NLB = 500
    OB = 80.0; OS = 20.0; NEU_HI = 60.0; NEU_LO = 40.0

    # Engine B — SmartMomentum (tuned for SET)
    SIG_TH = 38; CHG_TH = 14; MOM_SM = 3; SWING_LB = 5
    RSI_W = 18; MACD_W = 18; STOCH_W = 14; CCI_W = 14; WPR_W = 12; ADX_W = 12; MOM_W = 12
    CCI_PD = 20; WPR_PD = 14; ADX_PD = 14; MOM_PD = 14

    # Filters
    TREND_MA = 50; MIN_MOM = 0.4; STRONG_STR = 52.0

    # Risk
    ATR_PD = 14; ATR_SL = 1.5; RR = 2.0
    DIV_BARS = 20; VOL_PD = 20

    # ── Thai Stock Universe ──
    # Yahoo Finance uses .BK suffix for SET stocks
    # We store without suffix internally and add .BK when fetching
    STOCKS = [
        # SET50 — Large Cap
        "ADVANC", "AOT", "AWC", "BANPU", "BBL",
        "BDMS", "BEM", "BGRIM", "BH", "BTS",
        "CBG", "CENTEL", "COM7", "CPALL", "CPF",
        "CPN", "CRC", "DELTA", "EA", "EGCO",
        "GLOBAL", "GPSC", "GULF", "HMPRO", "INTUCH",
        "IVL", "JMART", "JMT", "KBANK", "KCE",
        "KTB", "KTC", "LH", "MINT", "MTC",
        "OR", "OSP", "PTT", "PTTEP", "PTTGC",
        "RATCH", "SAWAD", "SCB", "SCC", "SCGP",
        "TISCO", "TOP", "TRUE", "TTB", "TU",
        # SET100 & Popular — Mid Cap
        "AAV", "AMATA", "ANAN", "AP", "BAM",
        "BAFS", "BCH", "BCP", "BCPG", "BEAUTY",
        "BEC", "BJC", "BPP", "BROOK", "BTG",
        "BTSG", "CHG", "CK", "CKP", "CNT",
        "DOHOME", "DTAC", "ERW", "FORTH", "GFPT",
        "GGC", "GLAND", "GUNKUL", "HANA", "HUMAN",
        "ICC", "ICHI", "IRPC", "ITD", "ITC",
        "JAS", "JASIF", "JKN", "JUBILE", "KAMART",
        "KEX", "KKP", "KSL", "LANNA", "LOXLEY",
        "M", "MAJOR", "MAKRO", "MC", "MEGA",
        "MFEC", "MONO", "MTC", "NER", "NOBLE",
        "NRF", "NYT", "ORI", "PLANB", "PRM",
        "PSL", "PTG", "QH", "RBF", "RS",
        "S", "SAMART", "SAT", "SF", "SGP",
        "SINGER", "SIRI", "SPALI", "SPRC", "STA",
        "STEC", "SUPER", "SYNEX", "TASCO", "TCAP",
        "THAI", "THANI", "TISCO", "TKN", "TMB",
        "TMT", "TQM", "TVO", "UNIQ", "VGI",
        "WHA", "WHAUP", "WORK",
    ]

    # Remove duplicates
    STOCKS = list(dict.fromkeys(STOCKS))

    # Sector mapping for Thai stocks
    SECTOR_MAP = {}
    _energy = {"PTT", "PTTEP", "PTTGC", "TOP", "IRPC", "BANPU", "EGCO", "GPSC",
               "GULF", "RATCH", "EA", "BGRIM", "BCP", "BCPG", "SPRC", "OR",
               "BAFS", "GGC", "BPP", "CKP", "GUNKUL", "SUPER", "LANNA", "PTG"}
    _bank = {"KBANK", "BBL", "SCB", "KTB", "TTB", "TMB", "TISCO", "KKP",
             "TCAP", "BAM", "THANI", "SAWAD"}
    _tech = {"ADVANC", "TRUE", "INTUCH", "DTAC", "JAS", "JASIF", "COM7",
             "DELTA", "HANA", "KCE", "MFEC", "SAMART", "SYNEX", "HUMAN",
             "FORTH", "WORK", "MONO", "PLANB"}
    _property = {"CPN", "CRC", "LH", "AP", "SIRI", "SPALI", "QH", "ORI",
                 "SC", "NOBLE", "ANAN", "AMATA", "WHA", "WHAUP", "HMPRO",
                 "DOHOME", "AWC", "CENTEL", "MINT", "ERW", "SGP", "STEC",
                 "GLAND", "NRF"}
    _health = {"BDMS", "BH", "BCH", "CHG", "RBF", "MEGA", "M"}
    _consumer = {"CPALL", "CPF", "TU", "CBG", "BJC", "MAKRO", "GFPT",
                 "OSP", "TVO", "KSL", "NER", "MC", "BEAUTY", "ICC",
                 "ICHI", "SINGER", "JUBILE", "MAJOR"}
    _transport = {"AOT", "BTS", "BEM", "BTSG", "AAV", "THAI", "KEX",
                  "NYT", "PSL", "TASCO", "SAT", "PRM"}
    _industry = {"SCC", "SCGP", "IVL", "MTC", "KTC", "ITD", "CK",
                 "BROOK", "TKN", "STA", "TMT", "CNT", "SF", "BTG",
                 "KAMART", "LOXLEY"}
    _finance = {"KTC", "MTC", "SAWAD", "JMT", "JMART", "TQM", "S",
                "GLOBAL", "VGI", "RS", "JKN", "BEC", "UNIQ"}

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
    def get_sector(symbol):
        return C.SECTOR_MAP.get(symbol, "Other")

    @staticmethod
    def yf_symbol(symbol):
        """Convert internal symbol to Yahoo Finance .BK symbol"""
        return f"{symbol}.BK"

    STOCK_INFO = {}
    for s in STOCKS:
        STOCK_INFO[s] = {"dec": 2, "pip": 0.01}


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA STORE
# ═══════════════════════════════════════════════════════════════════════════════
class Store:
    def __init__(self):
        self.prices = {}
        self.signals = {}
        self.analysis = {}
        self.history = []
        self.last_scan = "Never"
        self.scan_count = 0
        self.connected = 0
        self.status = "STARTING"
        self.errors = []
        self.stats = {'buy': 0, 'sell': 0, 'strong': 0, 'conf': 0, 'weak': 0}
        self.first_scan_done = False

    def err(self, m):
        self.errors.insert(0, f"[{ts()}] {m}")
        self.errors = self.errors[:50]

    def get_state(self):
        """Get full sanitized state for client sync"""
        return sanitize({
            'prices': self.prices,
            'signals': self.signals,
            'analysis': self.analysis,
            'history': self.history[:100],
            'scan_count': self.scan_count,
            'last_scan': self.last_scan,
            'status': self.status,
            'stats': self.stats,
        })

store = Store()


# ═══════════════════════════════════════════════════════════════════════════════
#  YAHOO FINANCE DATA CLIENT — Thai Stocks (.BK)
# ═══════════════════════════════════════════════════════════════════════════════
class YFClient:
    """Fetch Thai stock data via yfinance with .BK suffix and caching"""

    def __init__(self):
        self._cache = {}
        self._cache_time = {}
        self._lock = threading.Lock()

    def get_history(self, symbol, period="3mo", interval="1h"):
        """Get OHLCV history as DataFrame"""
        yf_sym = C.yf_symbol(symbol)
        cache_key = f"{yf_sym}_{period}_{interval}"
        with self._lock:
            if cache_key in self._cache and time.time() - self._cache_time.get(cache_key, 0) < 45:
                return self._cache[cache_key]

        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_sym)
            df = ticker.history(period=period, interval=interval)
            if df is not None and len(df) >= 20:
                df.columns = [c.lower() for c in df.columns]
                df = df.rename(columns={'stock splits': 'splits'})
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.dropna(subset=['close'])
                with self._lock:
                    self._cache[cache_key] = df
                    self._cache_time[cache_key] = time.time()
                return df
            else:
                logger.warning(f"YF {yf_sym}: insufficient data ({len(df) if df is not None else 0} rows)")
        except Exception as e:
            logger.warning(f"YF {yf_sym}: {e}")
        return None

    def get_daily(self, symbol):
        """Get daily data for MTF analysis"""
        return self.get_history(symbol, period="6mo", interval="1d")

    def get_quote(self, symbol):
        """Get current quote info"""
        yf_sym = C.yf_symbol(symbol)
        cache_key = f"quote_{yf_sym}"
        with self._lock:
            if cache_key in self._cache and time.time() - self._cache_time.get(cache_key, 0) < 30:
                return self._cache[cache_key]
        try:
            import yfinance as yf
            ticker = yf.Ticker(yf_sym)
            info = ticker.fast_info
            quote = {
                'price': float(info.last_price) if hasattr(info, 'last_price') else 0,
                'prev_close': float(info.previous_close) if hasattr(info, 'previous_close') else 0,
                'market_cap': float(info.market_cap) if hasattr(info, 'market_cap') else 0,
                'volume': int(info.last_volume) if hasattr(info, 'last_volume') else 0,
            }
            with self._lock:
                self._cache[cache_key] = quote
                self._cache_time[cache_key] = time.time()
            return quote
        except:
            return None

yfc = YFClient()


# ═══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════
def sma(s, n):
    return s.rolling(n, min_periods=1).mean()

def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=1).mean()

def rma(s, n):
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean()

def calc_rsi(close, period=14):
    d = close.diff()
    g = d.where(d > 0, 0.0).ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()
    l = (-d.where(d < 0, 0.0)).ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()
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
    up = high.diff()
    dn = -low.diff()
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
    direction = np.sign(close.diff())
    return (volume * direction).cumsum()

def calc_vwap(high, low, close, volume):
    tp = (high + low + close) / 3.0
    cum_tp_vol = (tp * volume).cumsum()
    cum_vol = volume.cumsum()
    return cum_tp_vol / (cum_vol + 1e-10)


# ═══════════════════════════════════════════════════════════════════════════════
#  THAI MARKET SESSION (SET Hours: 10:00-12:30, 14:30-16:30 ICT)
# ═══════════════════════════════════════════════════════════════════════════════
def get_market_session():
    n = now()
    h = n.hour
    m = n.minute
    wd = n.weekday()
    t = h * 60 + m  # minutes since midnight

    if wd >= 5:
        return "วันหยุด WEEKEND 🔒", False

    if t < 570:  # before 09:30 (pre-open auction at 09:55)
        return "ก่อนเปิดตลาด PRE-MARKET", False
    elif 570 <= t < 600:  # 09:30-10:00 Pre-open
        return "เปิดจับคู่ PRE-OPEN ☀️", True
    elif 600 <= t < 660:  # 10:00-11:00 Morning open
        return "เช้า MORNING ⭐⭐", True
    elif 660 <= t < 750:  # 11:00-12:30
        return "เช้า MORNING ⭐", True
    elif 750 <= t < 870:  # 12:30-14:30 Lunch break
        return "พักเที่ยง LUNCH BREAK 🍜", False
    elif 870 <= t < 930:  # 14:30-15:30 Afternoon
        return "บ่าย AFTERNOON ⭐", True
    elif 930 <= t < 990:  # 15:30-16:30 Power hour
        return "ช่วงปิด CLOSING ⭐⭐", True
    elif 990 <= t < 1020:  # 16:30-17:00 Call auction / closing
        return "ปิดตลาด CLOSING AUCTION", False
    else:
        return "ปิดตลาด CLOSED 🔒", False


# ═══════════════════════════════════════════════════════════════════════════════
#  TCSM ENGINE — FULL ANALYSIS (Adapted for Thai Stocks)
# ═══════════════════════════════════════════════════════════════════════════════
def analyze_stock(symbol):
    """Complete TCSM dual-engine analysis for one Thai stock"""
    try:
        dec = 2
        sector = C.get_sector(symbol)

        # ── Fetch hourly data ──
        df = yfc.get_history(symbol, period="3mo", interval="1h")
        if df is None or len(df) < 30:
            return None

        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']
        price = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else price
        chg = price - prev
        chg_pct = (chg / prev * 100) if prev else 0

        # Thai stocks can have prices from 0.xx to 1000+
        # Adjust decimal places based on price
        if price < 1:
            dec = 4
        elif price < 10:
            dec = 3
        elif price < 100:
            dec = 2
        else:
            dec = 2

        # ── Daily data for MTF ──
        df_daily = yfc.get_daily(symbol)

        # ═══════════════════════════════════════════════════
        #  ENGINE A: TRIPLE CONFLUENCE
        # ═══════════════════════════════════════════════════

        # Stochastic RSI
        rv = calc_rsi(close, C.RSI_PD)
        rh = rv.rolling(C.STOCH_PD, min_periods=1).max()
        rl = rv.rolling(C.STOCH_PD, min_periods=1).min()
        srK = sma(((rv - rl) / (rh - rl + 1e-10)) * 100.0, C.STOCH_RSI_K)
        srD = sma(srK, C.STOCH_RSI_D)

        # Enhanced Stochastic
        ehh = high.rolling(C.ENH_K, min_periods=1).max()
        ell = low.rolling(C.ENH_K, min_periods=1).min()
        enK = sma(((close - ell) / (ehh - ell + 1e-10)) * 100.0, C.ENH_SLOW)
        enD = sma(enK, C.ENH_D)

        # MACD Normalized
        ml = ema(close, C.MACD_F) - ema(close, C.MACD_S)
        sl = ema(ml, C.MACD_SIG)
        lb = min(len(df), C.MACD_NLB)
        rH = pd.concat([ml.rolling(lb, min_periods=1).max(), sl.rolling(lb, min_periods=1).max()], axis=1).max(axis=1)
        rL = pd.concat([ml.rolling(lb, min_periods=1).min(), sl.rolling(lb, min_periods=1).min()], axis=1).min(axis=1)
        rng = rH - rL
        pH = rH + rng * 0.1
        pL = rL - rng * 0.1
        mnA = ((ml - pL) / (pH - pL + 1e-10) * 100.0).clip(0, 100)
        msA = ((sl - pL) / (pH - pL + 1e-10) * 100.0).clip(0, 100)

        sr_k = float(srK.iloc[-1]); sr_d = float(srD.iloc[-1])
        en_k = float(enK.iloc[-1]); en_d = float(enD.iloc[-1])
        mn_a = float(mnA.iloc[-1]); ms_a = float(msA.iloc[-1])

        def xo(a, b):
            return len(a) >= 2 and a.iloc[-1] > b.iloc[-1] and a.iloc[-2] <= b.iloc[-2]
        def xu(a, b):
            return len(a) >= 2 and a.iloc[-1] < b.iloc[-1] and a.iloc[-2] >= b.iloc[-2]
        def ir(v):
            return v > C.OS and v < C.OB

        bx = sum([
            xo(srK, srD) and ir(sr_k),
            xo(enK, enD) and ir(en_k),
            xo(mnA, msA) and ir(mn_a)
        ])
        sx = sum([
            xu(srK, srD) and ir(sr_k),
            xu(enK, enD) and ir(en_k),
            xu(mnA, msA) and ir(mn_a)
        ])

        # Zone counts
        bull_zn = sum([sr_k > C.NEU_HI, en_k > C.NEU_HI, mn_a > C.NEU_HI])
        bear_zn = sum([sr_k < C.NEU_LO, en_k < C.NEU_LO, mn_a < C.NEU_LO])

        # Trend detection
        ema20 = float(ema(close, 20).iloc[-1])
        ema50 = float(ema(close, 50).iloc[-1])
        ema200_val = float(ema(close, 200).iloc[-1]) if len(close) >= 200 else ema50
        above_ma = price > ema50

        avg_mom_a = float((srK.diff().abs().iloc[-1] + enK.diff().abs().iloc[-1] + mnA.diff().abs().iloc[-1]) / 3.0)
        conf_buy = bool(bx >= 2 and above_ma and avg_mom_a >= C.MIN_MOM)
        conf_sell = bool(sx >= 2 and not above_ma and avg_mom_a >= C.MIN_MOM)

        # Check for near-crossovers (within 3 bars)
        def recent_xo(a, b, bars=3):
            for i in range(1, min(bars + 1, len(a))):
                idx = -i
                pidx = idx - 1
                if abs(pidx) < len(a) and a.iloc[idx] > b.iloc[idx] and a.iloc[pidx] <= b.iloc[pidx]:
                    return True
            return False

        def recent_xu(a, b, bars=3):
            for i in range(1, min(bars + 1, len(a))):
                idx = -i
                pidx = idx - 1
                if abs(pidx) < len(a) and a.iloc[idx] < b.iloc[idx] and a.iloc[pidx] >= b.iloc[pidx]:
                    return True
            return False

        recent_bx = sum([
            recent_xo(srK, srD), recent_xo(enK, enD), recent_xo(mnA, msA)
        ])
        recent_sx = sum([
            recent_xu(srK, srD), recent_xu(enK, enD), recent_xu(mnA, msA)
        ])

        if not conf_buy and recent_bx >= 2 and above_ma:
            conf_buy = True
            bx = max(bx, recent_bx)
        if not conf_sell and recent_sx >= 2 and not above_ma:
            conf_sell = True
            sx = max(sx, recent_sx)

        bx = int(bx)
        sx = int(sx)
        bull_zn = int(bull_zn)
        bear_zn = int(bear_zn)

        # ═══════════════════════════════════════════════════
        #  ENGINE B: SMARTMOMENTUM (7 indicators)
        # ═══════════════════════════════════════════════════
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
        st_raw = float((stv.iloc[-1] - 50) * 2.0)
        stoch_sc = float(np.clip(st_raw + (10 if stv.iloc[-1] > st_sig.iloc[-1] else -10), -100, 100))

        cci_v = float(calc_cci(close, high, low, C.CCI_PD).iloc[-1])
        cci_sc = float(np.clip(cci_v / 2.0, -100, 100))

        wpr_v = float(calc_wpr(close, high, low, C.WPR_PD).iloc[-1])
        wpr_sc = float((wpr_v + 50) * 2.0)

        adx_v, pdi, mdi = calc_adx(high, low, close, C.ADX_PD)
        adx_val = float(adx_v.iloc[-1]); pdi_v = float(pdi.iloc[-1]); mdi_v = float(mdi.iloc[-1])
        adx_sc = float(np.clip(adx_val * 2.0, 0, 100) if pdi_v > mdi_v else np.clip(-adx_val * 2.0, -100, 0))

        mom_pd = min(C.MOM_PD, len(close) - 1)
        mom_v = float(close.iloc[-1] - close.iloc[-mom_pd]) if mom_pd > 0 else 0.0
        mom_pct = float(mom_v / close.iloc[-mom_pd] * 100) if mom_pd > 0 and float(close.iloc[-mom_pd]) != 0 else 0.0
        mom_sc = float(np.clip(mom_pct * 10.0, -100, 100))

        # Composite momentum
        wts = [C.RSI_W, C.MACD_W, C.STOCH_W, C.CCI_W, C.WPR_W, C.ADX_W, C.MOM_W]
        scs = [rsi_sc, macd_sc, stoch_sc, cci_sc, wpr_sc, adx_sc, mom_sc]
        tw = sum(wts)
        momentum = round(sum(s * w for s, w in zip(scs, wts)) / tw, 2) if tw > 0 else 0.0

        # ── Divergence ──
        div = "NONE"
        try:
            if len(df) > C.DIV_BARS:
                rl_ = low.iloc[-C.DIV_BARS:]
                rs_ = stv.iloc[-C.DIV_BARS:]
                if low.iloc[-1] < rl_.min() * 1.001 and stv.iloc[-1] > rs_.min():
                    div = "BULL"
                rh_ = high.iloc[-C.DIV_BARS:]
                if high.iloc[-1] > rh_.max() * 0.999 and stv.iloc[-1] < rs_.max():
                    div = "BEAR"
        except:
            pass

        # ── Volume analysis ──
        va = float(volume.rolling(C.VOL_PD, min_periods=1).mean().iloc[-1])
        vr = round(float(volume.iloc[-1]) / va, 2) if va > 0 else 1.0

        # ── OBV trend ──
        obv = calc_obv(close, volume)
        obv_sma = sma(obv, 20)
        obv_bullish = bool(obv.iloc[-1] > obv_sma.iloc[-1])

        # ── MTF Analysis using daily data ──
        mtf = {}

        # Hourly analysis (current)
        h_rsi = rsi_v
        h_score = 50 + (h_rsi - 50) * 0.8
        mtf['1h'] = {
            'score': round(h_score, 1),
            'summary': 'Buy' if h_score > 55 else ('Sell' if h_score < 45 else 'Neutral')
        }

        if df_daily is not None and len(df_daily) >= 20:
            d_close = df_daily['close']
            d_high = df_daily['high']
            d_low = df_daily['low']

            d_rsi = float(calc_rsi(d_close, 14).iloc[-1])
            d_stoch = float(calc_stoch(d_close, d_high, d_low, 14).iloc[-1])
            d_ema20 = float(ema(d_close, 20).iloc[-1])
            d_ema50 = float(ema(d_close, 50).iloc[-1])

            d_score = (d_rsi - 50) * 0.6 + (25 if float(d_close.iloc[-1]) > d_ema20 else -25) + (15 if float(d_close.iloc[-1]) > d_ema50 else -15)
            d_score = float(50 + np.clip(d_score, -50, 50))

            mtf['1D'] = {
                'score': round(d_score, 1),
                'summary': 'Buy' if d_score > 55 else ('Sell' if d_score < 45 else 'Neutral')
            }

            if len(df_daily) >= 30:
                w_close = d_close.iloc[-5:]
                w_trend = float((w_close.iloc[-1] - w_close.iloc[0]) / w_close.iloc[0] * 100)
                w_score = float(50 + np.clip(w_trend * 10, -40, 40))
                mtf['1W'] = {
                    'score': round(w_score, 1),
                    'summary': 'Buy' if w_score > 55 else ('Sell' if w_score < 45 else 'Neutral')
                }

            if len(close) >= 80:
                h4_close = close.iloc[::4]
                h4_rsi = float(calc_rsi(h4_close, 14).iloc[-1]) if len(h4_close) >= 15 else 50.0
                h4_score = float(50 + (h4_rsi - 50) * 0.7)
                mtf['4h'] = {
                    'score': round(h4_score, 1),
                    'summary': 'Buy' if h4_score > 55 else ('Sell' if h4_score < 45 else 'Neutral')
                }

        for tf in ['1h', '4h', '1D', '1W']:
            if tf not in mtf:
                mtf[tf] = {'score': 50.0, 'summary': 'Neutral'}

        mtf_buy = sum(1 for v in mtf.values() if v['score'] > 55)
        mtf_sell = sum(1 for v in mtf.values() if v['score'] < 45)

        if mtf_buy >= 3:
            mtf_align = "★ ALL BUY"
        elif mtf_sell >= 3:
            mtf_align = "★ ALL SELL"
        elif mtf_buy >= 2:
            mtf_align = "↗ Most Buy"
        elif mtf_sell >= 2:
            mtf_align = "↘ Most Sell"
        else:
            mtf_align = "Mixed"

        htf_score = float(mtf.get('1D', {}).get('score', 50))

        ema_golden = bool(ema20 > ema50)
        trend_label = "UPTREND" if ema_golden and price > ema20 else (
            "DOWNTREND" if not ema_golden and price < ema20 else "SIDEWAYS"
        )

        # ── Signal Strength ──
        am = abs(momentum)
        sig_str = 0.0
        sig_str += 20 if am >= C.SIG_TH else (12 if am >= C.CHG_TH else 5)
        sig_str += min(am * 0.35, 15)

        if (momentum > 0 and conf_buy) or (momentum < 0 and conf_sell):
            sig_str += 20
        if (momentum > 0 and bull_zn >= 2) or (momentum < 0 and bear_zn >= 2):
            sig_str += 10
        if (momentum > 0 and htf_score > 55) or (momentum < 0 and htf_score < 45):
            sig_str += 12
        if div == "BULL" and momentum > 0:
            sig_str += 8
        if div == "BEAR" and momentum < 0:
            sig_str += 8
        if vr > 1.5:
            sig_str += 10
        elif vr > 1.2:
            sig_str += 5
        if (momentum > 0 and obv_bullish) or (momentum < 0 and not obv_bullish):
            sig_str += 5
        if (momentum > 0 and ema_golden) or (momentum < 0 and not ema_golden):
            sig_str += 5

        sig_str = min(sig_str, 100)

        # ── Zone ──
        if momentum >= C.SIG_TH:
            zone = "STRONG BUY"
        elif momentum >= C.CHG_TH:
            zone = "BULLISH"
        elif momentum <= -C.SIG_TH:
            zone = "STRONG SELL"
        elif momentum <= -C.CHG_TH:
            zone = "BEARISH"
        else:
            zone = "NEUTRAL"

        session_name, in_session = get_market_session()

        # ═══════════════════════════════════════════════════
        #  SIGNAL GENERATION
        # ═══════════════════════════════════════════════════
        signal = None
        reasons = []
        direction = None
        sig_type = None

        st_val = float(stv.iloc[-1])
        bull_rev = st_val < 35 and momentum > -C.CHG_TH
        bear_rev = st_val > 65 and momentum < C.CHG_TH

        strong_buy = ((momentum >= C.SIG_TH) or bull_rev or conf_buy) and sig_str >= C.STRONG_STR
        strong_sell = ((momentum <= -C.SIG_TH) or bear_rev or conf_sell) and sig_str >= C.STRONG_STR
        c_buy = conf_buy and not strong_buy
        c_sell = conf_sell and not strong_sell
        w_buy = (momentum >= C.CHG_TH or bull_rev or (bull_zn >= 2 and above_ma)) and not strong_buy and not c_buy
        w_sell = (momentum <= -C.CHG_TH or bear_rev or (bear_zn >= 2 and not above_ma)) and not strong_sell and not c_sell

        if strong_buy:
            direction = "BUY"; sig_type = "STRONG"
            reasons.append("🔥 สัญญาณซื้อแรง Strong Buy")
        elif strong_sell:
            direction = "SELL"; sig_type = "STRONG"
            reasons.append("🔥 สัญญาณขายแรง Strong Sell")
        elif c_buy:
            direction = "BUY"; sig_type = "CONF"
            reasons.append(f"◆ Confluence ซื้อ ({bx}/3)")
        elif c_sell:
            direction = "SELL"; sig_type = "CONF"
            reasons.append(f"◆ Confluence ขาย ({sx}/3)")
        elif w_buy:
            direction = "BUY"; sig_type = "WEAK"
            reasons.append("◇ สัญญาณซื้อเบา Weak Buy")
        elif w_sell:
            direction = "SELL"; sig_type = "WEAK"
            reasons.append("◇ สัญญาณขายเบา Weak Sell")

        if direction:
            if above_ma and direction == "BUY":
                reasons.append("✅ เหนือ EMA50")
            if not above_ma and direction == "SELL":
                reasons.append("✅ ต่ำกว่า EMA50")
            if ema_golden and direction == "BUY":
                reasons.append("✅ Golden Cross")
            if not ema_golden and direction == "SELL":
                reasons.append("✅ Death Cross")
            if bull_zn >= 2 and direction == "BUY":
                reasons.append(f"✅ Bull Zone {bull_zn}/3")
            if bear_zn >= 2 and direction == "SELL":
                reasons.append(f"✅ Bear Zone {bear_zn}/3")
            if div != "NONE":
                reasons.append(f"✅ {div} Divergence")
            if vr > 1.5:
                reasons.append(f"✅ Vol {vr:.1f}x")
            if obv_bullish and direction == "BUY":
                reasons.append("✅ OBV ขาขึ้น")
            if not obv_bullish and direction == "SELL":
                reasons.append("✅ OBV ขาลง")
            if mtf_buy >= 3 and direction == "BUY":
                reasons.append("★ MTF ซื้อทุก TF")
            if mtf_sell >= 3 and direction == "SELL":
                reasons.append("★ MTF ขายทุก TF")
            if in_session:
                reasons.append(f"✅ {session_name}")

            # SL / TP
            if direction == "BUY":
                sl_ = round(price - atr_v * C.ATR_SL, dec)
                risk = price - sl_
                tp1_ = round(price + risk * 1.5, dec)
                tp2_ = round(price + risk * C.RR, dec)
                tp3_ = round(price + risk * 3.0, dec)
            else:
                sl_ = round(price + atr_v * C.ATR_SL, dec)
                risk = sl_ - price
                tp1_ = round(price - risk * 1.5, dec)
                tp2_ = round(price - risk * C.RR, dec)
                tp3_ = round(price - risk * 3.0, dec)

            risk_pct = round(risk / price * 100, 2) if price > 0 else 0.0
            rr = round(abs(price - tp2_) / max(risk, 1e-10), 2)

            signal = {
                'id': f"{symbol}_{direction}_{now().strftime('%H%M%S')}",
                'symbol': symbol, 'sector': sector,
                'direction': direction, 'type': sig_type,
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
                'trend': trend_label,
                'bull_zn': int(bull_zn), 'bear_zn': int(bear_zn),
                'div': div, 'vol': float(vr),
                'htf': round(htf_score, 1),
                'mtf_1h': mtf.get('1h', {}).get('summary', '?'),
                'mtf_4h': mtf.get('4h', {}).get('summary', '?'),
                'mtf_1d': mtf.get('1D', {}).get('summary', '?'),
                'mtf_1w': mtf.get('1W', {}).get('summary', '?'),
                'mtf_align': mtf_align,
                'session': session_name,
                'atr': round(atr_v, dec + 2),
                'risk_pct': float(risk_pct), 'rr': float(rr),
                'reasons': reasons,
                'timestamp': dts(), 'ts_unix': time.time(),
            }

        # ── Return analysis dict ──
        analysis = {
            'symbol': symbol, 'cat': sector,
            'price': round(price, dec), 'prev': round(prev, dec),
            'change': round(chg, dec + 1), 'change_pct': round(chg_pct, 3),
            'momentum': float(momentum), 'strength': round(sig_str),
            'zone': zone, 'session': session_name, 'trend': trend_label,
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
            'mtf': mtf, 'mtf_align': mtf_align,
            'time': ts(),
        }

        return analysis, signal

    except Exception as e:
        logger.error(f"Analyze {symbol}: {e}\n{traceback.format_exc()}")
        store.err(f"{symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════════════════
class Users:
    def __init__(self):
        self.f = 'users.json'
        self.u = self._load()

    def _load(self):
        try:
            if os.path.exists(self.f):
                with open(self.f) as f:
                    return json.load(f)
        except:
            pass
        d = {'admin': {
            'pw': hashlib.sha256(b'admin123_tcsm').hexdigest(),
            'role': 'admin', 'name': 'Admin', 'on': True
        }}
        self._save(d)
        return d

    def _save(self, u=None):
        try:
            with open(self.f, 'w') as f:
                json.dump(u or self.u, f, indent=2)
        except:
            pass

    def verify(self, u, p):
        if u not in self.u:
            return False
        return (self.u[u].get('on', True) and
                self.u[u]['pw'] == hashlib.sha256(f"{p}_tcsm".encode()).hexdigest())

users = Users()

def login_req(f):
    @wraps(f)
    def d(*a, **k):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*a, **k)
    return d


# ═══════════════════════════════════════════════════════════════════════════════
#  SCANNER LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def scanner():
    eventlet.sleep(5)
    logger.info("🚀 TCSM Thai Stock Pro v3.1 Scanner started")
    store.status = "RUNNING"

    while True:
        try:
            store.scan_count += 1
            store.last_scan = ts()
            t0 = time.time()
            ok = 0
            fail = 0

            stocks = list(C.STOCKS)
            for i in range(0, len(stocks), C.BATCH_SIZE):
                batch = stocks[i:i + C.BATCH_SIZE]
                for symbol in batch:
                    try:
                        result = analyze_stock(symbol)
                        if result:
                            analysis, signal = result
                            ok += 1

                            analysis = sanitize(analysis)
                            store.analysis[symbol] = analysis
                            store.prices[symbol] = {
                                'symbol': symbol, 'cat': analysis['cat'],
                                'price': analysis['price'],
                                'change': analysis['change'],
                                'change_pct': analysis['change_pct'],
                                'time': analysis['time'],
                            }

                            safe_emit('price_update', {
                                'symbol': symbol,
                                'data': store.prices[symbol],
                                'analysis': analysis
                            })

                            if signal:
                                signal = sanitize(signal)
                                old = store.signals.get(symbol)
                                is_new = (not old or
                                          old.get('direction') != signal['direction'] or
                                          time.time() - old.get('ts_unix', 0) > 1800)
                                if is_new:
                                    store.signals[symbol] = signal
                                    store.history.insert(0, signal)
                                    store.history = store.history[:500]
                                    store.stats['buy'] += 1 if signal['direction'] == "BUY" else 0
                                    store.stats['sell'] += 1 if signal['direction'] == "SELL" else 0
                                    st = signal['type'].lower()
                                    store.stats[st] = store.stats.get(st, 0) + 1

                                    safe_emit('new_signal', {'symbol': symbol, 'signal': signal})
                                    logger.info(f"🎯 {signal['type']} {signal['direction']} {symbol} @ ฿{signal['entry']} Str:{signal['strength']}%")
                        else:
                            fail += 1
                    except Exception as e:
                        fail += 1
                        store.err(f"{symbol}: {e}")
                        logger.warning(f"Stock {symbol} error: {e}")
                    eventlet.sleep(0.4)  # slightly more delay for .BK tickers
                eventlet.sleep(1.5)

            dur = time.time() - t0
            store.first_scan_done = True

            scan_summary = sanitize({
                'scan_count': store.scan_count, 'last_scan': store.last_scan,
                'stats': store.stats, 'status': store.status,
                'ok': ok, 'fail': fail, 'dur': round(dur, 1),
                'signals': len(store.signals),
            })
            safe_emit('scan_update', scan_summary)
            safe_emit('full_sync', store.get_state())

            logger.info(f"✅ Scan #{store.scan_count}: {ok}/{ok + fail} ({dur:.1f}s) | Signals: {len(store.signals)} | Clients: {store.connected}")

        except Exception as e:
            logger.error(f"Scanner: {e}\n{traceback.format_exc()}")
            store.err(f"Scanner: {e}")

        eventlet.sleep(C.SCAN_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
#  TEMPLATES — Thai-themed UI
# ═══════════════════════════════════════════════════════════════════════════════
LOGIN_HTML = '''<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TCSM Thai Stock Pro — เข้าสู่ระบบ</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
@keyframes gradient{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
body{background:linear-gradient(-45deg,#0a0e1a,#111827,#0f172a,#1a1a2e);background-size:400% 400%;animation:gradient 15s ease infinite;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif}
.card{background:rgba(17,24,39,.85);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.08)}
.glow-btn{position:relative;overflow:hidden;transition:all .3s}
.glow-btn::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:conic-gradient(from 0deg,transparent,rgba(234,179,8,.3),transparent 30%);animation:spin 3s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.glow-btn>span{position:relative;z-index:1;display:block;padding:12px;background:linear-gradient(135deg,#b45309,#d97706);border-radius:8px}
</style>
</head>
<body class="min-h-screen flex items-center justify-center p-4">
<div class="card rounded-2xl shadow-2xl w-full max-w-md p-8">
<div class="text-center mb-8">
<div class="text-6xl mb-4" style="animation:float 3s ease-in-out infinite">🇹🇭</div>
<h1 class="text-3xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-yellow-400 via-amber-400 to-orange-500">TCSM Thai Stock Pro</h1>
<p class="text-gray-500 text-sm mt-1">v3.1 — SET Stock Scanner (หุ้นไทย)</p>
<div class="flex justify-center gap-2 mt-3">
<span class="text-[10px] bg-green-600/20 text-green-400 px-2 py-0.5 rounded-full border border-green-600/30">SET50+</span>
<span class="text-[10px] bg-blue-600/20 text-blue-400 px-2 py-0.5 rounded-full border border-blue-600/30">Dual Engine</span>
<span class="text-[10px] bg-purple-600/20 text-purple-400 px-2 py-0.5 rounded-full border border-purple-600/30">Real-Time</span>
<span class="text-[10px] bg-red-600/20 text-red-400 px-2 py-0.5 rounded-full border border-red-600/30">🇹🇭 Thai Market</span>
</div></div>
{% with m=get_flashed_messages(with_categories=true) %}{% if m %}{% for c,msg in m %}
<div class="mb-4 p-3 rounded-lg text-sm {% if c=='error' %}bg-red-900/50 text-red-300 border border-red-800/50{% else %}bg-green-900/50 text-green-300 border border-green-800/50{% endif %}">{{msg}}</div>
{% endfor %}{% endif %}{% endwith %}
<form method="POST" class="space-y-5">
<div><label class="block text-gray-400 text-xs font-medium mb-1.5 uppercase tracking-wider">ชื่อผู้ใช้ Username</label>
<input type="text" name="u" required class="w-full px-4 py-3 bg-gray-900/60 border border-gray-700/50 rounded-xl text-white placeholder-gray-600 focus:border-yellow-500/50 focus:ring-2 focus:ring-yellow-500/20 focus:outline-none transition" placeholder="กรอกชื่อผู้ใช้"></div>
<div><label class="block text-gray-400 text-xs font-medium mb-1.5 uppercase tracking-wider">รหัสผ่าน Password</label>
<input type="password" name="p" required class="w-full px-4 py-3 bg-gray-900/60 border border-gray-700/50 rounded-xl text-white placeholder-gray-600 focus:border-yellow-500/50 focus:ring-2 focus:ring-yellow-500/20 focus:outline-none transition" placeholder="กรอกรหัสผ่าน"></div>
<div class="glow-btn rounded-xl"><span class="text-center font-bold text-white cursor-pointer"><button type="submit" class="w-full">🔓 เข้าสู่ระบบ Sign In</button></span></div>
</form>
<p class="mt-6 text-center text-gray-600 text-xs">Default: admin / admin123</p>
</div></body></html>'''


DASH_HTML = '''<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🇹🇭 TCSM Thai Stock Pro v3.1 — หุ้นไทย</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
*{scrollbar-width:thin;scrollbar-color:#1f2937 transparent}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#374151;border-radius:3px}
@keyframes pulse2{0%,100%{opacity:1}50%{opacity:.4}}.pulse2{animation:pulse2 1.5s infinite}
@keyframes glow{0%,100%{box-shadow:0 0 10px rgba(234,179,8,.2)}50%{box-shadow:0 0 25px rgba(234,179,8,.4)}}.glow{animation:glow 2s infinite}
@keyframes slideUp{from{transform:translateY(15px);opacity:0}to{transform:translateY(0);opacity:1}}.slideUp{animation:slideUp .35s ease-out}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}.fadeIn{animation:fadeIn .5s ease-out}
@keyframes loading{0%{transform:translateX(-100%)}50%{transform:translateX(0%)}100%{transform:translateX(100%)}}
.glass{background:rgba(17,24,39,.75);backdrop-filter:blur(12px);border:1px solid rgba(55,65,81,.35)}
.glass-card{background:rgba(17,24,39,.6);backdrop-filter:blur(8px);border:1px solid rgba(55,65,81,.3);transition:all .2s}
.glass-card:hover{border-color:rgba(234,179,8,.2);background:rgba(17,24,39,.8)}
.stat-card{background:linear-gradient(135deg,rgba(17,24,39,.8),rgba(31,41,55,.6));border:1px solid rgba(55,65,81,.3)}
.signal-buy{border-left:4px solid #22c55e;background:linear-gradient(135deg,rgba(22,101,52,.08),transparent)}
.signal-sell{border-left:4px solid #ef4444;background:linear-gradient(135deg,rgba(127,29,29,.08),transparent)}
.tag{display:inline-flex;align-items:center;padding:1px 6px;border-radius:4px;font-size:9px;font-weight:600}
body{background:#080c14;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif}
</style>
</head>
<body class="text-gray-200 min-h-screen text-[13px]">
<div class="max-w-[1700px] mx-auto px-3 py-3">

<!-- HEADER -->
<div class="flex flex-wrap justify-between items-center mb-3 gap-2">
<div>
<div class="flex items-center gap-3">
<h1 class="text-lg md:text-xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-yellow-400 to-amber-500">🇹🇭 TCSM Thai Stock Pro</h1>
<span class="tag bg-green-600/20 text-green-400 border border-green-600/30">v3.1</span>
<span class="tag bg-blue-600/20 text-blue-400 border border-blue-600/30">SET หุ้นไทย</span>
<span class="tag bg-red-600/20 text-red-400 border border-red-600/30">100+ TICKERS</span>
<span class="tag bg-purple-600/20 text-purple-400 border border-purple-600/30">REAL-TIME</span>
</div>
<p class="text-gray-600 text-[10px] mt-0.5">Engine A (StochRSI+EnhStoch+MACD) • Engine B (7-Indicator Weighted) • MTF • Divergence • OBV • Volume | ตลาดหลักทรัพย์แห่งประเทศไทย</p>
</div>
<div class="flex items-center gap-3">
<div id="clk" class="glass px-3 py-1.5 rounded-lg font-mono text-sm font-bold text-yellow-400">--:--:--</div>
<span id="st" class="text-red-400 text-sm">● Offline</span>
<button onclick="doRefresh()" class="px-3 py-1.5 bg-blue-600/60 hover:bg-blue-500 rounded-lg text-xs transition font-medium" title="รีเฟรชข้อมูล">🔄</button>
<a href="/logout" class="px-3 py-1.5 bg-red-600/60 hover:bg-red-500 rounded-lg text-xs transition font-medium">ออกจากระบบ</a>
</div></div>

<!-- STATS BAR -->
<div class="grid grid-cols-4 md:grid-cols-8 gap-2 mb-3">
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase tracking-wider">รอบสแกน</div><div id="sc" class="font-bold text-lg">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase tracking-wider">สัญญาณ</div><div id="si" class="font-bold text-lg text-yellow-400">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase tracking-wider">Strong</div><div id="st2" class="font-bold text-lg text-amber-400">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase tracking-wider">ซื้อ Buy</div><div id="bu" class="font-bold text-lg text-green-400">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase tracking-wider">ขาย Sell</div><div id="se" class="font-bold text-lg text-red-400">0</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase tracking-wider">ช่วงเวลา</div><div id="ss" class="text-yellow-400 font-bold text-xs">—</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase tracking-wider">สแกนล่าสุด</div><div id="ls" class="font-mono text-xs">--:--</div></div>
<div class="stat-card rounded-xl p-2.5 text-center"><div class="text-gray-500 text-[10px] uppercase tracking-wider">สถานะ</div><div id="ss2" class="text-blue-400 font-bold text-xs">INIT</div></div>
</div>

<!-- ALERT BANNER -->
<div id="al" class="hidden mb-3"><div class="bg-gradient-to-r from-yellow-900/60 to-amber-900/40 border border-yellow-600/50 rounded-xl p-4 glow">
<div class="flex items-center gap-3"><span class="text-3xl" id="ai">🎯</span>
<div class="flex-1"><div id="at" class="font-bold text-yellow-300 text-sm"></div><div id="ax" class="text-yellow-200/70 text-xs mt-0.5"></div></div>
<button onclick="this.closest('#al').classList.add('hidden')" class="text-yellow-400/50 hover:text-white text-xl transition">✕</button></div></div></div>

<!-- MAIN GRID -->
<div class="grid grid-cols-1 xl:grid-cols-12 gap-3">

<!-- LEFT: STOCK LIST -->
<div class="xl:col-span-3 space-y-3">
<div class="glass rounded-xl p-3">
<div class="flex justify-between items-center mb-2">
<h2 class="font-bold text-sm flex items-center gap-1.5">💹 หุ้นไทย <span id="ps" class="text-[9px] text-gray-600 font-normal">กำลังโหลด</span></h2>
<input id="search" type="text" placeholder="ค้นหา..." class="bg-gray-800/60 border border-gray-700/50 rounded-lg px-2 py-1 text-xs w-24 focus:outline-none focus:border-yellow-500/50" oninput="rP()">
</div>
<div class="flex flex-wrap gap-1 mb-2 text-[10px]">
<button onclick="fP('all')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-yellow-600" data-c="all">ทั้งหมด</button>
<button onclick="fP('Energy')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-gray-700" data-c="Energy">พลังงาน</button>
<button onclick="fP('Banking')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-gray-700" data-c="Banking">ธนาคาร</button>
<button onclick="fP('Tech')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-gray-700" data-c="Tech">เทคโนโลยี</button>
<button onclick="fP('Property')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-gray-700" data-c="Property">อสังหา</button>
<button onclick="fP('Health')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-gray-700" data-c="Health">สุขภาพ</button>
<button onclick="fP('Consumer')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-gray-700" data-c="Consumer">อุปโภค</button>
<button onclick="fP('Transport')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-gray-700" data-c="Transport">ขนส่ง</button>
<button onclick="fP('Industry')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-gray-700" data-c="Industry">อุตสาหกรรม</button>
<button onclick="fP('Finance')" class="px-2 py-0.5 rounded-lg pf transition font-medium bg-gray-700" data-c="Finance">การเงิน</button>
</div>
<div id="pr" class="space-y-1 max-h-[600px] overflow-y-auto pr-1"></div>
</div></div>

<!-- CENTER: SIGNALS -->
<div class="xl:col-span-6 space-y-3">
<div class="flex justify-between items-center">
<h2 class="font-bold text-sm">🎯 สัญญาณซื้อขาย Active Signals</h2>
<div class="flex gap-1 text-[10px]">
<button onclick="fS('all')" class="px-2 py-0.5 rounded-lg sf bg-yellow-600 transition" data-c="all">ทั้งหมด</button>
<button onclick="fS('STRONG')" class="px-2 py-0.5 rounded-lg sf bg-gray-700 transition" data-c="STRONG">แรง</button>
<button onclick="fS('CONF')" class="px-2 py-0.5 rounded-lg sf bg-gray-700 transition" data-c="CONF">Confluence</button>
<button onclick="fS('BUY')" class="px-2 py-0.5 rounded-lg sf bg-gray-700 transition" data-c="BUY">ซื้อ</button>
<button onclick="fS('SELL')" class="px-2 py-0.5 rounded-lg sf bg-gray-700 transition" data-c="SELL">ขาย</button>
</div></div>
<div id="sg" class="space-y-2">
<div class="glass rounded-xl p-8 text-center text-gray-500">
<div class="text-4xl mb-3">📡</div>
<div class="text-sm font-medium">กำลังสแกนหุ้นไทย SET 100+ ตัว...</div>
<div class="text-xs mt-1 text-gray-600">Dual Engine • Multi-Timeframe • Volume • OBV • Divergence</div>
<div class="mt-4 flex justify-center"><div class="w-48 h-1 bg-gray-800 rounded-full overflow-hidden"><div class="h-full bg-gradient-to-r from-yellow-600 to-amber-500 rounded-full" style="animation:loading 2s ease-in-out infinite;width:30%"></div></div></div>
</div></div>
</div>

<!-- RIGHT: PANELS -->
<div class="xl:col-span-3 space-y-3">
<div class="glass rounded-xl p-3">
<h3 class="font-bold text-[11px] text-yellow-400 mb-2 flex items-center gap-1">📊 Momentum สูงสุด</h3>
<div id="tm" class="space-y-1 max-h-[200px] overflow-y-auto"></div></div>

<div class="glass rounded-xl p-3">
<h3 class="font-bold text-[11px] text-blue-400 mb-2 flex items-center gap-1">🕐 MTF Alignment</h3>
<div id="mf" class="space-y-1 max-h-[200px] overflow-y-auto"></div></div>

<div class="glass rounded-xl p-3">
<h3 class="font-bold text-[11px] text-green-400 mb-2 flex items-center gap-1">📈 กลุ่มอุตสาหกรรม Sector Heatmap</h3>
<div id="hm" class="grid grid-cols-2 gap-1 text-[10px]"></div></div>

<div class="glass rounded-xl p-3">
<h3 class="font-bold text-[11px] text-purple-400 mb-2 flex items-center gap-1">📡 บันทึก Live Log</h3>
<div id="lg" class="h-32 overflow-y-auto font-mono text-[9px] space-y-0.5"></div></div>
</div></div>

<!-- HISTORY TABLE -->
<div class="glass rounded-xl overflow-hidden mt-3">
<div class="flex justify-between items-center p-3 border-b border-gray-800/50">
<h2 class="font-bold text-sm">📜 ประวัติสัญญาณ Signal History</h2>
<span class="text-[10px] text-gray-500" id="hc">0 สัญญาณ</span></div>
<div class="overflow-x-auto"><table class="w-full text-[11px]">
<thead class="bg-gray-900/60"><tr class="text-gray-500 uppercase text-[9px] tracking-wider">
<th class="px-3 py-2 text-left">เวลา</th><th class="px-3 py-2">หุ้น</th><th class="px-3 py-2">ประเภท</th><th class="px-3 py-2">ทิศทาง</th>
<th class="px-3 py-2 text-right">Entry</th><th class="px-3 py-2 text-right">SL</th>
<th class="px-3 py-2 text-right">TP1</th><th class="px-3 py-2 text-right">TP2</th><th class="px-3 py-2 text-right">TP3</th>
<th class="px-3 py-2">R:R</th><th class="px-3 py-2">Str%</th><th class="px-3 py-2">Mom</th><th class="px-3 py-2">Trend</th><th class="px-3 py-2">MTF</th>
</tr></thead>
<tbody id="ht"><tr><td colspan="14" class="py-6 text-center text-gray-600">รอสัญญาณ...</td></tr></tbody>
</table></div></div>

<div class="mt-3 text-center text-gray-700 text-[9px] pb-4">⚠️ เพื่อการศึกษาเท่านั้น ไม่ใช่คำแนะนำการลงทุน ผลการดำเนินงานในอดีตไม่ได้รับประกันผลในอนาคต | TCSM Thai Stock Pro v3.1</div>
</div>

<script>
const P={},A={},S={},H=[];let cn=false,pf_='all',sf_='all';

const so=io({
    transports:['websocket','polling'],
    reconnection:true,
    reconnectionDelay:1000,
    reconnectionDelayMax:5000,
    reconnectionAttempts:Infinity,
    timeout:30000
});

function uc(){
// Thai time (ICT = UTC+7)
const d=new Date(new Date().toLocaleString("en-US",{timeZone:"Asia/Bangkok"}));
document.getElementById('clk').textContent=d.toLocaleTimeString('en-GB',{hour12:false})+' ICT';
const h=d.getHours(),m=d.getMinutes(),wd=d.getDay(),t=h*60+m;
let s='ปิดตลาด 🔒';
if(wd===0||wd===6)s='วันหยุด 🔒';
else if(t<570)s='ก่อนเปิดตลาด';
else if(t<600)s='เปิดจับคู่ ☀️';
else if(t<660)s='เช้า ⭐⭐';
else if(t<750)s='เช้า ⭐';
else if(t<870)s='พักเที่ยง 🍜';
else if(t<930)s='บ่าย ⭐';
else if(t<990)s='ช่วงปิด ⭐⭐';
else if(t<1020)s='ปิดจับคู่';
document.getElementById('ss').textContent=s;
}
setInterval(uc,1000);uc();

function lo(m,t='info'){
const e=document.getElementById('lg');
const c={signal:'text-green-400',price:'text-blue-300',error:'text-red-400',info:'text-gray-400',sys:'text-purple-400'};
const d=document.createElement('div');d.className=c[t]||'text-gray-400';
d.textContent=`[${new Date().toLocaleTimeString('en-GB',{timeZone:'Asia/Bangkok',hour12:false})}] ${m}`;
e.insertBefore(d,e.firstChild);while(e.children.length>80)e.removeChild(e.lastChild);
}

function fP(c){pf_=c;document.querySelectorAll('.pf').forEach(b=>{b.classList.toggle('bg-yellow-600',b.dataset.c===c);b.classList.toggle('bg-gray-700',b.dataset.c!==c);});rP();}
function fS(c){sf_=c;document.querySelectorAll('.sf').forEach(b=>{b.classList.toggle('bg-yellow-600',b.dataset.c===c);b.classList.toggle('bg-gray-700',b.dataset.c!==c);});rS();}

function zc(z){return z.includes('BUY')?'text-green-400':z.includes('SELL')?'text-red-400':z.includes('BULL')?'text-green-500/80':z.includes('BEAR')?'text-red-500/80':'text-gray-500';}

function sf(v,d=0){return typeof v==='number'&&isFinite(v)?v:d;}

// Format Thai Baht price
function fmtP(p){
    if(p<1)return p.toFixed(4);
    if(p<10)return p.toFixed(3);
    return p.toFixed(2);
}

function rP(){
let h='',n=0;const q=(document.getElementById('search')?.value||'').toUpperCase();
const syms=Object.keys(A).sort((a,b)=>Math.abs(sf(A[b]?.momentum))-Math.abs(sf(A[a]?.momentum)));
for(const s of syms){const a=A[s];if(!a)continue;
if(pf_!=='all'&&a.cat!==pf_)continue;
if(q&&!s.includes(q))continue;
n++;
const cc=sf(a.change_pct)>=0?'text-green-400':'text-red-400';
const mc=sf(a.momentum)>=0?'text-green-400':'text-red-400';
const hs=S[s]?`<span class="w-2 h-2 rounded-full inline-block ${S[s].direction==='BUY'?'bg-green-400':'bg-red-400'} pulse2"></span>`:'';
const bw=Math.min(Math.abs(sf(a.momentum)),100);
h+=`<div class="glass-card rounded-lg p-2 ${S[s]?'border-yellow-600/30':''}">
<div class="flex justify-between items-center">
<div class="flex items-center gap-1.5">${hs}<span class="font-bold text-sm">${s}</span><span class="text-[9px] px-1 py-0.5 rounded bg-gray-800/60 text-gray-500">${a.cat||''}</span></div>
<div class="text-right"><span class="font-bold">฿${fmtP(sf(a.price))}</span> <span class="${cc} text-[10px]">${sf(a.change_pct)>=0?'+':''}${sf(a.change_pct).toFixed(2)}%</span></div></div>
<div class="flex justify-between mt-1 text-[9px]">
<span class="${mc} font-medium">Mom:${sf(a.momentum).toFixed(1)}</span>
<span class="${zc(a.zone||'')} font-medium">${a.zone||''}</span>
<span>Str:${sf(a.strength)}%</span>
<span class="text-gray-500">${a.trend||''}</span></div>
<div class="mt-1 bg-gray-800/60 rounded-full h-1 overflow-hidden"><div class="${sf(a.momentum)>=0?'bg-gradient-to-r from-green-600 to-green-400':'bg-gradient-to-r from-red-600 to-red-400'} h-full rounded-full transition-all" style="width:${bw}%"></div></div>
</div>`;
}
if(!n)h='<div class="text-gray-600 text-center py-6 text-[11px]">กำลังสแกนหุ้น...</div>';
document.getElementById('pr').innerHTML=h;
document.getElementById('ps').innerHTML=n>0?`<span class="text-green-400 pulse2">● ${n} ตัว</span>`:'กำลังโหลด';
}

function rT(){
const arr=Object.values(A).sort((a,b)=>Math.abs(sf(b.momentum))-Math.abs(sf(a.momentum))).slice(0,10);
document.getElementById('tm').innerHTML=arr.map(a=>{
const mc=sf(a.momentum)>=0?'text-green-400':'text-red-400';
return`<div class="flex justify-between items-center py-1 border-b border-gray-800/30">
<span class="font-bold text-[11px]">${a.symbol}</span>
<span class="${mc} font-mono font-bold text-[11px]">${sf(a.momentum)>=0?'+':''}${sf(a.momentum).toFixed(1)}</span>
<span class="text-[9px] text-gray-500">${a.zone||''}</span></div>`}).join('')||'<div class="text-gray-600 text-xs">รอข้อมูล...</div>';
}

function rM(){
const arr=Object.values(A).filter(a=>a.mtf_align).sort((a,b)=>Math.abs(sf(b.momentum))-Math.abs(sf(a.momentum)));
document.getElementById('mf').innerHTML=arr.filter(a=>a.mtf_align&&!a.mtf_align.includes('Mixed')).slice(0,10).map(a=>{
const mc=a.mtf_align.includes('BUY')?'text-green-400':a.mtf_align.includes('SELL')?'text-red-400':'text-gray-500';
const mtf=a.mtf||{};
return`<div class="flex justify-between items-center py-1 border-b border-gray-800/30">
<span class="font-bold text-[11px]">${a.symbol}</span>
<span class="${mc} font-bold text-[10px]">${a.mtf_align}</span>
<span class="text-[9px] text-gray-600">${Object.entries(mtf).map(([k,v])=>k[0]+':'+(v.summary||'?')[0]).join(' ')}</span></div>`
}).join('')||'<div class="text-gray-600 text-xs">รอข้อมูล...</div>';
}

function rHM(){
const sectors={};
const sectorTH={'Energy':'พลังงาน','Banking':'ธนาคาร','Tech':'เทคโนโลยี','Property':'อสังหา','Health':'สุขภาพ','Consumer':'อุปโภค','Transport':'ขนส่ง','Industry':'อุตฯ','Finance':'การเงิน','Other':'อื่นๆ'};
Object.values(A).forEach(a=>{
const cat=a.cat||'Other';
if(!sectors[cat])sectors[cat]={sum:0,cnt:0,buy:0,sell:0};
sectors[cat].sum+=sf(a.momentum);sectors[cat].cnt++;
if(S[a.symbol]){S[a.symbol].direction==='BUY'?sectors[cat].buy++:sectors[cat].sell++;}
});
document.getElementById('hm').innerHTML=Object.entries(sectors).map(([k,v])=>{
const avg=(v.sum/v.cnt).toFixed(1);const c=avg>=0?'from-green-900/40 to-green-800/20 border-green-700/30 text-green-400':'from-red-900/40 to-red-800/20 border-red-700/30 text-red-400';
return`<div class="bg-gradient-to-br ${c} border rounded-lg p-2">
<div class="font-bold">${sectorTH[k]||k}</div>
<div class="text-xs font-mono">${avg>=0?'+':''}${avg}</div>
<div class="text-[9px] text-gray-500">${v.cnt} ตัว • ${v.buy}ซื้อ/${v.sell}ขาย</div></div>`}).join('');
}

function rS(){
let list=Object.values(S).sort((a,b)=>sf(b.strength)-sf(a.strength));
if(sf_==='STRONG')list=list.filter(s=>s.type==='STRONG');
else if(sf_==='CONF')list=list.filter(s=>s.type==='CONF');
else if(sf_==='BUY')list=list.filter(s=>s.direction==='BUY');
else if(sf_==='SELL')list=list.filter(s=>s.direction==='SELL');

if(!list.length){document.getElementById('sg').innerHTML=`<div class="glass rounded-xl p-8 text-center text-gray-500"><div class="text-3xl mb-2">📡</div><div class="text-sm">${Object.keys(S).length?'ไม่มีสัญญาณตรงตามตัวกรอง':'กำลังสแกน...'}</div></div>`;document.getElementById('si').textContent=Object.keys(S).length;return;}
document.getElementById('si').textContent=Object.keys(S).length;

document.getElementById('sg').innerHTML=list.slice(0,20).map(s=>{
const ib=s.direction==='BUY';const dc=ib?'text-green-400':'text-red-400';
const cls=ib?'signal-buy':'signal-sell';
const tc=s.type==='STRONG'?'bg-gradient-to-r from-yellow-600 to-amber-500':s.type==='CONF'?'bg-gradient-to-r from-cyan-600 to-blue-500':'bg-gray-600';
const sw=Math.min(sf(s.strength),100);
const sc=sw>=75?'bg-gradient-to-r from-green-600 to-green-400':sw>=50?'bg-gradient-to-r from-yellow-600 to-yellow-400':'bg-gray-600';
const dirTH=ib?'ซื้อ BUY':'ขาย SELL';

return`<div class="glass ${cls} rounded-xl p-4 slideUp">
<div class="flex justify-between items-start mb-3">
<div>
<div class="flex items-center gap-2">
<span class="text-lg font-bold ${dc}">${ib?'🟢':'🔴'} ${s.symbol}</span>
<span class="px-2 py-0.5 rounded-md text-[10px] font-bold text-white ${tc}">${s.type} ${dirTH}</span>
<span class="tag bg-gray-800 text-gray-400 border border-gray-700">${s.sector||''}</span>
</div>
<div class="text-[10px] text-gray-500 mt-0.5">Mom: ${sf(s.momentum).toFixed(1)} • Conf: ${sf(s.conf)}/3 • ${s.trend||''} • RSI: ${sf(s.rsi_v,'?')}</div>
</div>
<div class="text-right text-[10px] text-gray-500">${s.timestamp||''}<br><span class="font-bold ${(s.mtf_align||'').includes('BUY')?'text-green-400':(s.mtf_align||'').includes('SELL')?'text-red-400':'text-gray-500'}">${s.mtf_align||''}</span></div>
</div>

<div class="grid grid-cols-5 gap-1.5 mb-3">
<div class="bg-gray-900/60 rounded-lg p-2 text-center"><div class="text-[9px] text-gray-500 uppercase">จุดเข้า</div><div class="font-bold text-sm">฿${fmtP(sf(s.entry))}</div></div>
<div class="bg-red-900/20 rounded-lg p-2 text-center border border-red-900/30"><div class="text-[9px] text-red-400 uppercase">Stop Loss</div><div class="font-bold text-sm text-red-400">฿${fmtP(sf(s.sl))}</div></div>
<div class="bg-green-900/15 rounded-lg p-2 text-center border border-green-900/25"><div class="text-[9px] text-green-400 uppercase">TP1</div><div class="font-bold text-sm text-green-400">฿${fmtP(sf(s.tp1))}</div></div>
<div class="bg-green-900/20 rounded-lg p-2 text-center border border-green-800/30"><div class="text-[9px] text-green-300 uppercase">TP2</div><div class="font-bold text-sm text-green-300">฿${fmtP(sf(s.tp2))}</div></div>
<div class="bg-green-900/25 rounded-lg p-2 text-center border border-green-700/35"><div class="text-[9px] text-green-200 uppercase">TP3</div><div class="font-bold text-sm text-green-200">฿${fmtP(sf(s.tp3))}</div></div>
</div>

<div class="flex items-center gap-3 mb-2">
<span class="text-[10px] text-gray-500">ความแรง</span>
<div class="flex-1 bg-gray-800/60 rounded-full h-1.5 overflow-hidden"><div class="${sc} h-full rounded-full transition-all" style="width:${sw}%"></div></div>
<span class="font-bold text-[11px]">${sf(s.strength)}%</span>
<span class="text-gray-700">|</span>
<span class="text-[10px]">R:R <b>${sf(s.rr)}:1</b></span>
<span class="text-gray-700">|</span>
<span class="text-[10px]">Risk <b>${sf(s.risk_pct)}%</b></span>
</div>

<div class="flex flex-wrap gap-1">${(s.reasons||[]).slice(0,10).map(r=>`<span class="tag bg-gray-800/80 text-gray-300 border border-gray-700/40">${r}</span>`).join('')}</div>
</div>`;}).join('');
}

function rH(){
if(!H.length){document.getElementById('ht').innerHTML='<tr><td colspan="14" class="py-6 text-center text-gray-600">รอสัญญาณ...</td></tr>';return;}
document.getElementById('hc').textContent=H.length+' สัญญาณ';
document.getElementById('ht').innerHTML=H.slice(0,80).map(s=>{
const dc=s.direction==='BUY'?'text-green-400 bg-green-900/30':'text-red-400 bg-red-900/30';
const tc=s.type==='STRONG'?'text-yellow-400':s.type==='CONF'?'text-cyan-400':'text-gray-400';
const dirTH=s.direction==='BUY'?'ซื้อ':'ขาย';
return`<tr class="border-t border-gray-800/30 hover:bg-gray-800/20 transition">
<td class="px-3 py-1.5 text-gray-500 text-[10px]">${s.timestamp||''}</td>
<td class="px-3 py-1.5 font-bold text-center">${s.symbol}</td>
<td class="px-3 py-1.5 text-center ${tc} font-bold">${s.type}</td>
<td class="px-3 py-1.5 text-center"><span class="px-2 py-0.5 rounded-md ${dc} font-bold text-[10px]">${dirTH}</span></td>
<td class="px-3 py-1.5 text-right font-mono">฿${fmtP(sf(s.entry))}</td>
<td class="px-3 py-1.5 text-right font-mono text-red-400">฿${fmtP(sf(s.sl))}</td>
<td class="px-3 py-1.5 text-right font-mono text-green-400">฿${fmtP(sf(s.tp1))}</td>
<td class="px-3 py-1.5 text-right font-mono text-green-300">฿${fmtP(sf(s.tp2))}</td>
<td class="px-3 py-1.5 text-right font-mono text-green-200">฿${fmtP(sf(s.tp3))}</td>
<td class="px-3 py-1.5 font-bold text-center">${sf(s.rr)}:1</td>
<td class="px-3 py-1.5 text-center"><span class="px-1.5 py-0.5 rounded ${sf(s.strength)>=75?'bg-green-900/50 text-green-400':sf(s.strength)>=50?'bg-yellow-900/50 text-yellow-400':'bg-gray-700 text-gray-400'}">${sf(s.strength)}%</span></td>
<td class="px-3 py-1.5 text-center font-mono ${sf(s.momentum)>=0?'text-green-400':'text-red-400'}">${sf(s.momentum)>=0?'+':''}${sf(s.momentum).toFixed(1)}</td>
<td class="px-3 py-1.5 text-center text-[10px]">${s.trend||'—'}</td>
<td class="px-3 py-1.5 text-center text-[10px] font-medium ${(s.mtf_align||'').includes('BUY')?'text-green-400':(s.mtf_align||'').includes('SELL')?'text-red-400':'text-gray-500'}">${s.mtf_align||''}</td>
</tr>`}).join('');
}

function sA(s){
const dirTH=s.direction==='BUY'?'ซื้อ BUY':'ขาย SELL';
document.getElementById('ai').textContent=s.direction==='BUY'?'🟢':'🔴';
document.getElementById('at').textContent=`${s.type} ${dirTH} — ${s.symbol} @ ฿${fmtP(sf(s.entry))}`;
document.getElementById('ax').textContent=`SL:฿${fmtP(sf(s.sl))} TP2:฿${fmtP(sf(s.tp2))} Str:${sf(s.strength)}% Mom:${sf(s.momentum).toFixed(1)} ${s.mtf_align||''}`;
document.getElementById('al').classList.remove('hidden');
try{const c=new(window.AudioContext||window.webkitAudioContext)();const o=c.createOscillator();const g=c.createGain();o.connect(g);g.connect(c.destination);o.frequency.value=s.direction==='BUY'?880:660;g.gain.value=0.08;o.start();o.stop(c.currentTime+0.15);}catch(e){}
setTimeout(()=>document.getElementById('al').classList.add('hidden'),12000);
}

function loadState(d){
    if(!d)return;
    if(d.prices)Object.assign(P,d.prices);
    if(d.signals){for(const[k,v]of Object.entries(d.signals)){S[k]=v;}}
    if(d.analysis){for(const[k,v]of Object.entries(d.analysis)){A[k]=v;}}
    if(d.history){
        const existingIds=new Set(H.map(h=>h.id));
        const newItems=(d.history||[]).filter(h=>!existingIds.has(h.id));
        H.unshift(...newItems);
        if(H.length===0 && d.history.length>0) H.push(...d.history);
    }
    if(d.stats){
        document.getElementById('bu').textContent=d.stats.buy||0;
        document.getElementById('se').textContent=d.stats.sell||0;
        document.getElementById('st2').textContent=d.stats.strong||0;
    }
    document.getElementById('sc').textContent=d.scan_count||0;
    rP();rS();rH();rT();rM();rHM();
}

function doRefresh(){
    lo('รีเฟรชผ่าน API...','sys');
    fetch('/api/signals').then(r=>r.json()).then(d=>{
        if(d.signals){for(const[k,v]of Object.entries(d.signals)){S[k]=v;}}
        if(d.history){H.length=0;H.push(...d.history);}
        if(d.stats){
            document.getElementById('bu').textContent=d.stats.buy||0;
            document.getElementById('se').textContent=d.stats.sell||0;
            document.getElementById('st2').textContent=d.stats.strong||0;
        }
        rS();rH();
        lo(`รีเฟรชแล้ว: ${Object.keys(S).length} สัญญาณ`,'sys');
    }).catch(e=>lo('รีเฟรชล้มเหลว: '+e,'error'));
}

setInterval(()=>{
    if(Object.keys(A).length===0 || Object.keys(S).length===0){
        lo('Auto-sync: ดึงข้อมูลผ่าน API...','sys');
        doRefresh();
    }
},30000);

// Socket handlers
so.on('connect',()=>{
    cn=true;
    document.getElementById('st').innerHTML='<span class="text-green-400 pulse2">● เชื่อมต่อแล้ว LIVE</span>';
    lo('เชื่อมต่อ TCSM Thai Stock Pro v3.1','sys');
});
so.on('disconnect',()=>{
    cn=false;
    document.getElementById('st').innerHTML='<span class="text-red-400">● ขาดการเชื่อมต่อ</span>';
    lo('ขาดการเชื่อมต่อ','error');
});
so.on('reconnect_attempt',(n)=>{
    document.getElementById('st').innerHTML=`<span class="text-yellow-400 pulse2">● เชื่อมต่อใหม่(${n})</span>`;
});
so.on('reconnect',()=>{
    lo('เชื่อมต่อใหม่ — ขอข้อมูล...','sys');
    so.emit('request_state');
});

so.on('init',(d)=>{
    lo(`Init: ${Object.keys(d.analysis||{}).length} หุ้น, ${Object.keys(d.signals||{}).length} สัญญาณ`,'sys');
    loadState(d);
    fP('all');
});

so.on('full_sync',(d)=>{
    lo(`Full sync: ${Object.keys(d.analysis||{}).length} หุ้น, ${Object.keys(d.signals||{}).length} สัญญาณ`,'sys');
    loadState(d);
});

so.on('price_update',(d)=>{
    if(!d?.symbol)return;
    P[d.symbol]=d.data;
    if(d.analysis)A[d.symbol]=d.analysis;
    rP();rT();rM();rHM();
});

so.on('new_signal',(d)=>{
    if(!d?.signal)return;
    S[d.symbol]=d.signal;
    if(!H.find(h=>h.id===d.signal.id)){H.unshift(d.signal);}
    rS();rH();sA(d.signal);
    const dirTH=d.signal.direction==='BUY'?'ซื้อ':'ขาย';
    lo(`🎯 ${d.signal.type} ${dirTH} ${d.symbol} @฿${fmtP(sf(d.signal.entry))} Str:${sf(d.signal.strength)}%`,'signal');
});

so.on('scan_update',(d)=>{
    if(!d)return;
    document.getElementById('sc').textContent=d.scan_count||0;
    document.getElementById('ls').textContent=d.last_scan||'--';
    document.getElementById('ss2').textContent=d.status||'OK';
    if(d.stats){
        document.getElementById('bu').textContent=d.stats.buy||0;
        document.getElementById('se').textContent=d.stats.sell||0;
        document.getElementById('st2').textContent=d.stats.strong||0;
    }
});
</script>
</body></html>'''


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
        'status': 'ok', 'time': dts(),
        'timezone': 'ICT (UTC+7)',
        'market': 'SET (Stock Exchange of Thailand)',
        'scans': store.scan_count, 'scanner': store.status,
        'stocks': len(C.STOCKS), 'active_signals': len(store.signals),
        'connected': store.connected,
        'first_scan_done': store.first_scan_done,
    }))

@app.route('/api/signals')
@login_req
def api_signals():
    return jsonify(sanitize({
        'signals': store.signals,
        'history': store.history[:100],
        'stats': store.stats,
    }))

@app.route('/api/analysis/<symbol>')
@login_req
def api_analysis(symbol):
    a = store.analysis.get(symbol.upper())
    s = store.signals.get(symbol.upper())
    if not a:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(sanitize({'analysis': a, 'signal': s}))

@app.route('/api/refresh')
@login_req
def api_refresh():
    return jsonify(sanitize({
        'prices': store.prices,
        'signals': store.signals,
        'analysis': store.analysis,
        'history': store.history[:100],
        'scan_count': store.scan_count,
        'stats': store.stats,
    }))

@app.route('/api/stocks')
@login_req
def api_stocks():
    """List all monitored stocks with sectors"""
    return jsonify(sanitize({
        'stocks': [{
            'symbol': s,
            'yf_symbol': C.yf_symbol(s),
            'sector': C.get_sector(s),
        } for s in C.STOCKS],
        'total': len(C.STOCKS),
    }))


# ═══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════
@socketio.on('connect')
def ws_conn():
    store.connected += 1
    logger.info(f"WS connect: {store.connected} clients")
    try:
        emit('init', store.get_state())
    except Exception as e:
        logger.error(f"WS init emit failed: {e}")

@socketio.on('disconnect')
def ws_disc():
    store.connected = max(0, store.connected - 1)
    logger.info(f"WS disconnect: {store.connected} clients")

@socketio.on('request_state')
def ws_request_state():
    try:
        emit('full_sync', store.get_state())
    except Exception as e:
        logger.error(f"WS request_state emit failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════
port = int(os.environ.get('PORT', 8000))
eventlet.spawn(scanner)

if __name__ == '__main__':
    print("=" * 65)
    print("  🇹🇭 TCSM THAI STOCK PRO v3.1 — SET Stock Scanner")
    print("  📊 ตลาดหลักทรัพย์แห่งประเทศไทย")
    print("  🔧 Dual Engine + MTF + Volume + OBV + Divergence")
    print("  🕐 เวลาไทย ICT (UTC+7)")
    print("  📈 เปิดตลาด: 10:00-12:30, 14:30-16:30")
    print("=" * 65)
    print(f"  Time:    {dts()} ICT")
    print(f"  Stocks:  {len(C.STOCKS)} tickers (.BK suffix)")
    print(f"  Data:    Yahoo Finance (free, no key needed)")
    print(f"  Port:    {port}")
    print(f"  Login:   admin / admin123")
    print("=" * 65)
    socketio.run(app, host='0.0.0.0', port=port, debug=False, use_reloader=False)