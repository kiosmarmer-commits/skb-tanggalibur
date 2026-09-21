#!/usr/bin/env python3
"""
Kalender Libur Nasional Indonesia
=================================
Scraper SKB 3 Menteri (Hari Libur Nasional & Cuti Bersama)

Strategi (berurutan):
1. Coba URL PDF yang sudah diketahui (tanpa browser) — cepat & stabil
2. Scrape daftar JDIH dengan Selenium (cadangan)

Contoh:
  python main.py
  python main.py --year 2025
  python main.py --year 2026
  python main.py --year 2027
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from pdf2image import convert_from_path
import pytesseract

# Selenium opsional — hanya dipakai jika strategi 1 gagal
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
        TimeoutException,
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
GITHUB_FOLDER = os.getenv("GITHUB_FOLDER", "kalender")

MONTH_MAP = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
    "september": 9, "oktober": 10, "november": 11, "desember": 12,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# URL PDF langsung per tahun (sumber resmi / mirror yang stabil)
# Bisa ditambah tanpa mengubah logika utama
KNOWN_PDF_URLS: Dict[int, List[str]] = {
    2027: [
        "https://data-jdih.menpan.go.id/dokumen/2026skb002.pdf",
    ],
    2026: [
        "https://data-jdih.menpan.go.id/dokumen/2025skbmenpanrb005.pdf",
    ],
    2025: [
        "https://jdih.kemenkoinfra.go.id/cfind/source/files/keputusan-bersama-3-menteri-nomor-1017-2-2-tahun-2024.pdf",
        "https://www.kemenkopmk.go.id/sites/default/files/artikel/2025-08/SKB%20Perubahan%20Libur%20Nasional%20dan%20Cuti%20Bersama%20Tahun%202025.pdf",
    ],
}


# ============================================================
# STRATEGI 1: URL LANGSUNG (tanpa browser)
# ============================================================
def try_direct_pdf(year: int) -> Optional[str]:
    """Coba URL yang sudah diketahui. Return URL jika valid."""
    urls = list(KNOWN_PDF_URLS.get(year, []))
    # pola generik di data-jdih
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
        try:
            r = requests.head(url, headers=HEADERS, timeout=12, allow_redirects=True)
            if r.status_code != 200:
                # beberapa server tidak support HEAD
                r = requests.get(url, headers=HEADERS, timeout=15, stream=True)
            ctype = (r.headers.get("content-type") or "").lower()
            if r.status_code == 200 and (
                "pdf" in ctype or "octet" in ctype or int(r.headers.get("content-length") or 0) > 5000
            ):
                print(f"[+] PDF langsung ditemukan: {url}")
                return url
        except Exception:
            continue
    return None


# ============================================================
# STRATEGI 2: SELENIUM (cadangan)
# ============================================================
def create_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={HEADERS['User-Agent']}")
    options.page_load_strategy = "eager"

    chrome_candidates = [
        os.getenv("CHROME_BIN", ""),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ]
    driver_candidates = [
        os.getenv("CHROMEDRIVER_PATH", ""),
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
    ]

    chrome_bin = next((p for p in chrome_candidates if p and Path(p).exists()), None)
    driver_path = next((p for p in driver_candidates if p and Path(p).exists()), None)

    if chrome_bin:
        options.binary_location = chrome_bin
        print(f"    Chrome binary: {chrome_bin}")
    if driver_path:
        print(f"    Chromedriver : {driver_path}")
        service = Service(executable_path=driver_path)
    else:
        service = Service()

    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(25)
    return driver


def dismiss_alert(driver):
    try:
        alert = Alert(driver)
        alert.accept()
        time.sleep(0.5)
    except NoAlertPresentException:
        pass
    except Exception:
        pass


def extract_pdf_from_unduh(driver) -> Optional[str]:
    buttons = driver.find_elements(
        By.XPATH, "//button[contains(translate(., 'UNDUH', 'unduh'), 'unduh')]"
    )
    for btn in buttons:
        try:
            attrs = driver.execute_script(
                "var e=arguments[0],o={};for(var a of e.attributes)o[a.name]=a.value;return o;",
                btn,
            )
            val = attrs.get("@click") or attrs.get("x-on:click") or attrs.get("onclick") or ""
            val = val.replace("\\/", "/")
            m = re.search(r"https?://[^\s'\"<>]+?\.pdf", val)
            if m:
                return m.group(0)
        except Exception:
            continue
    # link langsung
    for a in driver.find_elements(By.CSS_SELECTOR, "a[href$='.pdf']"):
        href = a.get_attribute("href") or ""
        if href.endswith(".pdf"):
            return href
    return None


def scrape_with_selenium(target_year: Optional[int]) -> Optional[Tuple[str, str, int]]:
    if not HAS_SELENIUM:
        print("[!] Selenium tidak tersedia")
        return None

    print("[*] Mencoba scrape via Selenium...")
    driver = create_driver()
    try:
        driver.get(LIST_URL)
        time.sleep(3)
        dismiss_alert(driver)

        buttons = driver.find_elements(
            By.XPATH, "//button[contains(normalize-space(.),'Lihat')]"
        )
        if not buttons:
            print("[!] Tombol Lihat tidak ditemukan")
            return None

        print(f"[*] Memeriksa hingga {min(len(buttons), 8)} dokumen...")
        found: List[Tuple[str, str, int]] = []

        for i in range(min(len(buttons), 8)):
            try:
                buttons = driver.find_elements(
                    By.XPATH, "//button[contains(normalize-space(.),'Lihat')]"
                )
                if i >= len(buttons):
                    break
                btn = buttons[i]
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn
                )
                time.sleep(0.4)
                try:
                    btn.click()
                except ElementNotInteractableException:
                    driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                dismiss_alert(driver)

                page_text = BeautifulSoup(driver.page_source, "html.parser").get_text(" ", strip=True)
                year = None
                title = ""
                m = re.search(
                    r"Hari Libur Nasional dan Cuti Bersama\s+Tahun\s+(\d{4})",
                    page_text,
                    re.I,
                )
                if m:
                    year = int(m.group(1))
                    title = m.group(0)[:200]
                if not year:
                    m2 = re.search(r"Cuti Bersama Tahun\s+(\d{4})", page_text, re.I)
                    if m2:
                        year = int(m2.group(1))

                pdf_url = extract_pdf_from_unduh(driver)
                if year and pdf_url:
                    print(f"    [{i+1}] Tahun {year} → PDF OK")
                    found.append((title or f"SKB {year}", pdf_url, year))
                elif year:
                    print(f"    [{i+1}] Tahun {year} → PDF tidak ketemu")
                else:
                    print(f"    [{i+1}] Bukan dokumen libur")

                driver.get(LIST_URL)  # reload daftar (hindari page expired)
                time.sleep(2)
                dismiss_alert(driver)
            except UnexpectedAlertPresentException:
                dismiss_alert(driver)
                try:
                    driver.get(LIST_URL)
                    time.sleep(2)
                except Exception:
                    pass
            except Exception as e:
                print(f"    [{i+1}] Error: {str(e)[:80]}")
                try:
                    dismiss_alert(driver)
                    driver.get(LIST_URL)
                    time.sleep(2)
                except Exception:
                    pass

        if not found:
            return None

        if target_year:
            matched = [x for x in found if x[2] == target_year]
            if matched:
                return matched[0]
            return None

        found.sort(key=lambda x: x[2], reverse=True)
        return found[0]
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ============================================================
# DOWNLOAD + OCR
# ============================================================
def download_pdf(pdf_url: str) -> Path:
    print(f"[*] Mengunduh PDF...")
    resp = requests.get(pdf_url, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    tmp = Path(tempfile.gettempdir()) / ("skb_" + pdf_url.split("/")[-1].split("?")[0])
    if not tmp.suffix:
        tmp = tmp.with_suffix(".pdf")
    tmp.write_bytes(resp.content)
    print(f"[+] Tersimpan: {tmp.name} ({len(resp.content):,} bytes)")
    return tmp


def ocr_pdf(pdf_path: Path) -> str:
    print("[*] OCR (bahasa Indonesia)...")
    images = convert_from_path(
        str(pdf_path), dpi=220, first_page=4, last_page=5, fmt="png"
    )
    parts = []
    for i, img in enumerate(images):
        t = pytesseract.image_to_string(img, lang="ind+eng", config="--psm 6 --oem 3")
        parts.append(t)
        print(f"    Halaman {i+4}: {len(t)} karakter")
    return "\n".join(parts)


def parse_date_part(date_str: str, year: int) -> List[str]:
    date_str = date_str.lower().strip()
    month = None
    for name, num in MONTH_MAP.items():
        if name in date_str:
            month = num
            break
    if not month:
        return []
    numbers_part = re.split(
        r"(januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)",
        date_str,
    )[0]
    numbers = re.findall(r"\d+", numbers_part)
    if not numbers:
        return []
    if len(numbers) == 2 and "-" in numbers_part:
        return [
            f"{year}-{month:02d}-{d:02d}"
            for d in range(int(numbers[0]), int(numbers[1]) + 1)
        ]
    return [f"{year}-{month:02d}-{int(n):02d}" for n in numbers]


def extract_from_ocr(text: str, year: int) -> Dict:
    national, joint = [], []
    m_a = re.search(
        r"A\.\s*HARI LIBUR NASIONAL.*?(?=B\.\s*CUTI BERSAMA|$)",
        text, re.DOTALL | re.I,
    )
    if m_a:
        for line in m_a.group(0).splitlines():
            m = re.search(
                r"(\d+)\s*[.\)]\s*([\d\-\s,dan]+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember))\s+([A-Za-z\-]+)\s+(.+)",
                line, re.I,
            )
            if m:
                for iso in parse_date_part(m.group(2), year):
                    national.append({
                        "date": iso, "day": m.group(3).strip(),
                        "name": m.group(4).strip(), "type": "national_holiday",
                    })
    m_b = re.search(
        r"B\.\s*CUTI BERSAMA.*?(?=MENTERI AGAMA|$)",
        text, re.DOTALL | re.I,
    )
    if m_b:
        for line in m_b.group(0).splitlines():
            m = re.search(
                r"(\d+)\s*[.\)]\s*([\d\-\s,dan]+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember))\s+([A-Za-z\-,\s]+)\s+(.+)",
                line, re.I,
            )
            if m:
                for iso in parse_date_part(m.group(2), year):
                    joint.append({
                        "date": iso, "day": m.group(3).strip(),
                        "name": m.group(4).strip(), "type": "joint_leave",
                    })
    national.sort(key=lambda x: x["date"])
    joint.sort(key=lambda x: x["date"])
    return {
        "year": year,
        "source": "Keputusan Bersama Menteri Agama, Ketenagakerjaan, dan PANRB",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "national_holidays": national,
        "joint_leave": joint,
        "total_national": len(national),
        "total_joint_leave": len(joint),
    }




# ============================================================
# UPLOAD GITHUB
# ============================================================
def upload_to_github(content: str, filename: str, message: str) -> bool:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("[!] GITHUB_TOKEN / GITHUB_REPO belum diset → skip upload")
        return False

    remote_path = f"{GITHUB_FOLDER.strip('/')}/{filename}" if GITHUB_FOLDER else filename
    print(f"[*] Upload: {GITHUB_REPO}/{remote_path}")

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{remote_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    sha = None
    try:
        r = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(api_url, headers=headers, json=payload, timeout=45)
        if r.status_code in (200, 201):
            print(f"[✓] Berhasil upload → {remote_path}")
            return True
        print(f"[!] Gagal upload HTTP {r.status_code}: {r.text[:250]}")
        return False
    except Exception as e:
        print(f"[!] Error upload: {e}")
        return False


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Scraper Hari Libur Nasional & Cuti Bersama")
    parser.add_argument("--year", type=int, default=None, help="Tahun target (contoh: 2025)")
    args = parser.parse_args()
    target_year = args.year

    print("=" * 60)
    print("  Kalender Libur Nasional Indonesia")
    print(f"  Target : {target_year or 'Terbaru'}")
    print("=" * 60)

    data = None
    year = target_year or 2027
    pdf_path = None
    pdf_url = None

    # ----- Strategi 1: URL langsung -----
    if target_year:
        print(f"[*] Strategi 1: coba URL PDF langsung untuk {target_year}...")
        pdf_url = try_direct_pdf(target_year)
        if pdf_url:
            year = target_year

    # ----- Strategi 2: Selenium -----
    if not pdf_url:
        result = scrape_with_selenium(target_year)
        if result:
            _, pdf_url, year = result
            print(f"[+] Selenium menemukan tahun {year}")
            print(f"[+] PDF: {pdf_url}")

    # ----- Proses PDF bila ada -----
    if pdf_url:
        try:
            pdf_path = download_pdf(pdf_url)
            ocr_text = ocr_pdf(pdf_path)
            data = extract_from_ocr(ocr_text, year)
            if data["total_national"] < 8:
                print(f"[!] OCR kurang lengkap ({data['total_national']} libur). Hasil tetap disimpan.")
            else:
                print("[+] Parsing OCR berhasil.")
        except Exception as e:
            print(f"[!] Gagal proses PDF: {e}")
            data = None

    if data is None:
        print("[!] Gagal mendapatkan data libur (PDF tidak ketemu / OCR gagal).")
        print("    Coba lagi atau periksa koneksi / URL di KNOWN_PDF_URLS.")
        return

    if pdf_path and pdf_path.exists():
        try:
            pdf_path.unlink()
        except Exception:
            pass

    # Simpan + upload
    output_name = f"holidays-{year}.json"
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    Path(output_name).write_text(json_str, encoding="utf-8")

    print(f"\n[✓] File lokal : {output_name}")
    print(f"    Tahun          : {data['year']}")
    print(f"    Libur Nasional : {data['total_national']}")
    print(f"    Cuti Bersama   : {data['total_joint_leave']}")

    msg = f"Update holidays-{year}.json ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
    upload_to_github(json_str, output_name, msg)
    print("\nSelesai.")


if __name__ == "__main__":
    main()
