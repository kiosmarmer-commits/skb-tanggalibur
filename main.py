#!/usr/bin/env python3
"""
Kalender Libur Nasional Indonesia (optimized)
=============================================
- index.json + next_check_after (hemat iLovePDF)
- Hash PDF: skip jika sumber tidak berubah
- Musim cek Sep–Jan untuk tahun terbaru
- MAX_ILOVEPDF_DOCS_PER_RUN
- Jangan timpa JSON complete dengan hasil jelek
- Parser 2 pass + normalisasi nama
- Cache cek JDIH 24 jam
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

try:
    from pdf2image import convert_from_path
    import pytesseract
    from PIL import ImageOps, ImageEnhance, ImageFilter
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.alert import Alert
    from selenium.common.exceptions import (
        UnexpectedAlertPresentException,
        NoAlertPresentException,
        ElementNotInteractableException,
    )
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

# ============================================================
# KONFIGURASI
# ============================================================
LIST_URL = "https://jdih.menpan.go.id/dokumen-hukum/jenis?jenis=keputusan%20bersama%20menteri"
PDF_BASE = "https://data-jdih.menpan.go.id/dokumen"

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FOLDER = os.getenv("GITHUB_FOLDER", "kalender").strip("/")

ILOVEPDF_PUBLIC_KEY = os.getenv("ILOVEPDF_PUBLIC_KEY", "")
ILOVEPDF_SECRET_KEY = os.getenv("ILOVEPDF_SECRET_KEY", "")

MAX_ILOVEPDF_DOCS_PER_RUN = int(os.getenv("MAX_ILOVEPDF_DOCS_PER_RUN", "2"))

MIN_NATIONAL_COMPLETE = 15
MIN_JOINT_COMPLETE = 5
MIN_NATIONAL_SOFT = 12  # lengkap longgar jika tanggal kunci ada
MIN_NATIONAL_REJECT = 8  # di bawah ini jangan timpa complete

RETRY_DAYS_NOT_FOUND = 14
RETRY_DAYS_INCOMPLETE = 7
SEASON_START_MONTH = 9   # Sep
SEASON_END_MONTH = 1     # Jan (tahun berikutnya)
JDIH_CACHE_HOURS = 24

MONTH_MAP = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
    "september": 9, "oktober": 10, "november": 11, "desember": 12,
}

# Normalisasi nama libur (substring OCR → kanonik)
NAME_NORMALIZE = [
    (r"idul\s*fitri|idulfitri", "Idul Fitri"),
    (r"idul\s*adha|iduladha", "Idul Adha"),
    (r"isra\s*mikraj|isra.?mi.?raj", "Isra Mikraj Nabi Muhammad S.A.W."),
    (r"imlek", "Tahun Baru Imlek"),
    (r"nyepi", "Hari Suci Nyepi"),
    (r"wafat\s*(yesus|isa)", "Wafat Yesus Kristus"),
    (r"kebangkitan|paskah", "Kebangkitan Yesus Kristus (Paskah)"),
    (r"hari\s*buruh", "Hari Buruh Internasional"),
    (r"waisak", "Hari Raya Waisak"),
    (r"kenaikan\s*(yesus|isa)", "Kenaikan Yesus Kristus"),
    (r"pancasila", "Hari Lahir Pancasila"),
    (r"muharr?am|tahun\s*baru\s*islam", "1 Muharam Tahun Baru Islam"),
    (r"proklamasi|kemerdekaan", "Proklamasi Kemerdekaan"),
    (r"maulid", "Maulid Nabi Muhammad S.A.W."),
    (r"kelahiran\s*yesus|natal", "Kelahiran Yesus Kristus"),
    (r"tahun\s*baru\s*\d*\s*masehi|tahun\s*baru\s*masehi", "Tahun Baru Masehi"),
]

KEY_DATES_HINTS = [
    r"idul\s*fitri",
    r"proklamasi|17\s*agustus",
    r"kelahiran\s*yesus|natal|25\s*desember",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

KNOWN_PDF_URLS: Dict[int, List[str]] = {
    2027: ["https://data-jdih.menpan.go.id/dokumen/2026skb002.pdf"],
    2026: ["https://data-jdih.menpan.go.id/dokumen/2025skbmenpanrb005.pdf"],
    2025: [
        "https://jdih.kemenkoinfra.go.id/cfind/source/files/keputusan-bersama-3-menteri-nomor-1017-2-2-tahun-2024.pdf",
        "https://www.kemenkopmk.go.id/sites/default/files/artikel/2025-08/SKB%20Perubahan%20Libur%20Nasional%20dan%20Cuti%20Bersama%20Tahun%202025.pdf",
    ],
}

# runtime
_ilovepdf_used = 0
_jdih_cache_path = Path(tempfile.gettempdir()) / "kalender_jdih_cache.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def log_result(year: int, action: str, reason: str, credits: int = 0) -> None:
    print(f"RESULT year={year} action={action} reason={reason} credits_est={credits}")


# ============================================================
# GITHUB
# ============================================================
def _gh_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_url(path: str) -> str:
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FOLDER}/{path}"


def github_enabled() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def github_get_json(filename: str) -> Tuple[Optional[Dict], Optional[str]]:
    if not github_enabled():
        return None, None
    try:
        r = requests.get(
            _gh_url(filename), headers=_gh_headers(),
            params={"ref": GITHUB_BRANCH}, timeout=25,
        )
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        body = r.json()
        raw = base64.b64decode(body["content"]).decode("utf-8")
        return json.loads(raw), body.get("sha")
    except Exception as e:
        print(f"[!] Baca GitHub {filename}: {e}")
        return None, None


def github_put_json(filename: str, data: Dict, message: str) -> bool:
    if not github_enabled():
        print(f"[!] GitHub belum diset → skip {filename}")
        return False
    _, sha = github_get_json(filename)
    payload = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(_gh_url(filename), headers=_gh_headers(), json=payload, timeout=45)
        if r.status_code in (200, 201):
            print(f"[✓] GitHub ← {GITHUB_FOLDER}/{filename}")
            return True
        print(f"[!] Upload HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[!] Upload: {e}")
        return False


def empty_index() -> Dict:
    return {"years": {}, "latest_year": None, "next_check_after": None, "updated_at": iso_now()}


def load_index() -> Dict:
    data, _ = github_get_json("index.json")
    if data:
        return data
    p = Path("index.json")
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return empty_index()


def save_index(index: Dict) -> None:
    index["updated_at"] = iso_now()
    ys = [int(y) for y in (index.get("years") or {}).keys()]
    index["latest_year"] = max(ys) if ys else None
    Path("index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    github_put_json("index.json", index, f"Update index.json ({now_utc().strftime('%Y-%m-%d')})")


# ============================================================
# COMPLETE / HASH / SEASON
# ============================================================
def has_key_holidays(data: Dict) -> bool:
    blob = " ".join(
        h.get("name", "") for h in (data.get("national_holidays") or [])
    ).lower()
    hits = sum(1 for pat in KEY_DATES_HINTS if re.search(pat, blob, re.I))
    return hits >= 2


def is_complete(data: Dict) -> bool:
    n = int(data.get("total_national") or 0)
    j = int(data.get("total_joint_leave") or 0)
    if n >= MIN_NATIONAL_COMPLETE and j >= MIN_JOINT_COMPLETE:
        return True
    if n >= MIN_NATIONAL_SOFT and j >= MIN_JOINT_COMPLETE and has_key_holidays(data):
        return True
    return False


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def in_skb_season(today: Optional[datetime] = None) -> bool:
    """True di Sep–Dec atau Jan (musim rilis SKB tahun berikutnya)."""
    d = (today or now_utc()).date()
    return d.month >= SEASON_START_MONTH or d.month <= SEASON_END_MONTH


def default_next_check(after_success_year: Optional[int] = None) -> str:
    n = now_utc()
    if after_success_year:
        # pantau tahun berikutnya mulai 1 Sep tahun after_success_year
        target = datetime(after_success_year, SEASON_START_MONTH, 1, tzinfo=timezone.utc)
        if target.date() < n.date():
            target = n + timedelta(days=RETRY_DAYS_NOT_FOUND)
        return target.date().isoformat()
    if not in_skb_season(n):
        # lompat ke 1 Sep tahun ini / depan
        y = n.year if n.month < SEASON_START_MONTH else n.year + 1
        return datetime(y, SEASON_START_MONTH, 1, tzinfo=timezone.utc).date().isoformat()
    return (n + timedelta(days=RETRY_DAYS_NOT_FOUND)).date().isoformat()


def should_check_now(index: Dict, force: bool) -> bool:
    if force:
        return True
    nca = index.get("next_check_after")
    if not nca:
        return True
    try:
        s = str(nca)[:10]
        due = datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return True
    return now_utc().date() >= due


def years_to_process(
    index: Dict, year: Optional[int], from_year: Optional[int], force: bool
) -> List[int]:
    cur = now_utc().year
    if year is not None:
        return [year]
    if from_year is not None:
        return list(range(from_year, max(cur + 1, from_year) + 1))

    years_map = index.get("years") or {}
    incomplete = [int(y) for y, m in years_map.items() if not m.get("complete")]
    latest = index.get("latest_year")

    if latest is None:
        targets = [cur, cur + 1]
    else:
        targets = [int(latest) + 1]
        # di luar musim: jangan paksa cek tahun baru kecuali incomplete
        if not in_skb_season() and not force:
            targets = []

    targets = sorted(set(targets + incomplete))
    if not should_check_now(index, force):
        print(f"[*] Belum waktunya (next_check_after={index.get('next_check_after')})")
        print("    Gunakan --year / --from / --force untuk memaksa.")
        return []
    return targets


def needs_scrape(index: Dict, y: int, force: bool) -> bool:
    meta = (index.get("years") or {}).get(str(y))
    if force:
        return True
    if not meta:
        return True
    if not meta.get("complete"):
        return True
    return False


# ============================================================
# JDIH CACHE + PDF RESOLVE
# ============================================================
def load_jdih_cache() -> Dict:
    try:
        if _jdih_cache_path.exists():
            return json.loads(_jdih_cache_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_jdih_cache(cache: Dict) -> None:
    try:
        _jdih_cache_path.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


def jdih_years_from_page() -> Set[int]:
    cache = load_jdih_cache()
    ts = cache.get("ts")
    if ts:
        try:
            age = now_utc() - datetime.fromisoformat(ts)
            if age < timedelta(hours=JDIH_CACHE_HOURS) and cache.get("years"):
                print(f"[*] JDIH cache hit: {cache['years']}")
                return set(cache["years"])
        except Exception:
            pass

    years: Set[int] = set()
    if not HAS_SELENIUM:
        return years

    print("[*] Load daftar JDIH...")
    driver = create_driver()
    try:
        driver.get(LIST_URL)
        time.sleep(3)
        dismiss_alert(driver)
        text = BeautifulSoup(driver.page_source, "html.parser").get_text(" ", strip=True)
        for m in re.finditer(
            r"(?:Hari Libur Nasional dan )?Cuti Bersama\s+Tahun\s+(\d{4})",
            text,
            re.I,
        ):
            years.add(int(m.group(1)))
        for m in re.finditer(
            r"Hari Libur Nasional dan Cuti Bersama\s+Tahun\s+(\d{4})", text, re.I
        ):
            years.add(int(m.group(1)))
        print(f"[+] JDIH years: {sorted(years)}")
        save_jdih_cache({"ts": iso_now(), "years": sorted(years)})
    except Exception as e:
        print(f"[!] JDIH: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return years


def create_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={HEADERS['User-Agent']}")
    options.page_load_strategy = "eager"
    for p in (os.getenv("CHROME_BIN", ""), "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if p and Path(p).exists():
            options.binary_location = p
            break
    dp = None
    for p in (os.getenv("CHROMEDRIVER_PATH", ""), "/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver"):
        if p and Path(p).exists():
            dp = p
            break
    service = Service(executable_path=dp) if dp else Service()
    d = webdriver.Chrome(service=service, options=options)
    d.set_page_load_timeout(25)
    return d


def dismiss_alert(driver):
    try:
        Alert(driver).accept()
        time.sleep(0.2)
    except NoAlertPresentException:
        pass
    except Exception:
        pass


def url_looks_like_pdf(url: str) -> bool:
    try:
        r = requests.head(url, headers=HEADERS, timeout=12, allow_redirects=True)
        if r.status_code != 200:
            r = requests.get(url, headers=HEADERS, timeout=15, stream=True)
        ctype = (r.headers.get("content-type") or "").lower()
        clen = int(r.headers.get("content-length") or 0)
        return r.status_code == 200 and ("pdf" in ctype or "octet" in ctype or clen > 5000)
    except Exception:
        return False


def try_direct_pdf(year: int) -> Optional[str]:
    urls = list(KNOWN_PDF_URLS.get(year, []))
    for name in (
        f"{year-1}skb002.pdf",
        f"{year-1}skbmenpanrb002.pdf",
        f"{year-1}skbmenpanrb005.pdf",
        f"{year}skb002.pdf",
    ):
        urls.append(f"{PDF_BASE}/{name}")
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        if url_looks_like_pdf(url):
            print(f"[+] PDF URL: {url}")
            return url
    return None


def extract_pdf_from_unduh(driver) -> Optional[str]:
    for btn in driver.find_elements(
        By.XPATH, "//button[contains(translate(., 'UNDUH', 'unduh'), 'unduh')]"
    ):
        try:
            attrs = driver.execute_script(
                "var e=arguments[0],o={};for(var a of e.attributes)o[a.name]=a.value;return o;",
                btn,
            )
            val = (attrs.get("@click") or attrs.get("x-on:click") or attrs.get("onclick") or "")
            val = val.replace("\\/", "/")
            m = re.search(r"https?://[^\s'\"<>]+?\.pdf", val)
            if m:
                return m.group(0)
        except Exception:
            continue
    for a in driver.find_elements(By.CSS_SELECTOR, "a[href$='.pdf']"):
        href = a.get_attribute("href") or ""
        if href.endswith(".pdf"):
            return href
    return None


def scrape_pdf_url_selenium(target_year: int) -> Optional[str]:
    if not HAS_SELENIUM:
        return None
    print(f"[*] Selenium PDF tahun {target_year}...")
    driver = create_driver()
    try:
        driver.get(LIST_URL)
        time.sleep(3)
        dismiss_alert(driver)
        for i in range(10):
            buttons = driver.find_elements(
                By.XPATH, "//button[contains(normalize-space(.),'Lihat')]"
            )
            if i >= len(buttons):
                break
            try:
                btn = buttons[i]
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.3)
                try:
                    btn.click()
                except ElementNotInteractableException:
                    driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                dismiss_alert(driver)
                page = BeautifulSoup(driver.page_source, "html.parser").get_text(" ", strip=True)
                if re.search(rf"Cuti Bersama\s+Tahun\s+{target_year}", page, re.I):
                    pdf = extract_pdf_from_unduh(driver)
                    if pdf:
                        print(f"[+] Selenium PDF: {pdf}")
                        return pdf
                driver.get(LIST_URL)
                time.sleep(1.5)
                dismiss_alert(driver)
            except UnexpectedAlertPresentException:
                dismiss_alert(driver)
            except Exception:
                try:
                    driver.get(LIST_URL)
                    time.sleep(1.5)
                except Exception:
                    pass
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def resolve_pdf_urls(year: int) -> List[str]:
    urls: List[str] = []
    for u in KNOWN_PDF_URLS.get(year, []):
        if u not in urls and url_looks_like_pdf(u):
            urls.append(u)
    u = try_direct_pdf(year)
    if u and u not in urls:
        urls.append(u)
    if not urls:
        u2 = scrape_pdf_url_selenium(year)
        if u2 and u2 not in urls:
            urls.append(u2)
    return urls


# ============================================================
# DOWNLOAD + OCR
# ============================================================
def download_pdf(pdf_url: str) -> Path:
    print(f"[*] Download PDF...")
    resp = requests.get(pdf_url, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    name = pdf_url.split("/")[-1].split("?")[0] or "skb.pdf"
    if not name.endswith(".pdf"):
        name += ".pdf"
    tmp = Path(tempfile.gettempdir()) / f"skb_{name}"
    tmp.write_bytes(resp.content)
    print(f"[+] {len(resp.content):,} bytes, sha256={file_sha256(tmp)[:12]}...")
    return tmp


def ocr_via_ilovepdf(pdf_path: Path) -> Optional[str]:
    global _ilovepdf_used
    if not ILOVEPDF_PUBLIC_KEY or not ILOVEPDF_SECRET_KEY:
        print("[*] iLovePDF key belum diset")
        return None
    if _ilovepdf_used >= MAX_ILOVEPDF_DOCS_PER_RUN:
        print(f"[!] Batas MAX_ILOVEPDF_DOCS_PER_RUN={MAX_ILOVEPDF_DOCS_PER_RUN} tercapai")
        return None
    try:
        from ilovepdf import PdfOcrTask, ExtractTask
    except ImportError:
        print("[*] ilovepdf package belum terpasang")
        return None

    out_dir = Path(tempfile.mkdtemp(prefix="ilovepdf_"))
    try:
        print("[*] iLovePDF OCR (ind+eng)...")
        task = PdfOcrTask(ILOVEPDF_PUBLIC_KEY, ILOVEPDF_SECRET_KEY)
        f = task.add_file(str(pdf_path))
        try:
            f.ocr_languages = ["ind", "eng"]
        except Exception:
            try:
                f.ocr_languages = "ind"
            except Exception:
                pass
        task.execute()
        task.set_output_filename("ocr_result.pdf")
        task.download(str(out_dir))
        ocr_pdf = out_dir / "ocr_result.pdf"
        if not ocr_pdf.exists():
            pdfs = list(out_dir.glob("*.pdf"))
            if not pdfs:
                return None
            ocr_pdf = pdfs[0]
        _ilovepdf_used += 1  # PdfOcrTask

        print("[*] iLovePDF Extract...")
        ext = ExtractTask(ILOVEPDF_PUBLIC_KEY, ILOVEPDF_SECRET_KEY)
        ext.add_file(str(ocr_pdf))
        ext.execute()
        ext.set_output_filename("extract.txt")
        ext.download(str(out_dir))
        _ilovepdf_used += 1  # ExtractTask counts as doc in some plans; track usage
        txts = list(out_dir.glob("*.txt"))
        if not txts:
            return None
        content = txts[0].read_text(encoding="utf-8", errors="replace")
        print(f"[+] iLovePDF extract {len(content)} chars (docs_used≈{_ilovepdf_used})")
        return content
    except Exception as e:
        print(f"[!] iLovePDF: {e}")
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def ocr_via_tesseract(pdf_path: Path) -> str:
    if not HAS_TESSERACT:
        return ""
    print("[*] Tesseract lokal...")
    images = convert_from_path(str(pdf_path), dpi=300, fmt="png")
    parts = []
    start = max(0, len(images) - 3)
    for i in range(start, len(images)):
        g = ImageOps.autocontrast(images[i].convert("L"))
        g = ImageEnhance.Contrast(g).enhance(1.5)
        g = g.filter(ImageFilter.SHARPEN)
        t1 = pytesseract.image_to_string(g, lang="ind+eng", config="--psm 6 --oem 3")
        t2 = pytesseract.image_to_string(g, lang="ind+eng", config="--psm 4 --oem 3")
        t = t1 if len(t1) >= len(t2) else t2
        parts.append(f"=== PAGE {i+1} ===\n{t}")
        print(f"    Halaman {i+1}: {len(t)} karakter")
    return "\n".join(parts)


def ocr_pdf_text(pdf_path: Path) -> str:
    t = ocr_via_ilovepdf(pdf_path)
    if t:
        return t
    return ocr_via_tesseract(pdf_path)


# ============================================================
# PARSER + NORMALISASI
# ============================================================
def normalize_holiday_name(name: str) -> str:
    n = re.sub(r"\s+", " ", name).strip(" .|=-")
    low = n.lower()
    for pat, canon in NAME_NORMALIZE:
        if re.search(pat, low, re.I):
            # pertahankan tahun/hijriah di akhir jika ada
            tail = ""
            ym = re.search(r"(\d{4}\s*(?:Hijriah|Kongzili|BE|Saka)?|\d{3,4}\s*Hijriah).*$", n, re.I)
            if ym and canon not in ("Tahun Baru Masehi",):
                tail = " " + ym.group(0).strip()
            if canon == "Tahun Baru Masehi":
                ym2 = re.search(r"20\d{2}", n)
                return f"Tahun Baru {ym2.group(0)} Masehi" if ym2 else canon
            return (canon + tail).strip()
    return n


def parse_dates_from_chunk(chunk: str, year: int) -> List[str]:
    month_pat = (
        r"(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)"
    )
    results = []
    for m in re.finditer(
        rf"(\d{{1,2}})\s*{month_pat}\s*[-–]\s*(\d{{1,2}})\s*{month_pat}", chunk, re.I
    ):
        d1, mo1, d2, mo2 = int(m.group(1)), m.group(2).lower(), int(m.group(3)), m.group(4).lower()
        if MONTH_MAP.get(mo1):
            results.append(f"{year}-{MONTH_MAP[mo1]:02d}-{d1:02d}")
        if MONTH_MAP.get(mo2):
            results.append(f"{year}-{MONTH_MAP[mo2]:02d}-{d2:02d}")
    if results:
        return results
    m = re.search(
        rf"(\d{{1,2}}\s*,\s*\d{{1,2}}(?:\s*,\s*\d{{1,2}})*(?:\s*,?\s*dan\s*\d{{1,2}})?).{{0,50}}?{month_pat}",
        chunk, re.I,
    )
    if m:
        days = re.findall(r"\d{1,2}", m.group(1))
        mo = MONTH_MAP.get(m.group(2).lower())
        if mo and days:
            return [f"{year}-{mo:02d}-{int(d):02d}" for d in days]
    m = re.search(rf"(\d{{1,2}})\s+{month_pat}", chunk, re.I)
    if m:
        mo = MONTH_MAP.get(m.group(2).lower())
        if mo:
            return [f"{year}-{mo:02d}-{int(m.group(1)):02d}"]
    return []


def extract_from_ocr(text: str, year: int) -> Dict:
    text_norm = text.replace("|", " ").replace("]", " ").replace("[", " ")
    section_a = section_b = ""
    m_a = re.search(
        r"A\.\s*HARI LIBUR NASIONAL.*?(?=B\.\s*CUTI BERSAMA|$)",
        text_norm, re.DOTALL | re.I,
    )
    if m_a:
        section_a = m_a.group(0)
    m_b = re.search(
        r"B\.\s*CUTI BERSAMA.*?(?=MENTERI AGAMA|PLT\.|$)",
        text_norm, re.DOTALL | re.I,
    )
    if m_b:
        section_b = m_b.group(0)

    month_pat = (
        r"(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)"
    )
    KEYWORDS = [
        "Tahun Baru", "Isra Mikraj", "Imlek", "Nyepi", "Idul Fitri", "Idulfitri",
        "Wafat Yesus", "Wafat Isa", "Kebangkitan", "Paskah", "Hari Buruh",
        "Waisak", "Kenaikan Yesus", "Kenaikan Isa", "Pancasila", "Idul Adha",
        "Iduladha", "Muharam", "Muharram", "Proklamasi", "Maulid", "Kelahiran Yesus",
        "Natal",
    ]

    def rows_from_section(section: str, tipe: str) -> List[Dict]:
        if not section:
            return []
        raw = [ln.strip() for ln in section.splitlines() if ln.strip()]
        merged: List[str] = []
        buf = ""
        for ln in raw:
            cand = f"{buf} {ln}".strip() if buf else ln
            has_month = bool(re.search(month_pat, cand, re.I))
            has_name = any(re.search(kw, cand, re.I) for kw in KEYWORDS)
            if len(ln) < 12 and not has_name:
                buf = cand
                continue
            if has_month and not has_name:
                buf = cand
                continue
            if has_name and not has_month and buf:
                merged.append(f"{buf} {ln}".strip())
                buf = ""
                continue
            if buf:
                merged.append(buf)
                buf = ""
            merged.append(ln)
        if buf:
            merged.append(buf)
        ms = re.compile(rf"^{month_pat}", re.I)
        merged2: List[str] = []
        for ln in merged:
            if merged2 and ms.search(ln):
                merged2[-1] += " " + ln
            else:
                merged2.append(ln)

        out = []
        for ln in merged2:
            if not re.search(month_pat, ln, re.I):
                continue
            name = None
            for kw in KEYWORDS:
                if re.search(kw, ln, re.I):
                    mkw = re.search(rf"({re.escape(kw)}.*)$", ln, re.I)
                    name = (mkw.group(1) if mkw else kw)
                    name = re.split(
                        r"\s+(?:April|Mei|Juni|Juli|Agustus|Senin|Selasa|Rabu|Kamis|Jumat|Jum/?at|Sabtu|Minggu)\b",
                        name, maxsplit=1, flags=re.I,
                    )[0].strip(" .|=-")
                    break
            if not name:
                mday = re.search(
                    r"(Senin|Selasa|Rabu|Kamis|Jumat|Jum'?at|Sabtu|Minggu)\s+(.+)$",
                    ln, re.I,
                )
                if mday:
                    name = mday.group(2).strip(" .|")
                else:
                    continue
            dates = parse_dates_from_chunk(ln, year)
            day_m = re.search(
                r"(Senin|Selasa|Rabu|Kamis|Jumat|Jum'?at|Sabtu|Minggu)", ln, re.I
            )
            day = (day_m.group(1) if day_m else "").replace("Jum'at", "Jumat")
            name = normalize_holiday_name(name)
            for iso in dates:
                out.append({"date": iso, "day": day, "name": name, "type": tipe})
        return out

    def dedupe(items: List[Dict]) -> List[Dict]:
        seen = set()
        out = []
        for it in sorted(items, key=lambda x: x["date"]):
            if it["date"] not in seen:
                seen.add(it["date"])
                out.append(it)
        return out

    # Pass 1: section A/B
    national = rows_from_section(section_a or "", "national_holiday")
    joint = rows_from_section(section_b or "", "joint_leave")

    # Pass 2: global scan seluruh teks (tangkap baris yang lolos section)
    global_rows = rows_from_section(text_norm, "national_holiday")
    known_dates = {r["date"] for r in national + joint}
    for r in global_rows:
        if r["date"] in known_dates:
            continue
        # heuristik: jika di dekat "CUTI BERSAMA" di teks → joint
        pos = text_norm.lower().find(r["name"][:20].lower())
        cuti_pos = text_norm.lower().find("cuti bersama")
        if cuti_pos >= 0 and pos > cuti_pos:
            r = dict(r, type="joint_leave")
            joint.append(r)
        else:
            national.append(r)
        known_dates.add(r["date"])

    national = dedupe(national)
    joint = dedupe([x for x in joint if x["type"] == "joint_leave" or x["date"] not in {n["date"] for n in national}])
    # pastikan joint hanya type joint
    joint = dedupe([{**x, "type": "joint_leave"} for x in joint])

    return {
        "year": year,
        "source": "Keputusan Bersama Menteri Agama, Ketenagakerjaan, dan PANRB",
        "scraped_at": iso_now(),
        "national_holidays": national,
        "joint_leave": joint,
        "total_national": len(national),
        "total_joint_leave": len(joint),
    }


# ============================================================
# PROSES TAHUN
# ============================================================
def register_year(index: Dict, data: Dict, source_pdf: str = "", source_sha: str = "") -> None:
    y = str(data["year"])
    index.setdefault("years", {})[y] = {
        "file": f"holidays-{y}.json",
        "total_national": data.get("total_national", 0),
        "total_joint_leave": data.get("total_joint_leave", 0),
        "source_pdf": source_pdf,
        "source_sha256": source_sha,
        "scraped_at": data.get("scraped_at") or iso_now(),
        "complete": is_complete(data),
    }


def process_year(year: int, index: Dict, force: bool) -> Optional[Dict]:
    global _ilovepdf_used
    print("-" * 50)
    print(f"  Tahun {year}")
    print("-" * 50)

    meta = (index.get("years") or {}).get(str(year)) or {}

    if not needs_scrape(index, year, force):
        log_result(year, "skip", "complete")
        return None

    # Ketersediaan di JDIH (cache) — skip OCR jika belum ada & bukan known URL
    has_known = bool(KNOWN_PDF_URLS.get(year))
    if not force and not has_known:
        jyears = jdih_years_from_page()
        if jyears and year not in jyears and try_direct_pdf(year) is None:
            index["next_check_after"] = (
                now_utc() + timedelta(days=RETRY_DAYS_NOT_FOUND)
            ).date().isoformat()
            log_result(year, "skip", "not_found_on_jdih")
            return None

    urls = resolve_pdf_urls(year)
    if not urls:
        index["next_check_after"] = (
            now_utc() + timedelta(days=RETRY_DAYS_NOT_FOUND)
        ).date().isoformat()
        log_result(year, "skip", "no_pdf_url")
        return None

    best: Optional[Dict] = None
    best_score = -1
    best_url = ""
    best_sha = ""
    credits_before = _ilovepdf_used

    for url in urls:
        # skip jika hash sama dengan index (sumber tidak berubah)
        try:
            pdf_path = download_pdf(url)
        except Exception as e:
            print(f"[!] Download gagal: {e}")
            continue
        try:
            sha = file_sha256(pdf_path)
            if (
                not force
                and meta.get("complete")
                and meta.get("source_sha256") == sha
            ):
                print("[*] PDF tidak berubah (hash sama) → skip OCR")
                log_result(year, "skip", "same_hash")
                return None

            if _ilovepdf_used >= MAX_ILOVEPDF_DOCS_PER_RUN and ILOVEPDF_PUBLIC_KEY:
                # masih bisa tesseract
                print("[*] Kuota iLovePDF run ini habis, pakai Tesseract bila ada")

            ocr_text = ocr_pdf_text(pdf_path)
            if not ocr_text.strip():
                print("[!] OCR kosong")
                continue
            cand = extract_from_ocr(ocr_text, year)
            score = cand["total_national"] + cand["total_joint_leave"]
            print(f"    → nasional={cand['total_national']} cuti={cand['total_joint_leave']}")
            if score > best_score:
                best_score = score
                best = cand
                best_url = url
                best_sha = sha
        finally:
            try:
                pdf_path.unlink()
            except Exception:
                pass

    credits = _ilovepdf_used - credits_before

    if not best:
        index["next_check_after"] = (
            now_utc() + timedelta(days=RETRY_DAYS_NOT_FOUND)
        ).date().isoformat()
        log_result(year, "fail", "ocr_empty", credits)
        return None

    # Jangan timpa data complete dengan hasil jelek
    if (
        meta.get("complete")
        and not force
        and int(best.get("total_national") or 0) < MIN_NATIONAL_REJECT
    ):
        print("[!] Hasil baru terlalu sedikit; pertahankan JSON complete yang ada")
        log_result(year, "skip", "reject_poor_overwrite", credits)
        return None

    out_name = f"holidays-{year}.json"
    Path(out_name).write_text(
        json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[✓] {out_name} complete={is_complete(best)} "
        f"n={best['total_national']} j={best['total_joint_leave']}"
    )
    github_put_json(
        out_name, best,
        f"Update holidays-{year}.json ({now_utc().strftime('%Y-%m-%d')})",
    )
    register_year(index, best, best_url, best_sha)

    if is_complete(best):
        index["next_check_after"] = default_next_check(year)
        log_result(year, "ocr", "ok_complete", credits)
    else:
        index["next_check_after"] = (
            now_utc() + timedelta(days=RETRY_DAYS_INCOMPLETE)
        ).date().isoformat()
        log_result(year, "ocr", "ok_incomplete", credits)

    return best


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Scraper libur nasional (optimized)")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--from", dest="from_year", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("  Kalender Libur Nasional Indonesia")
    if args.year:
        mode = f"year={args.year}"
    elif args.from_year:
        mode = f"from={args.from_year}"
    else:
        mode = "auto"
    if args.force:
        mode += " force"
    print(f"  Mode: {mode}")
    print(f"  Season: {'yes' if in_skb_season() else 'no'} | max_ilovepdf/run={MAX_ILOVEPDF_DOCS_PER_RUN}")
    print("=" * 60)

    index = load_index()
    print(
        f"[*] Index latest={index.get('latest_year')} "
        f"next_check={index.get('next_check_after')} "
        f"years={list((index.get('years') or {}).keys())}"
    )

    targets = years_to_process(index, args.year, args.from_year, args.force)
    if not targets:
        print("[*] Tidak ada target. Selesai.")
        return

    print(f"[*] Target: {targets}")
    for y in targets:
        if _ilovepdf_used >= MAX_ILOVEPDF_DOCS_PER_RUN and not args.force:
            # force masih boleh lanjut via tesseract
            if ILOVEPDF_PUBLIC_KEY:
                print(f"[*] Kuota iLovePDF run tercapai; sisa tahun memakai Tesseract saja")
        process_year(y, index, args.force)

    save_index(index)
    print(f"\nSelesai. iLovePDF docs used ≈ {_ilovepdf_used}")


if __name__ == "__main__":
    main()
