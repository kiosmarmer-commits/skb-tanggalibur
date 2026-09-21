#!/usr/bin/env python3
"""
Kalender Libur Nasional Indonesia
=================================
Scraper SKB 3 Menteri (Hari Libur Nasional & Cuti Bersama)
Sumber: https://jdih.menpan.go.id

Fitur:
- Ambil SKB terbaru ATAU tahun tertentu (--year 2025)
- Selenium headless Chrome
- OCR Bahasa Indonesia
- Output: holidays-{tahun}.json
- Auto-upload ke GitHub folder "kalender/"

Contoh:
  python main.py                  # ambil yang terbaru
  python main.py --year 2025      # ambil tahun 2025
  python main.py --year 2026
  python main.py --year 2027
"""

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
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

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

# Mapping nama file PDF yang sudah diketahui (fallback cepat)
# URL PDF langsung (bukan hanya nama file) untuk fallback cepat
KNOWN_PDF_URLS = {
    2027: [
        "https://data-jdih.menpan.go.id/dokumen/2026skb002.pdf",
    ],
    2026: [
        "https://data-jdih.menpan.go.id/dokumen/2025skbmenpanrb005.pdf",
        "https://data-jdih.menpan.go.id/dokumen/2025skb005.pdf",
    ],
    2025: [
        "https://jdih.kemenkoinfra.go.id/cfind/source/files/keputusan-bersama-3-menteri-nomor-1017-2-2-tahun-2024.pdf",
        "https://www.kemenkopmk.go.id/sites/default/files/artikel/2025-08/SKB%20Perubahan%20Libur%20Nasional%20dan%20Cuti%20Bersama%20Tahun%202025.pdf",
    ],
}


# ============================================================
# SELENIUM
# ============================================================
def create_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={HEADERS['User-Agent']}")
    options.page_load_strategy = "eager"

    # Gunakan Chromium & Chromedriver dari sistem (Railway/Docker)
    chrome_candidates = [
        os.getenv("CHROME_BIN", ""),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    driver_candidates = [
        os.getenv("CHROMEDRIVER_PATH", ""),
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ]

    chrome_bin = next((p for p in chrome_candidates if p and Path(p).exists()), None)
    chromedriver_path = next((p for p in driver_candidates if p and Path(p).exists()), None)

    if chrome_bin:
        options.binary_location = chrome_bin
        print(f"    Chrome binary: {chrome_bin}")
    if chromedriver_path:
        print(f"    Chromedriver : {chromedriver_path}")
        service = Service(executable_path=chromedriver_path)
    else:
        # biarkan selenium cari sendiri
        service = Service()

    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)
    return driver



def extract_pdf_url_from_element(driver, btn) -> Optional[str]:
    """Ambil URL PDF dari atribut Alpine.js @click pada tombol Unduh."""
    try:
        attrs = driver.execute_script(
            """
            const el = arguments[0];
            const result = {};
            for (const attr of el.attributes) {
                result[attr.name] = attr.value;
            }
            return result;
            """,
            btn,
        )
        click_val = (
            attrs.get("@click")
            or attrs.get("x-on:click")
            or attrs.get("onclick")
            or ""
        )
        click_val = click_val.replace("\\/", "/")
        m = re.search(r"https?://[^\s'\"<>]+?\.pdf", click_val)
        if m:
            return m.group(0)
        m2 = re.search(r"data-jdih\.menpan\.go\.id/dokumen/[^\s'\"<>]+\.pdf", click_val)
        if m2:
            return "https://" + m2.group(0)
    except Exception:
        pass
    return None


def get_all_lihat_buttons(driver):
    return driver.find_elements(By.XPATH, "//button[contains(normalize-space(.),'Lihat')]")


def scrape_skb(target_year: Optional[int] = None) -> Tuple[str, str, int]:
    """
    Scrape dinamis:
    1. Buka daftar Keputusan Bersama Menteri
    2. Loop semua tombol Lihat
    3. Di halaman detail, baca judul + tahun
    4. Ambil URL PDF dari tombol Unduh
    5. Jika target_year cocok (atau ambil terbaru), kembalikan

    Tidak bergantung hardcode nama file.
    """
    print(f"[*] Membuka halaman daftar (target: {target_year or 'terbaru'})...")
    driver = create_driver()

    try:
        driver.get(LIST_URL)
        time.sleep(3)

        # Kumpulkan kandidat dari teks halaman dulu (cepat)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        page_candidates = []
        for text_node in soup.stripped_strings:
            if "Hari Libur Nasional dan Cuti Bersama" in text_node and "Tahun" in text_node:
                ym = re.search(r"Tahun\s+(\d{4})", text_node)
                if ym:
                    page_candidates.append((text_node.strip()[:200], int(ym.group(1))))

        if page_candidates:
            years_found = sorted({c[1] for c in page_candidates}, reverse=True)
            print(f"[+] Tahun terdeteksi di halaman: {years_found}")

        buttons = get_all_lihat_buttons(driver)
        if not buttons:
            raise RuntimeError("Tidak ada tombol Lihat di halaman daftar")

        print(f"[*] Memeriksa {len(buttons)} dokumen...")

        found = []  # list of (title, pdf_url, year)

        # Batasi biar tidak terlalu lama (cukup 12 item teratas biasanya)
        max_check = min(len(buttons), 12)

        for i in range(max_check):
            # re-query setiap iterasi karena DOM bisa berubah setelah back
            buttons = get_all_lihat_buttons(driver)
            if i >= len(buttons):
                break

            btn = buttons[i]
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.3)
                btn.click()
                time.sleep(2)

                # Baca judul di halaman detail
                detail_text = driver.page_source
                detail_soup = BeautifulSoup(detail_text, "html.parser")
                title = ""
                year = None

                # Cari teks judul yang mengandung Cuti Bersama / Libur Nasional
                for t in detail_soup.stripped_strings:
                    if "Hari Libur Nasional dan Cuti Bersama" in t:
                        title = t.strip()[:250]
                        ym = re.search(r"Tahun\s+(\d{4})", t)
                        if ym:
                            year = int(ym.group(1))
                        break

                # Fallback: cari pola Tahun YYYY di seluruh halaman detail
                if year is None:
                    ym = re.search(
                        r"Cuti Bersama Tahun\s+(\d{4})|Libur Nasional.*?Tahun\s+(\d{4})",
                        detail_soup.get_text(" ", strip=True),
                        re.IGNORECASE,
                    )
                    if ym:
                        year = int(ym.group(1) or ym.group(2))

                # Ambil PDF URL dari tombol Unduh
                pdf_url = None
                unduh_btns = driver.find_elements(
                    By.XPATH,
                    "//button[contains(translate(., 'UNDUH', 'unduh'), 'unduh')]",
                )
                for ub in unduh_btns:
                    pdf_url = extract_pdf_url_from_element(driver, ub)
                    if pdf_url:
                        break

                # Alternatif: cari link .pdf langsung di DOM
                if not pdf_url:
                    for a in driver.find_elements(By.CSS_SELECTOR, "a[href$='.pdf']"):
                        href = a.get_attribute("href") or ""
                        if href.endswith(".pdf"):
                            pdf_url = href
                            break

                if year and pdf_url:
                    print(f"    [{i+1}] Tahun {year} → PDF OK")
                    found.append((title or f"SKB Tahun {year}", pdf_url, year))
                elif year:
                    print(f"    [{i+1}] Tahun {year} → PDF tidak ketemu di detail")
                else:
                    print(f"    [{i+1}] Bukan dokumen libur / tahun tidak terbaca")

                # Kembali ke daftar
                driver.back()
                time.sleep(1.5)

            except Exception as e:
                print(f"    [{i+1}] Error: {e}")
                try:
                    driver.get(LIST_URL)
                    time.sleep(2)
                except Exception:
                    pass
                continue

        if not found:
            # Fallback terakhir: pakai KNOWN_PDF_URLS jika ada
            if target_year and target_year in KNOWN_PDF_URLS:
                for url in KNOWN_PDF_URLS[target_year]:
                    try:
                        r = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
                        if r.status_code == 200:
                            print(f"[+] Fallback URL langsung: {url}")
                            return f"SKB Tahun {target_year}", url, target_year
                    except Exception:
                        continue
            raise RuntimeError(
                f"Tidak menemukan SKB libur untuk tahun {target_year or 'apapun'} di halaman daftar"
            )

        # Pilih hasil
        if target_year:
            matched = [x for x in found if x[2] == target_year]
            if matched:
                title, pdf_url, year = matched[0]
                print(f"[+] Dipilih: tahun {year}")
                print(f"[+] PDF: {pdf_url}")
                return title, pdf_url, year
            else:
                # coba known urls
                if target_year in KNOWN_PDF_URLS:
                    for url in KNOWN_PDF_URLS[target_year]:
                        try:
                            r = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
                            if r.status_code == 200:
                                print(f"[+] Tahun {target_year} tidak di list, pakai URL cadangan")
                                return f"SKB Tahun {target_year}", url, target_year
                        except Exception:
                            continue
                available = sorted({x[2] for x in found})
                raise RuntimeError(
                    f"Tahun {target_year} tidak ditemukan. Yang tersedia di halaman: {available}"
                )

        # Tanpa target → ambil tahun terbesar
        found.sort(key=lambda x: x[2], reverse=True)
        title, pdf_url, year = found[0]
        print(f"[+] Dipilih (terbaru): tahun {year}")
        print(f"[+] PDF: {pdf_url}")
        return title, pdf_url, year

    finally:
        driver.quit()


# ============================================================
# DOWNLOAD + OCR
# ============================================================
def download_pdf(pdf_url: str) -> Path:
    print(f"[*] Mengunduh PDF...")
    resp = requests.get(pdf_url, headers=HEADERS, timeout=90)
    resp.raise_for_status()

    filename = pdf_url.split("/")[-1]
    tmp = Path(tempfile.gettempdir()) / filename
    tmp.write_bytes(resp.content)
    print(f"[+] Tersimpan: {tmp.name} ({len(resp.content):,} bytes)")
    return tmp


def ocr_pdf(pdf_path: Path) -> str:
    print("[*] OCR (bahasa Indonesia)...")
    images = convert_from_path(
        str(pdf_path),
        dpi=220,
        first_page=4,
        last_page=5,
        fmt="png",
    )
    texts = []
    for i, img in enumerate(images):
        text = pytesseract.image_to_string(
            img, lang="ind+eng", config="--psm 6 --oem 3"
        )
        texts.append(text)
        print(f"    Halaman {i+4}: {len(text)} karakter")
    return "\n".join(texts)


def parse_date_part(date_str: str, year: int) -> List[str]:
    date_str = date_str.lower().strip()
    results = []
    month = None
    for name, num in MONTH_MAP.items():
        if name in date_str:
            month = num
            break
    if not month:
        return results

    numbers_part = re.split(
        r"(januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)",
        date_str,
    )[0]
    numbers = re.findall(r"\d+", numbers_part)
    if not numbers:
        return results

    if len(numbers) == 2 and "-" in numbers_part:
        for d in range(int(numbers[0]), int(numbers[1]) + 1):
            results.append(f"{year}-{month:02d}-{d:02d}")
    else:
        for n in numbers:
            results.append(f"{year}-{month:02d}-{int(n):02d}")
    return results


def extract_from_ocr(text: str, year: int) -> Dict:
    national, joint = [], []

    m_a = re.search(
        r"A\.\s*HARI LIBUR NASIONAL.*?(?=B\.\s*CUTI BERSAMA|$)",
        text, re.DOTALL | re.IGNORECASE,
    )
    if m_a:
        for line in m_a.group(0).splitlines():
            m = re.search(
                r"(\d+)\s*[.\)]\s*([\d\-\s,dan]+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember))\s+([A-Za-z\-]+)\s+(.+)",
                line, re.IGNORECASE,
            )
            if m:
                for iso in parse_date_part(m.group(2), year):
                    national.append({
                        "date": iso,
                        "day": m.group(3).strip(),
                        "name": m.group(4).strip(),
                        "type": "national_holiday",
                    })

    m_b = re.search(
        r"B\.\s*CUTI BERSAMA.*?(?=MENTERI AGAMA|$)",
        text, re.DOTALL | re.IGNORECASE,
    )
    if m_b:
        for line in m_b.group(0).splitlines():
            m = re.search(
                r"(\d+)\s*[.\)]\s*([\d\-\s,dan]+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember))\s+([A-Za-z\-,\s]+)\s+(.+)",
                line, re.IGNORECASE,
            )
            if m:
                for iso in parse_date_part(m.group(2), year):
                    joint.append({
                        "date": iso,
                        "day": m.group(3).strip(),
                        "name": m.group(4).strip(),
                        "type": "joint_leave",
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
# FALLBACK DATA (data akurat yang sudah diketahui)
# ============================================================
def get_fallback(year: int) -> Optional[Dict]:
    """Return data akurat jika tersedia, else None."""
    # --- 2027 ---
    if year == 2027:
        return {
            "year": 2027,
            "source": "Keputusan Bersama Menteri Nomor 1205/3/2 Tahun 2026",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "national_holidays": [
                {"date": "2027-01-01", "day": "Jumat", "name": "Tahun Baru 2027 Masehi", "type": "national_holiday"},
                {"date": "2027-01-05", "day": "Selasa", "name": "Isra Mikraj Nabi Muhammad S.A.W. 1448 Hijriah", "type": "national_holiday"},
                {"date": "2027-02-06", "day": "Sabtu", "name": "Tahun Baru Imlek 2578 Kongzili", "type": "national_holiday"},
                {"date": "2027-03-08", "day": "Senin", "name": "Hari Suci Nyepi (Tahun Baru Saka 1949)", "type": "national_holiday"},
                {"date": "2027-03-10", "day": "Rabu", "name": "Idul Fitri 1448 Hijriah", "type": "national_holiday"},
                {"date": "2027-03-11", "day": "Kamis", "name": "Idul Fitri 1448 Hijriah", "type": "national_holiday"},
                {"date": "2027-03-26", "day": "Jumat", "name": "Wafat Yesus Kristus", "type": "national_holiday"},
                {"date": "2027-03-28", "day": "Minggu", "name": "Kebangkitan Yesus Kristus (Paskah)", "type": "national_holiday"},
                {"date": "2027-05-01", "day": "Sabtu", "name": "Hari Buruh Internasional", "type": "national_holiday"},
                {"date": "2027-05-06", "day": "Kamis", "name": "Kenaikan Yesus Kristus", "type": "national_holiday"},
                {"date": "2027-05-17", "day": "Senin", "name": "Idul Adha 1448 Hijriah", "type": "national_holiday"},
                {"date": "2027-05-20", "day": "Kamis", "name": "Hari Raya Waisak 2571 BE", "type": "national_holiday"},
                {"date": "2027-06-01", "day": "Selasa", "name": "Hari Lahir Pancasila", "type": "national_holiday"},
                {"date": "2027-06-06", "day": "Minggu", "name": "1 Muharam Tahun Baru Islam 1449 Hijriah", "type": "national_holiday"},
                {"date": "2027-08-15", "day": "Minggu", "name": "Maulid Nabi Muhammad S.A.W.", "type": "national_holiday"},
                {"date": "2027-08-17", "day": "Selasa", "name": "Proklamasi Kemerdekaan", "type": "national_holiday"},
                {"date": "2027-12-25", "day": "Sabtu", "name": "Kelahiran Yesus Kristus", "type": "national_holiday"},
                {"date": "2027-12-26", "day": "Minggu", "name": "Isra Mikraj Nabi Muhammad S.A.W. 1449 Hijriah", "type": "national_holiday"},
            ],
            "joint_leave": [
                {"date": "2027-02-05", "day": "Jumat", "name": "Tahun Baru Imlek 2578 Kongzili", "type": "joint_leave"},
                {"date": "2027-03-09", "day": "Selasa", "name": "Idul Fitri 1448 Hijriah", "type": "joint_leave"},
                {"date": "2027-03-12", "day": "Jumat", "name": "Idul Fitri 1448 Hijriah", "type": "joint_leave"},
                {"date": "2027-03-15", "day": "Senin", "name": "Idul Fitri 1448 Hijriah", "type": "joint_leave"},
                {"date": "2027-03-25", "day": "Kamis", "name": "Wafat Yesus Kristus", "type": "joint_leave"},
                {"date": "2027-05-18", "day": "Selasa", "name": "Idul Adha 1448 Hijriah", "type": "joint_leave"},
                {"date": "2027-05-19", "day": "Rabu", "name": "Hari Raya Waisak 2571 BE", "type": "joint_leave"},
                {"date": "2027-12-24", "day": "Jumat", "name": "Kelahiran Yesus Kristus", "type": "joint_leave"},
            ],
            "total_national": 18,
            "total_joint_leave": 8,
        }

    # --- 2025 (termasuk revisi 18 Agustus cuti bersama) ---
    if year == 2025:
        return {
            "year": 2025,
            "source": "SKB 1017/2/2 Tahun 2024 + Perubahan SKB 933/1/3 Tahun 2025",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "national_holidays": [
                {"date": "2025-01-01", "day": "Rabu", "name": "Tahun Baru 2025 Masehi", "type": "national_holiday"},
                {"date": "2025-01-27", "day": "Senin", "name": "Isra Mikraj Nabi Muhammad S.A.W.", "type": "national_holiday"},
                {"date": "2025-01-29", "day": "Rabu", "name": "Tahun Baru Imlek 2576 Kongzili", "type": "national_holiday"},
                {"date": "2025-03-29", "day": "Sabtu", "name": "Hari Suci Nyepi (Tahun Baru Saka 1947)", "type": "national_holiday"},
                {"date": "2025-03-31", "day": "Senin", "name": "Idul Fitri 1446 Hijriah", "type": "national_holiday"},
                {"date": "2025-04-01", "day": "Selasa", "name": "Idul Fitri 1446 Hijriah", "type": "national_holiday"},
                {"date": "2025-04-18", "day": "Jumat", "name": "Wafat Yesus Kristus", "type": "national_holiday"},
                {"date": "2025-04-20", "day": "Minggu", "name": "Kebangkitan Yesus Kristus (Paskah)", "type": "national_holiday"},
                {"date": "2025-05-01", "day": "Kamis", "name": "Hari Buruh Internasional", "type": "national_holiday"},
                {"date": "2025-05-12", "day": "Senin", "name": "Hari Raya Waisak 2569 BE", "type": "national_holiday"},
                {"date": "2025-05-29", "day": "Kamis", "name": "Kenaikan Yesus Kristus", "type": "national_holiday"},
                {"date": "2025-06-01", "day": "Minggu", "name": "Hari Lahir Pancasila", "type": "national_holiday"},
                {"date": "2025-06-06", "day": "Jumat", "name": "Idul Adha 1446 Hijriah", "type": "national_holiday"},
                {"date": "2025-06-27", "day": "Jumat", "name": "1 Muharam Tahun Baru Islam 1447 Hijriah", "type": "national_holiday"},
                {"date": "2025-08-17", "day": "Minggu", "name": "Proklamasi Kemerdekaan", "type": "national_holiday"},
                {"date": "2025-09-05", "day": "Jumat", "name": "Maulid Nabi Muhammad S.A.W.", "type": "national_holiday"},
                {"date": "2025-12-25", "day": "Kamis", "name": "Kelahiran Yesus Kristus", "type": "national_holiday"},
            ],
            "joint_leave": [
                {"date": "2025-01-28", "day": "Selasa", "name": "Tahun Baru Imlek 2576 Kongzili", "type": "joint_leave"},
                {"date": "2025-03-28", "day": "Jumat", "name": "Hari Suci Nyepi (Tahun Baru Saka 1947)", "type": "joint_leave"},
                {"date": "2025-04-02", "day": "Rabu", "name": "Idul Fitri 1446 Hijriah", "type": "joint_leave"},
                {"date": "2025-04-03", "day": "Kamis", "name": "Idul Fitri 1446 Hijriah", "type": "joint_leave"},
                {"date": "2025-04-04", "day": "Jumat", "name": "Idul Fitri 1446 Hijriah", "type": "joint_leave"},
                {"date": "2025-04-07", "day": "Senin", "name": "Idul Fitri 1446 Hijriah", "type": "joint_leave"},
                {"date": "2025-05-13", "day": "Selasa", "name": "Hari Raya Waisak 2569 BE", "type": "joint_leave"},
                {"date": "2025-05-30", "day": "Jumat", "name": "Kenaikan Yesus Kristus", "type": "joint_leave"},
                {"date": "2025-06-09", "day": "Senin", "name": "Idul Adha 1446 Hijriah", "type": "joint_leave"},
                {"date": "2025-08-18", "day": "Senin", "name": "Proklamasi Kemerdekaan (Cuti Bersama)", "type": "joint_leave"},
                {"date": "2025-12-26", "day": "Jumat", "name": "Kelahiran Yesus Kristus", "type": "joint_leave"},
            ],
            "total_national": 17,
            "total_joint_leave": 11,
        }

    return None



# ============================================================
# UPLOAD KE GITHUB
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
    parser = argparse.ArgumentParser(
        description="Scraper Hari Libur Nasional & Cuti Bersama (SKB 3 Menteri)"
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Tahun yang ingin diambil (contoh: 2025, 2026, 2027). Kosongkan = ambil terbaru.",
    )
    args = parser.parse_args()
    target_year = args.year

    print("=" * 60)
    print("  Kalender Libur Nasional Indonesia")
    print(f"  Target : {target_year or 'Terbaru'}")
    print("=" * 60)

    data = None
    pdf_path = None
    year = target_year or 2027

    try:
        title, pdf_url, year = scrape_skb(target_year)
        print(f"[+] Tahun yang diproses: {year}")

        pdf_path = download_pdf(pdf_url)

        try:
            ocr_text = ocr_pdf(pdf_path)
            data = extract_from_ocr(ocr_text, year)

            if data["total_national"] < 8:
                print(f"[!] OCR kurang lengkap ({data['total_national']} libur). Coba fallback...")
                fb = get_fallback(year)
                if fb:
                    data = fb
                    print("[+] Memakai data fallback akurat.")
                else:
                    print("[!] Tidak ada fallback untuk tahun ini. Hasil OCR tetap dipakai.")
            else:
                print("[+] Parsing OCR berhasil.")
        except Exception as e:
            print(f"[!] OCR error: {e}")
            fb = get_fallback(year)
            if fb:
                data = fb
            else:
                raise

    except Exception as e:
        print(f"[!] Gagal scrape: {e}")
        fb = get_fallback(target_year or 2027)
        if fb:
            print("[*] Memakai data fallback akurat.")
            data = fb
            year = data["year"]
        else:
            print("[!] Tidak ada data fallback. Proses dihentikan.")
            return

    finally:
        if pdf_path and pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass

    # Simpan lokal
    output_name = f"holidays-{year}.json"
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    Path(output_name).write_text(json_str, encoding="utf-8")

    print(f"\n[✓] File lokal : {output_name}")
    print(f"    Tahun          : {data['year']}")
    print(f"    Libur Nasional : {data['total_national']}")
    print(f"    Cuti Bersama   : {data['total_joint_leave']}")

    # Upload GitHub
    msg = f"Update holidays-{year}.json ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
    upload_to_github(json_str, output_name, msg)

    print("\nSelesai.")


if __name__ == "__main__":
    main()
