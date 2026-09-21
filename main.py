"""
scrape_skb_holidays.py
======================
Scraping Hari Libur Nasional & Cuti Bersama Indonesia dari:
  1. JDIH Menpan (SKB 3 Menteri) — via Selenium Remote WebDriver
  2. Fallback: API JSON publik (jika scraping gagal)

Output: hari-libur-{tahun}.json yang otomatis di-commit ke GitHub.

Deploy: Railway (Python service + Selenium Standalone Chrome service)
"""

import base64
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import requests
from github import Github, InputGitTreeElement
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# =========================================================
# KONFIGURASI
# =========================================================
JDIH_URL = (
    "https://jdih.menpan.go.id/dokumen-hukum/jenis"
    "?jenis=keputusan%20bersama%20menteri"
)

SELENIUM_URL = os.environ.get(
    "SELENIUM_REMOTE_URL",
    "http://standalone-chrome.railway.internal:4444/wd/hub",
)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")       # format: "owner/repo"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# Tahun target: tahun ini & tahun depan
NOW = datetime.utcnow()
TARGET_YEARS = [NOW.year, NOW.year + 1]

# Fallback API JSON publik (kalau scraping JDIH gagal)
FALLBACK_APIS = [
    # Format: URL template dengan {year}
    "https://raw.githubusercontent.com/game5413/Indonesia-Holidays-Calendar/main/data/{year}.json",
    "https://raw.githubusercontent.com/guangrei/APIHariLibur_V2/main/holidays.json",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

BULAN_ID = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
    "september": 9, "oktober": 10, "november": 11, "desember": 12,
}
HARI_ID = {
    0: "Senin", 1: "Selasa", 2: "Rabu", 3: "Kamis",
    4: "Jumat", 5: "Sabtu", 6: "Minggu",
}


# =========================================================
# UTIL
# =========================================================
def log(msg):
    print(msg, flush=True)


def safe_filename(s):
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


# =========================================================
# 1. SELENIUM SETUP
# =========================================================
def setup_driver():
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--headless=new")
    opts.add_experimental_option(
        "excludeSwitches", ["enable-automation", "enable-logging"]
    )
    opts.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Remote(
        command_executor=SELENIUM_URL,
        options=opts,
    )
    driver.set_page_load_timeout(60)
    return driver


# =========================================================
# 2. SCRAPING JDIH
# =========================================================
def scrape_jdih(driver):
    """
    Ambil semua link dokumen SKB dari halaman JDIH.
    Return list of dict: [{"url": ..., "judul": ...}, ...]
    """
    log(f"[*] Membuka {JDIH_URL}")
    driver.get(JDIH_URL)

    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
    except Exception:
        log("[!] Timeout saat membuka halaman JDIH")
        return []

    time.sleep(8)  # tunggu Livewire render

    all_links = []
    seen_href = set()
    page = 1

    while page <= 5:
        log(f"[*] Memproses halaman {page}...")

        # Scroll untuk trigger lazy-load
        try:
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(1)
        except Exception:
            pass

        # Ambil semua link
        links = driver.find_elements(By.TAG_NAME, "a")
        log(f"    Total <a> di halaman: {len(links)}")

        page_added = 0
        for a in links:
            try:
                href = a.get_attribute("href") or ""
                text = (a.text or "").strip()
            except Exception:
                continue

            if not href or href in seen_href:
                continue

            # Filter: hanya link dokumen-hukum
            if "dokumen-hukum/" not in href:
                continue

            # Judul harus mengandung 'libur' atau 'cuti'
            low = text.lower()
            if not ("libur" in low or "cuti" in low or "hari besar" in low):
                continue

            seen_href.add(href)
            all_links.append({"url": href, "judul": text})
            page_added += 1
            log(f"    → {text[:100]}")

        log(f"    Link relevan di halaman {page}: {page_added}")

        # Coba klik tombol Next
        try:
            next_btn = driver.find_element(
                By.XPATH,
                "//a[contains(., 'Next') or contains(., 'Selanjutnya') "
                "or contains(., '›') or contains(., '»')]",
            )
            if next_btn.is_enabled() and next_btn.is_displayed():
                driver.execute_script("arguments[0].click();", next_btn)
                time.sleep(5)
                page += 1
            else:
                break
        except Exception:
            break

    log(f"[✓] Total link relevan: {len(all_links)}")
    return all_links


def filter_skb_by_year(links, years):
    """Ambil link yang judulnya mengandung tahun target."""
    hasil = {}
    for link in links:
        judul = link["judul"]
        for y in years:
            if str(y) in judul and y not in hasil:
                hasil[y] = link
    return hasil


# =========================================================
# 3. UNDUH & EKSTRAK PDF
# =========================================================
def find_pdf_url(driver, page_url):
    """Buka halaman detail, cari URL PDF."""
    driver.get(page_url)
    time.sleep(5)

    # Cari <a> / <iframe> / <embed> dengan .pdf
    candidates = []
    for sel in ["a", "iframe", "embed", "object"]:
        for el in driver.find_elements(By.TAG_NAME, sel):
            src = (
                el.get_attribute("href")
                or el.get_attribute("src")
                or el.get_attribute("data")
                or ""
            )
            if ".pdf" in src.lower():
                candidates.append(src)

    # Cari tombol Download/Unduh
    if not candidates:
        for el in driver.find_elements(
            By.XPATH,
            "//a[contains(., 'Download') or contains(., 'Unduh') "
            "or contains(., 'PDF')]",
        ):
            href = el.get_attribute("href") or ""
            if href:
                candidates.append(href)

    return candidates[0] if candidates else None


def download_pdf(pdf_url, tahun):
    try:
        r = requests.get(pdf_url, headers=HEADERS, timeout=90, stream=True)
        r.raise_for_status()
        pdf_path = OUTPUT_DIR / f"skb-{tahun}.pdf"
        with open(pdf_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        log(f"[✓] PDF {tahun} diunduh: {pdf_path} "
            f"({pdf_path.stat().st_size // 1024} KB)")
        return pdf_path
    except Exception as e:
        log(f"[!] Gagal unduh PDF {tahun}: {e}")
        return None


def extract_text(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
        return text
    except Exception as e:
        log(f"[!] Gagal ekstrak PDF: {e}")
        return ""


# =========================================================
# 4. PARSING TEKS SKB
# =========================================================
def parse_tanggal_iso(tgl_str):
    """'1 Januari 2026' -> '2026-01-01'"""
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", tgl_str, re.I)
    if not m:
        return None, None
    d, bln, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    bln_num = BULAN_ID.get(bln)
    if not bln_num:
        return None, None
    try:
        dt = datetime(y, bln_num, d)
        return dt.strftime("%Y-%m-%d"), HARI_ID[dt.weekday()]
    except ValueError:
        return None, None


def parse_skb(text, tahun):
    """
    Parsing teks PDF SKB untuk menghasilkan struktur JSON.
    """
    if not text:
        return None

    text = re.sub(r"\s+", " ", text)

    # Nomor SKB
    m = re.search(
        r"(Nomor[:\s]*[\d]+[^\n]{0,80}?Tahun\s*\d{4})", text, re.I
    )
    sumber = m.group(1).strip() if m else f"SKB Hari Libur {tahun}"

    # Pola tanggal
    bulan = "|".join(BULAN_ID.keys())
    pola = re.compile(rf"(\d{{1,2}})\s+({bulan})\s+{tahun}", re.I)

    # Tentukan batas bagian
    upper = text.upper()
    idx_libur = upper.find("HARI LIBUR NASIONAL")
    idx_cuti = upper.find("CUTI BERSAMA")

    libur, cuti = [], []
    seen_tgl = set()

    for match in pola.finditer(text):
        tgl_str = match.group(0)
        iso, hari = parse_tanggal_iso(tgl_str)
        if not iso or iso in seen_tgl:
            continue
        seen_tgl.add(iso)

        # Ambil keterangan setelah tanggal
        s = match.end()
        ket = text[s:s + 150].strip()
        ket = re.split(r"[.;]|\d{1,2}\s+(?:Januari|Februari|Maret|April|Mei|"
                       r"Juni|Juli|Agustus|September|Oktober|November|Desember)",
                       ket)[0].strip()
        ket = re.sub(r"^(?:[-–—:]\s*)", "", ket).strip()

        item = {"tanggal": iso, "hari": hari, "keterangan": ket}

        pos = match.start()
        if idx_libur != -1 and idx_cuti != -1:
            if idx_libur < pos < idx_cuti:
                libur.append(item)
            elif pos > idx_cuti:
                cuti.append(item)
        elif idx_cuti != -1 and pos > idx_cuti:
            cuti.append(item)
        else:
            libur.append(item)

    libur.sort(key=lambda x: x["tanggal"])
    cuti.sort(key=lambda x: x["tanggal"])

    if not libur and not cuti:
        return None

    return {
        "tahun": tahun,
        "sumber": sumber,
        "sumber_url": JDIH_URL,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "libur_nasional": libur,
        "cuti_bersama": cuti,
    }


# =========================================================
# 5. FALLBACK API PUBLIK
# =========================================================
def fetch_fallback(tahun):
    """Ambil data hari libur dari API JSON publik."""
    log(f"[*] Mencoba fallback API untuk {tahun}...")

    # Coba API 1: game5413
    try:
        url = FALLBACK_APIS[0].format(year=tahun)
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            data = r.json()
            result = normalize_fallback(data, tahun, url)
            if result:
                log(f"[✓] Fallback 1 berhasil ({len(result['libur_nasional'])} libur)")
                return result
    except Exception as e:
        log(f"[!] Fallback 1 gagal: {e}")

    # Coba API 2: guangrei
    try:
        r = requests.get(FALLBACK_APIS[1], headers=HEADERS, timeout=30)
        if r.status_code == 200:
            data = r.json()
            result = normalize_fallback(data, tahun, FALLBACK_APIS[1])
            if result:
                log(f"[✓] Fallback 2 berhasil ({len(result['libur_nasional'])} libur)")
                return result
    except Exception as e:
        log(f"[!] Fallback 2 gagal: {e}")

    log(f"[!] Semua fallback gagal untuk {tahun}")
    return None


def normalize_fallback(data, tahun, sumber_url):
    """
    Normalisasi data dari berbagai sumber fallback ke format standar.
    """
    libur, cuti = [], []

    # Kalau data adalah list of dict
    items = data if isinstance(data, list) else data.get("data", [])
    if isinstance(items, dict):
        # Mungkin format {"2026-01-01": {...}}
        items = [
            {"tanggal": k, **v} if isinstance(v, dict) else {"tanggal": k, "keterangan": str(v)}
            for k, v in items.items()
        ]

    for item in items:
        if not isinstance(item, dict):
            continue
        tanggal = (
            item.get("tanggal")
            or item.get("date")
            or item.get("holiday_date")
            or ""
        )
        if not tanggal.startswith(str(tahun)):
            continue

        keterangan = (
            item.get("keterangan")
            or item.get("summary")
            or item.get("name")
            or item.get("description")
            or ""
        )

        try:
            dt = datetime.strptime(tanggal[:10], "%Y-%m-%d")
            hari = HARI_ID[dt.weekday()]
        except Exception:
            hari = ""

        entry = {"tanggal": tanggal[:10], "hari": hari, "keterangan": keterangan}

        is_cuti = (
            item.get("is_cuti_bersama")
            or "cuti bersama" in keterangan.lower()
        )
        if is_cuti:
            cuti.append(entry)
        else:
            libur.append(entry)

    if not libur and not cuti:
        return None

    libur.sort(key=lambda x: x["tanggal"])
    cuti.sort(key=lambda x: x["tanggal"])

    return {
        "tahun": tahun,
        "sumber": "API JSON publik (fallback)",
        "sumber_url": sumber_url,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "libur_nasional": libur,
        "cuti_bersama": cuti,
    }


# =========================================================
# 6. GITHUB COMMIT
# =========================================================
def commit_to_github(files_to_commit, message):
    """
    Commit list of {"path": ..., "content": ...} ke GitHub.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log("[!] GITHUB_TOKEN / GITHUB_REPO tidak diset, skip commit.")
        return

    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO)

        branch_ref = repo.get_git_ref(f"heads/{GITHUB_BRANCH}")
        branch_sha = branch_ref.object.sha
        base_tree = repo.get_git_tree(branch_sha)

        elements = []
        for f in files_to_commit:
            content_b64 = base64.b64encode(
                f["content"].encode("utf-8")
            ).decode("utf-8")
            elements.append(
                InputGitTreeElement(
                    path=f["path"],
                    mode="100644",
                    type="blob",
                    content=content_b64,
                )
            )

        new_tree = repo.create_git_tree(elements, base_tree)
        parent = repo.get_git_commit(branch_sha)
        new_commit = repo.create_git_commit(message, new_tree, [parent])
        branch_ref.edit(new_commit.sha)

        log(f"[✓] Commit berhasil: {new_commit.sha[:8]} — {message}")
    except Exception as e:
        log(f"[!] Commit ke GitHub gagal: {e}")
        log(traceback.format_exc())


# =========================================================
# MAIN
# =========================================================
def main():
    log("=" * 60)
    log(f"[*] Mulai scraping hari libur Indonesia")
    log(f"[*] Target tahun: {TARGET_YEARS}")
    log("=" * 60)

    results = {}       # {tahun: dict_json}
    driver = None

    # ---------- FASE 1: Scraping JDIH ----------
    try:
        log("\n[FASE 1] Scraping JDIH Menpan via Selenium...")
        driver = setup_driver()
        log(f"[✓] Terhubung ke Selenium: {SELENIUM_URL}")

        links = scrape_jdih(driver)
        skb_map = filter_skb_by_year(links, TARGET_YEARS)
        log(f"[✓] SKB ditemukan untuk tahun: {list(skb_map.keys())}")

        for tahun, link in skb_map.items():
            log(f"\n[*] Memproses SKB {tahun}: {link['judul'][:80]}")
            pdf_url = find_pdf_url(driver, link["url"])
            if not pdf_url:
                log(f"[!] PDF tidak ditemukan untuk {tahun}")
                continue

            log(f"    PDF URL: {pdf_url[:120]}")
            pdf_path = download_pdf(pdf_url, tahun)
            if not pdf_path:
                continue

            text = extract_text(pdf_path)
            log(f"    Panjang teks: {len(text)} karakter")

            data = parse_skb(text, tahun)
            if data:
                results[tahun] = data
                log(f"[✓] Parsing {tahun} berhasil: "
                    f"{len(data['libur_nasional'])} libur + "
                    f"{len(data['cuti_bersama'])} cuti bersama")
            else:
                log(f"[!] Parsing {tahun} gagal, akan pakai fallback")

    except Exception as e:
        log(f"[!] FASE 1 error: {e}")
        log(traceback.format_exc())
    finally:
        if driver:
            try:
                driver.quit()
                log("[*] Driver ditutup.")
            except Exception:
                pass

    # ---------- FASE 2: Fallback untuk tahun yang belum berhasil ----------
    log("\n[FASE 2] Fallback untuk tahun yang belum lengkap...")
    for tahun in TARGET_YEARS:
        if tahun in results and results[tahun]["libur_nasional"]:
            continue
        fb = fetch_fallback(tahun)
        if fb:
            results[tahun] = fb

    # ---------- FASE 3: Simpan JSON & Commit ----------
    log("\n[FASE 3] Simpan JSON & commit ke GitHub...")
    if not results:
        log("[!] Tidak ada data yang berhasil dikumpulkan. Keluar.")
        sys.exit(1)

    files_to_commit = []
    for tahun, data in sorted(results.items()):
        filename = f"output/hari-libur-{tahun}.json"
        content = json.dumps(data, indent=2, ensure_ascii=False)

        local_path = OUTPUT_DIR / f"hari-libur-{tahun}.json"
        local_path.write_text(content, encoding="utf-8")
        log(f"[✓] Lokal: {local_path}")

        files_to_commit.append({"path": filename, "content": content})

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    commit_msg = (
        f"Auto-update hari libur Indonesia [{timestamp}] "
        f"({', '.join(str(y) for y in sorted(results.keys()))})"
    )
    commit_to_github(files_to_commit, commit_msg)

    log("\n" + "=" * 60)
    log(f"[✓] SELESAI. Tahun diproses: {sorted(results.keys())}")
    log("=" * 60)


if __name__ == "__main__":
    main()
