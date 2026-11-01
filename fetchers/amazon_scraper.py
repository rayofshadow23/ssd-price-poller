"""
Amazon.it product scraper for ssd-price-poller.

Safety features:
- one stable Chrome identity (curl_cffi impersonation when available)
- global rate limit with jitter
- short per-ASIN cache (so /check and the 30 min cycle do not double the requests)
- circuit breaker: after a CAPTCHA / 503 / 429 all requests stop for a long pause
- price is read from the main price block of the page, not from the first price found

Return keys are compatible with the old scraper, plus "blocked" (True when Amazon is paused).
"""
import re
import random
import threading
import time
import requests
from typing import Dict, Any, Optional, Tuple

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CFFI = False

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
MIN_INTERVAL_S = 6.0        # minimum time between two Amazon requests
JITTER_S = 4.0              # random extra delay (0..JITTER_S)
CACHE_TTL_S = 600           # same ASIN is not fetched again for 10 min (cycle is 30 min)
BLOCK_BASE_S = 3600         # first pause after a block: 1 hour
BLOCK_MAX_S = 6 * 3600      # longest pause: 6 hours

FALLBACK_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# The main price of the product is inside one of these blocks.
# Prices outside them (sponsored items, "customers also bought") are ignored.
PRICE_BLOCK_IDS = (
    "corePriceDisplay_desktop_feature_div",
    "corePrice_feature_div",
    "corePrice_desktop",
    "apex_desktop",
    "buybox",
)
PRICE_BLOCK_LEN = 6000      # characters read after the start of the block


def parse_price_str(raw: str) -> Optional[float]:
    """Converte una stringa di prezzo italiana/internazionale in float."""
    if not raw:
        return None
    cleaned = raw.replace("€", "").replace("&euro;", "").replace("\xa0", " ").strip()
    m = re.search(r"([0-9.,]+)", cleaned)
    if not m:
        return None
    num_str = m.group(1).strip(".,")
    if not num_str:
        return None
    if "." in num_str and "," in num_str:
        if num_str.find(".") < num_str.find(","):
            num_str = num_str.replace(".", "").replace(",", ".")
        else:
            num_str = num_str.replace(",", "")
    elif "," in num_str:
        num_str = num_str.replace(",", ".")
    elif "." in num_str:
        parts = num_str.split(".")
        if len(parts) == 2 and len(parts[1]) == 3 and int(parts[0]) > 0:
            num_str = num_str.replace(".", "")
    try:
        val = float(num_str)
        return val if val > 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def get_default_session():
    if HAS_CFFI:
        s = cffi_requests.Session(impersonate="chrome")
        s.headers.update({"Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7"})
        return s

    s = requests.Session()
    s.headers.update({
        "User-Agent": FALLBACK_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Chromium";v="131", "Google Chrome";v="131", "Not?A_Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Linux"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    })
    return s


_shared_session = None
_session_lock = threading.Lock()


def get_shared_session():
    global _shared_session
    with _session_lock:
        if _shared_session is None:
            _shared_session = get_default_session()
        return _shared_session


def _reset_shared_session():
    global _shared_session
    with _session_lock:
        _shared_session = None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_next_allowed = 0.0


def _throttle():
    global _next_allowed
    with _rate_lock:
        wait = _next_allowed - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _next_allowed = time.monotonic() + MIN_INTERVAL_S + random.uniform(0, JITTER_S)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
_block_lock = threading.Lock()
_blocked_until = 0.0
_block_strikes = 0


def blocked_seconds_left() -> int:
    """Seconds until Amazon requests are allowed again (0 = not paused)."""
    with _block_lock:
        return max(0, int(_blocked_until - time.monotonic()))


def _trip_breaker() -> int:
    global _blocked_until, _block_strikes
    with _block_lock:
        _block_strikes += 1
        duration = min(BLOCK_BASE_S * (2 ** (_block_strikes - 1)), BLOCK_MAX_S)
        _blocked_until = time.monotonic() + duration
    _reset_shared_session()
    return int(duration)


def _record_success():
    global _block_strikes
    with _block_lock:
        _block_strikes = 0



# ---------------------------------------------------------------------------
# Public IP (logged at startup and on every block, cached 24h)
# ---------------------------------------------------------------------------
_ip_cache = {"ip": None, "at": 0.0}


def get_public_ip() -> str:
    """IP pubblico con cui il container esce su internet. Non passa da Amazon."""
    now = time.monotonic()
    if _ip_cache["ip"] and now - _ip_cache["at"] < 86400:
        return _ip_cache["ip"]
    try:
        ip = requests.get("https://api.ipify.org", params={"format": "text"}, timeout=5).text.strip()
        if not re.fullmatch(r"[0-9a-fA-F:.]{7,45}", ip):
            raise ValueError("risposta non valida")
    except Exception:
        return "sconosciuto"
    _ip_cache.update({"ip": ip, "at": now})
    return ip

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _cache_get(asin: str) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        item = _cache.get(asin)
        if item and (time.monotonic() - item[0]) < CACHE_TTL_S:
            return dict(item[1])
        if item:
            del _cache[asin]
    return None


def _cache_put(asin: str, result: Dict[str, Any]):
    with _cache_lock:
        _cache[asin] = (time.monotonic(), dict(result))
        if len(_cache) > 500:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]
            for key, _ in oldest:
                _cache.pop(key, None)


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------
def _price_region(html: str) -> Tuple[str, bool]:
    """Return (text to search for the price, found_main_block)."""
    for block_id in PRICE_BLOCK_IDS:
        m = re.search(r'id=["\']' + re.escape(block_id) + r'["\']', html)
        if m:
            return html[m.start(): m.start() + PRICE_BLOCK_LEN], True
    return html, False


def _find_price(region: str) -> float:
    # a) screen-reader price inside the block: take the lowest (sale price) when multiple
    prices = []
    for m in re.finditer(r'class=["\'][^"\']*a-offscreen[^"\']*["\']>\s*([0-9.,]+)\s*(?:€|&euro;|&nbsp;€)?\s*<', region):
        p = parse_price_str(m.group(1))
        if p:
            prices.append(p)
    if prices:
        return min(prices)
    # b) whole + fraction
    m_whole = re.search(r'class=["\']a-price-whole["\']>([0-9.,]+)<', region)
    m_frac = re.search(r'class=["\']a-price-fraction["\']>([0-9]+)<', region)
    if m_whole:
        w_str = m_whole.group(1).replace(".", "").replace(",", "").strip()
        f_str = m_frac.group(1).strip() if m_frac else "00"
        try:
            return float(f"{w_str}.{f_str}")
        except ValueError:
            pass
    return 0.0


def _parse_product_html(asin: str, html: str, url: str) -> Dict[str, Any]:
    title = ""
    m_title = re.search(r'id=["\']productTitle["\'][^>]*>\s*([^<]+)\s*<', html)
    if m_title:
        title = m_title.group(1).strip()

    deal_badge = ""
    m_badge = re.search(
        r'(Offerta Black Friday|Black Friday|Offerta a tempo|Offerta del giorno|Offerta lampo|Offerta di primavera)',
        html, re.IGNORECASE)
    if m_badge:
        deal_badge = m_badge.group(0).strip()

    region, found_block = _price_region(html)

    discount_pct = 0.0
    m_disc = re.search(r'class=["\'][^"\']*savingsPercentage[^"\']*["\']>([^<]+)<', region)
    if m_disc:
        disc_str = m_disc.group(1).replace("-", "").replace("%", "").strip()
        try:
            discount_pct = float(disc_str)
        except ValueError:
            pass

    price = _find_price(region)

    is_available = True
    if price == 0.0 and ("Attualmente non disponibile" in html or "Non disponibile" in html):
        is_available = False

    reference_price = 0.0
    reference_label = ""
    m_basis = re.search(
        r'(Prezzo (?:consigliato|più basso recente|mediano))[^0-9€]*([0-9.,]+)(?:\s|&nbsp;)*(?:€|&euro;)',
        region, re.I)
    if m_basis:
        reference_label = m_basis.group(1).strip()
        ref_p = parse_price_str(m_basis.group(2))
        if ref_p:
            reference_price = ref_p

    if reference_price == 0.0:
        m_span_basis = re.search(
            r'class=["\'][^"\']*basisPrice[^"\']*["\'][^>]*>.*?class=["\']a-offscreen["\']>([0-9.,]+)(?:\s|&nbsp;)*(?:€|&euro;)',
            region, re.S | re.I)
        if m_span_basis:
            ref_p = parse_price_str(m_span_basis.group(1))
            if ref_p:
                reference_price = ref_p
                reference_label = "Prezzo consigliato"

    if reference_price > price > 0.0 and discount_pct == 0.0:
        discount_pct = round(((reference_price - price) / reference_price) * 100, 0)

    return {
        "success": price > 0.0,
        "asin": asin,
        "title": title or "Prodotto Amazon",
        "price": price,
        "reference_price": reference_price,
        "reference_label": reference_label,
        "discount_pct": discount_pct,
        "deal_badge": deal_badge,
        "keepa_graph_url": f"https://graph.keepa.com/pricehistory.png?asin={asin}&domain=it",
        "is_available": is_available,
        "url": url,
        "price_from_main_block": found_block,
        "error": None if price > 0.0 else "Prezzo non trovato",
        "blocked": False,
    }


def _fail(asin: str, error: str, blocked: bool = False) -> Dict[str, Any]:
    return {
        "success": False,
        "asin": asin,
        "error": error,
        "price": 0.0,
        "discount_pct": 0.0,
        "deal_badge": "",
        "is_available": False,
        "blocked": blocked,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_amazon_product(asin: str, session=None, max_retries: int = 1) -> Dict[str, Any]:
    """
    Fetch one product page from Amazon.it.
    - cache, global rate limit and circuit breaker are inside this function
    - max_retries applies only to network errors, never to a CAPTCHA
    - when Amazon is paused the result has blocked=True
    """
    cached = _cache_get(asin)
    if cached is not None:
        return cached

    left = blocked_seconds_left()
    if left > 0:
        return _fail(asin, f"Amazon in pausa ancora {left // 60 + 1} min", blocked=True)

    url = f"https://www.amazon.it/dp/{asin}"

    for attempt in range(max_retries + 1):
        s = session or get_shared_session()
        _throttle()

        left = blocked_seconds_left()
        if left > 0:
            return _fail(asin, f"Amazon in pausa ancora {left // 60 + 1} min", blocked=True)

        try:
            resp = s.get(url, timeout=12)
            html = resp.text

            is_block = (
                resp.status_code in (429, 503)
                or "api-services-support@amazon.com" in html
                or "Type the characters you see in this image" in html
                or "/errors/validateCaptcha" in html
            )
            if is_block:
                pause = _trip_breaker()
                print(f"[AMAZON] Blocco/CAPTCHA rilevato. IP pubblico: {get_public_ip()}. Pausa di {pause // 60} minuti.")
                return _fail(asin, "Amazon CAPTCHA", blocked=True)

            if resp.status_code != 200:
                result = _fail(asin, f"HTTP status {resp.status_code}")
                _cache_put(asin, result)
                return result

            _record_success()
            result = _parse_product_html(asin, html, url)
            _cache_put(asin, result)
            return result

        except Exception as e:
            if attempt < max_retries:
                time.sleep(random.uniform(3, 6))
                continue
            return _fail(asin, str(e))

    return _fail(asin, "Errore sconosciuto")
