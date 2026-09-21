"""
scrape_skb_holidays.py
======================
Scraping Hari Libur Nasional & Cuti Bersama Indonesia dari JDIH Menpan.

Alur:
  1. Buka halaman daftar SKB di JDIH Menpan.
  2. Cari link SKB dengan judul mengandung "libur"/"cuti" & tahun target.
  3. Klik tombol "Lihat" untuk masuk halaman detail.
  4. Cari URL PDF & unduh.
  5. Ekstrak teks dengan PyMuPDF, parsing jadi struktur JSON.
  6. Commit JSON ke GitHub.

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

NOW = datetime.utcnow()
TARGET_YEARS = [NOW.year, NOW.year + 1]

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

    driver = webdriver.Remote(command_executor=SELENIUM_URL, options=opts)
    driver.set_page_load_timeout(60)
    return driver


# =========================================================
# 2. SCRAPING DAFTAR SKB DI JDIH
# =========================================================
def scrape_jdih(driver):
    """
    Ambil link detail SKB dari halaman daftar JDIH.
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

        # Ambil semua <a> yang mengarah ke dokumen-hukum
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
            if "dokumen-hukum/" not in href:
                continue

            # Cek judul link ATAU parent card-nya
            judul = text
            if not ("libur" in judul.lower() or "cuti" in judul.lower()):
                # Coba ambil teks dari parent (card)
                try:
                    parent = a.find_element(
                        By.XPATH, "./ancestor::*[self::div or self::article][1]"
                    )
                    parent_text = (parent.text or "").strip()
                    if ("libur" in parent_text.lower()
                            or "cuti" in parent_text.lower()):
                        judul = parent_text
                except Exception:
                    pass

            if not ("libur" in judul.lower()
                    or "cuti" in judul.lower()
                    or "hari besar" in judul.lower()):
                continue

            seen_href.add(href)
            all_links.append({"url": href, "judul": judul})
            page_added += 1
            log(f"    → {judul[:120].replace(chr(10), ' | ')}")

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
# 3. BUKA HALAMAN DETAIL & UNDUH PDF
# =========================================================
def open_detail_and_find_pdf(driver, detail_url, tahun):
    """
    Buka halaman detail SKB, klik tombol 'Lihat' untuk masuk ke viewer,
    lalu cari URL PDF.
    """
    log(f"    Membuka halaman detail: {detail_url}")
    driver.get(detail_url)
    time.sleep(5)

    # Simpan HTML debug
    try:
        debug_path = OUTPUT_DIR / f"debug-detail-{tahun}.html"
        debug_path.write_text(driver.page_source, encoding="utf-8")
        log(f"    [i] HTML debug: {debug_path}")
    except Exception:
        pass

    # ---------- Klik tombol "Lihat" ----------
    clicked = False
    for xpath in [
        "//a[normalize-space(.)='Lihat']",
        "//button[normalize-space(.)='Lihat']",
        "//a[contains(normalize-space(.), 'Lihat')]",
        "//button[contains(normalize-space(.), 'Lihat')]",
        "//a[contains(@href, 'lihat') or contains(@href, 'view')]",
    ]:
        try:
            btn = driver.find_element(By.XPATH, xpath)
            driver.execute_script("arguments[0].click();", btn)
            log(f"    [✓] Tombol 'Lihat' diklik via: {xpath}")
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        log("    [!] Tombol 'Lihat' tidak ditemukan, coba cari PDF langsung.")

    # Tunggu viewer/PDF dimuat
    time.sleep(6)

    # ---------- Cari URL PDF ----------
    pdf_url = None

    # 1) Cek semua elemen yang mungkin membawa PDF
    for sel in ["a", "iframe", "embed", "object", "source"]:
        try:
            for el in driver.find_elements(By.TAG_NAME, sel):
                src = (
                    el.get_attribute("href")
                    or el.get_attribute("src")
                    or el.get_attribute("data")
                    or ""
                )
                if ".pdf" in src.lower():
                    pdf_url = src
                    break
            if pdf_url:
                break
        except Exception:
            continue

    # 2) Cek via JavaScript (jika PDF di-embed oleh viewer JS)
    if not pdf_url:
        try:
            pdf_url = driver.execute_script(
                """
                const el = document.querySelector(
                  'iframe[src*=".pdf"], embed[src*=".pdf"], '
                  'a[href*=".pdf"], object[data*=".pdf"]'
                );
                if (el) return el.src || el.href || el.data;
                return null;
                """
            )
        except Exception:
            pass

    # 3) Cek via tombol Unduh/Download
    if not pdf_url:
        for xpath in [
            "//a[contains(., 'Unduh') or contains(., 'Download')]",
            "//button[contains(., 'Unduh') or contains(., 'Download')]",
        ]:
            try:
                el = driver.find_element(By.XPATH, xpath)
                href = el.get_attribute("href") or ""
                if href:
                    pdf_url = href
                    break
                # Klik untuk trigger download
                driver.execute_script("arguments[0].click();", el)
                time.sleep(5)
                # Cek lagi
                for a in driver.find_elements(By.TAG_NAME, "a"):
                    h = a.get_attribute("href") or ""
                    if ".pdf" in h.lower():
                        pdf_url = h
                        break
                if pdf_url:
                    break
            except Exception:
                continue

    return pdf_url


def download_pdf(pdf_url, tahun):
    try:
        r = requests.get(pdf_url, headers=HEADERS, timeout=90, stream=True)
        r.raise_for_status()
        pdf_path = OUTPUT_DIR / f"skb-{tahun}.pdf"
        with open(pdf_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        log(f"    [✓] PDF diunduh: {pdf_path} "
            f"({pdf_path.stat().st_size // 1024} KB)")
        return pdf_path
    except Exception as e:
        log(f"    [!] Gagal unduh PDF: {e}")
        return None


# =========================================================
# 4. EKSTRAK TEKS & PARSING
# =========================================================
def extract_text(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
        return text
    except Exception as e:
        log(f"    [!] Gagal ekstrak PDF: {e}")
        return ""


def parse_tanggal_iso(tgl_str):
    """'1 Januari 2026' -> ('2026-01-01', 'Kamis')"""
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
    """Parsing teks PDF SKB menjadi struktur JSON."""
    if not text:
        return None

    text_norm = re.sub(r"\s+", " ", text)

    # Nomor SKB
    m = re.search(
        r"(Nomor[:\s]*\d+[^\n]{0,80}?Tahun\s*\d{4})", text_norm, re.I
    )
    sumber = m.group(1).strip() if m else f"SKB Hari Libur {tahun}"

    bulan = "|".join(BULAN_ID.keys())
    pola = re.compile(rf"(\d{{1,2}})\s+({bulan})\s+{tahun}", re.I)

    upper = text_norm.upper()
    idx_libur = upper.find("HARI LIBUR NASIONAL")
    idx_cuti = upper.find("CUTI BERSAMA")

    libur, cuti = [], []
    seen_tgl = set()

    for match in pola.finditer(text_norm):
        iso, hari = parse_tanggal_iso(match.group(0))
        if not iso or iso in seen_tgl:
            continue
        seen_tgl.add(iso)

        # Keterangan = 150 karakter setelah tanggal
        s = match.end()
        ket = text_norm[s:s + 150].strip()
        ket = re.split(r"[.;]\s|\s(?=\d{1,2}\s+\w+\s+\d{4})", ket)[0].strip()
        ket = re.sub(r"^[-–—:\s]+", "", ket).strip()

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
# 5. GITHUB COMMIT
# =========================================================
def commit_to_github(files_to_commit, message):
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
    log("[*] Mulai scraping hari libur Indonesia dari JDIH Menpan")
    log(f"[*] Target tahun: {TARGET_YEARS}")
    log("=" * 60)

    results = {}
    driver = None

    try:
        log("\n[FASE 1] Setup Selenium WebDriver...")
        driver = setup_driver()
        log(f"[✓] Terhubung ke: {SELENIUM_URL}")

        log("\n[FASE 2] Scraping daftar SKB...")
        links = scrape_jdih(driver)
        if not links:
            log("[!] Tidak ada link SKB ditemukan. Keluar.")
            sys.exit(1)

        skb_map = filter_skb_by_year(links, TARGET_YEARS)
        log(f"[✓] SKB cocok untuk tahun: {list(skb_map.keys())}")

        if not skb_map:
            log("[!] Tidak ada SKB untuk tahun target. Keluar.")
            sys.exit(1)

        log("\n[FASE 3] Proses setiap SKB...")
        for tahun, link in skb_map.items():
            log(f"\n[*] === Tahun {tahun} ===")
            log(f"    Judul: {link['judul'][:120]}")

            pdf_url = open_detail_and_find_pdf(
                driver, link["url"], tahun
            )
            if not pdf_url:
                log(f"    [!] PDF tidak ditemukan untuk {tahun}")
                continue

            log(f"    PDF URL: {pdf_url[:150]}")
            pdf_path = download_pdf(pdf_url, tahun)
            if not pdf_path:
                continue

            text = extract_text(pdf_path)
            log(f"    Panjang teks: {len(text)} karakter")
            if len(text) < 100:
                log(f"    [!] Teks terlalu pendek, mungkin PDF scan.")
                continue

            data = parse_skb(text, tahun)
            if not data:
                log(f"    [!] Parsing gagal.")
                continue

            results[tahun] = data
            log(f"    [✓] Parsing OK: "
                f"{len(data['libur_nasional'])} libur + "
                f"{len(data['cuti_bersama'])} cuti bersama")

    except Exception as e:
        log(f"[!] Error: {e}")
        log(traceback.format_exc())
    finally:
        if driver:
            try:
                driver.quit()
                log("[*] Driver ditutup.")
            except Exception:
                pass

    # ---------- Simpan JSON & Commit ----------
    log("\n[FASE 4] Simpan JSON & commit ke GitHub...")
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
