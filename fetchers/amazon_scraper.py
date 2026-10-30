import re
import random
import time
import requests
from typing import Dict, Any, Optional

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"
]

def parse_price_str(raw: str) -> Optional[float]:
    """Converte una stringa di prezzo italiana/internazionale in float."""
    if not raw:
        return None
    # Rimuovi simboli di valuta, codici html e spazi
    cleaned = raw.replace("€", "").replace("&euro;", "").replace("\xa0", " ").strip()
    
    # Estrai la parte numerica principale
    m = re.search(r"([0-9.,]+)", cleaned)
    if not m:
        return None
    num_str = m.group(1).strip(".,")
    
    if "." in num_str and "," in num_str:
        if num_str.find(".") < num_str.find(","):
            # Formato IT: 1.250,50 -> rimuovi '.', sostituisci ',' con '.'
            num_str = num_str.replace(".", "").replace(",", ".")
        else:
            # Formato US: 1,250.50 -> rimuovi ','
            num_str = num_str.replace(",", "")
    elif "," in num_str:
        # Solo virgola: 139,99 -> 139.99
        num_str = num_str.replace(",", ".")
    elif "." in num_str:
        # Se ci sono 3 cifre dopo l'ultimo punto e nessuna virgola (es. 1.250), è separatore migliaia
        parts = num_str.split(".")
        if len(parts) == 2 and len(parts[1]) == 3 and int(parts[0]) > 0:
            num_str = num_str.replace(".", "")
            
    try:
        val = float(num_str)
        return val if val > 0 else None
    except ValueError:
        return None

def get_default_session() -> requests.Session:
    """Crea una sessione requests configurata con cookie realistici."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Linux"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    })
    return s

_global_session = None

def get_shared_session() -> requests.Session:
    global _global_session
    if _global_session is None:
        _global_session = get_default_session()
    return _global_session

def fetch_amazon_product(asin: str, session: Optional[requests.Session] = None, max_retries: int = 2) -> Dict[str, Any]:
    """
    Effettua lo scraping della pagina prodotto di Amazon.it per l'ASIN specificato.
    Include retry automatico con backoff e rotazione User-Agent se incontra un captcha.
    """
    url = f"https://www.amazon.it/dp/{asin}"
    s = session or get_shared_session()
    
    for attempt in range(max_retries + 1):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        try:
            resp = s.get(url, headers=headers, timeout=12)
            html = resp.text

            # Rilevamento CAPTCHA
            is_captcha = "api-services-support@amazon.com" in html or "Type the characters you see in this image" in html or resp.status_code == 503
            if is_captcha:
                if attempt < max_retries:
                    wait_time = (attempt + 1) * 3
                    time.sleep(wait_time)
                    # Resetta la sessione con un nuovo client
                    s = get_default_session()
                    continue
                return {
                    "success": False,
                    "asin": asin,
                    "error": "Amazon CAPTCHA / Rate limit attivo (riprova più tardi)",
                    "price": 0.0,
                    "is_available": False
                }

            if resp.status_code != 200:
                return {
                    "success": False,
                    "asin": asin,
                    "error": f"HTTP status code: {resp.status_code}",
                    "price": 0.0,
                    "is_available": False
                }

            # 1. Estrazione Titolo
            title = ""
            m_title = re.search(r'id=["\']productTitle["\'][^>]*>\s*([^<]+)\s*<', html)
            if m_title:
                title = m_title.group(1).strip()

            # 2. Estrazione Badge Offerta
            deal_badge = ""
            m_badge = re.search(r'(Offerta Black Friday|Black Friday|Offerta a tempo|Offerta del giorno|Offerta lampo|Offerta di primavera)', html, re.IGNORECASE)
            if m_badge:
                deal_badge = m_badge.group(0).strip()

            # 3. Estrazione Sconto Percentuale
            discount_pct = 0.0
            m_disc = re.search(r'class=["\'][^"\']*savingsPercentage[^"\']*["\']>([^<]+)<', html)
            if m_disc:
                disc_str = m_disc.group(1).replace("-", "").replace("%", "").strip()
                try:
                    discount_pct = float(disc_str)
                except ValueError:
                    pass

            # 4. Estrazione Prezzo BuyBox
            price = 0.0

            m_price_topay = re.search(r'class=["\'][^"\']*aok-offscreen[^"\']*["\']>\s*([0-9.,]+)\s*(?:€|&euro;)', html)
            if m_price_topay:
                p = parse_price_str(m_price_topay.group(1))
                if p:
                    price = p

            if price == 0.0:
                m_whole = re.search(r'class=["\']a-price-whole["\']>([0-9.,]+)<', html)
                m_frac = re.search(r'class=["\']a-price-fraction["\']>([0-9]+)<', html)
                if m_whole:
                    w_str = m_whole.group(1).replace(".", "").replace(",", "").strip()
                    f_str = m_frac.group(1).strip() if m_frac else "00"
                    try:
                        price = float(f"{w_str}.{f_str}")
                    except ValueError:
                        pass

            if price == 0.0:
                m_offscreen = re.search(r'class=["\']a-offscreen["\']>([0-9.,]+)\s*(?:€|&euro;)?<', html)
                if m_offscreen:
                    p = parse_price_str(m_offscreen.group(1))
                    if p:
                        price = p

            is_available = True
            if "Attualmente non disponibile" in html or "Non disponibile" in html:
                if price == 0.0:
                    is_available = False

            return {
                "success": price > 0.0,
                "asin": asin,
                "title": title,
                "price": price,
                "discount_pct": discount_pct,
                "deal_badge": deal_badge,
                "is_available": is_available,
                "url": url,
                "error": None if price > 0.0 else "Prezzo non trovato nella pagina"
            }

        except Exception as e:
            if attempt < max_retries:
                time.sleep(2)
                continue
            return {
                "success": False,
                "asin": asin,
                "error": str(e),
                "price": 0.0,
                "is_available": False
            }
