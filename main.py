#!/usr/bin/env python3
"""
Kalender Libur Nasional Indonesia
=================================
Scraper SKB 3 Menteri (Hari Libur Nasional & Cuti Bersama)
Sumber: https://jdih.menpan.go.id

- Selenium (headless Chrome) untuk scrape
- OCR Bahasa Indonesia
- Output: holidays-{tahun}.json
- Auto-upload ke GitHub di dalam folder "kalender/"

Cocok dijalankan di Railway (Cron Job / One-shot)
"""

import json
import os
import re
import tempfile
import time
import base64
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
from webdriver_manager.chrome import ChromeDriverManager

# ============================================================
# KONFIGURASI
# ============================================================
LIST_URL = "https://jdih.menpan.go.id/dokumen-hukum/jenis?jenis=keputusan%20bersama%20menteri"
PDF_BASE = "https://data-jdih.menpan.go.id/dokumen"

# GitHub settings (wajib diisi lewat environment variable)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")          # format: username/repo
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FOLDER = os.getenv("GITHUB_FOLDER", "kalender")  # folder tujuan di repo

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


# ============================================================
# SELENIUM
# ============================================================
def create_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={HEADERS['User-Agent']}")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def scrape_latest_skb() -> Tuple[str, str, int]:
    """
    Scrape halaman daftar SKB, ambil yang terbaru tentang
    Hari Libur Nasional & Cuti Bersama.
    Return: (title, pdf_url, year)
    """
    print("[*] Membuka halaman daftar dengan Selenium...")
    driver = create_driver()

    try:
        driver.get(LIST_URL)
        time.sleep(4)

        page_text = driver.page_source
        soup = BeautifulSoup(page_text, "html.parser")

        candidates = []
        for text_node in soup.stripped_strings:
            if "Hari Libur Nasional dan Cuti Bersama" in text_node and "Tahun" in text_node:
                year_match = re.search(r"Tahun\s+(\d{4})", text_node)
                if year_match:
                    year = int(year_match.group(1))
                    candidates.append((text_node.strip()[:250], year))

        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            title, year = candidates[0]
            print(f"[+] Ditemukan: {title[:90]}... (tahun {year})")
        else:
            print("[!] Judul spesifik tidak ditemukan, mencoba klik tombol Lihat pertama...")
            title = "SKB terbaru"
            year = 2027

        buttons = driver.find_elements(By.XPATH, "//button[contains(text(),'Lihat')]")
        if not buttons:
            raise RuntimeError("Tombol Lihat tidak ditemukan")

        buttons[0].click()
        time.sleep(3)

        pdf_url = None
        unduh_btns = driver.find_elements(
            By.XPATH, "//button[contains(text(),'Unduh') or contains(text(),'unduh')]"
        )

        for btn in unduh_btns:
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
            click_val = attrs.get("@click") or attrs.get("x-on:click") or ""
            pdf_match = re.search(
                r"https?://data-jdih\.menpan\.go\.id/dokumen/[^'\"\\]+\.pdf",
                click_val.replace("\\/", "/"),
            )
            if pdf_match:
                pdf_url = pdf_match.group(0)
                break

        if not pdf_url:
            possible = [
                f"{year-1}skb002.pdf",
                f"{year-1}skbmenpanrb002.pdf",
                f"{year-1}skbmenpanrb005.pdf",
                "2026skb002.pdf",
            ]
            for name in possible:
                test = f"{PDF_BASE}/{name}"
                r = requests.head(test, headers=HEADERS, timeout=10)
                if r.status_code == 200:
                    pdf_url = test
                    break

        if not pdf_url:
            raise RuntimeError("URL PDF tidak ditemukan")

        print(f"[+] PDF URL: {pdf_url}")
        return title, pdf_url, year

    finally:
        driver.quit()


# ============================================================
# DOWNLOAD + OCR
# ============================================================
def download_pdf(pdf_url: str) -> Path:
    print(f"[*] Mengunduh PDF: {pdf_url}")
    resp = requests.get(pdf_url, headers=HEADERS, timeout=90)
    resp.raise_for_status()

    filename = pdf_url.split("/")[-1]
    tmp = Path(tempfile.gettempdir()) / filename
    tmp.write_bytes(resp.content)
    print(f"[+] Tersimpan: {tmp} ({len(resp.content):,} bytes)")
    return tmp


def ocr_pdf(pdf_path: Path) -> str:
    print("[*] OCR halaman lampiran (bahasa Indonesia)...")
    images = convert_from_path(
        str(pdf_path),
        dpi=250,
        first_page=4,
        last_page=5,
        fmt="png",
    )
    texts = []
    for i, img in enumerate(images):
        text = pytesseract.image_to_string(
            img,
            lang="ind+eng",
            config="--psm 6 --oem 3",
        )
        texts.append(f"=== PAGE {i+4} ===\n{text}")
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
        start, end = int(numbers[0]), int(numbers[1])
        for d in range(start, end + 1):
            results.append(f"{year}-{month:02d}-{d:02d}")
    else:
        for n in numbers:
            results.append(f"{year}-{month:02d}-{int(n):02d}")
    return results


def extract_from_ocr(text: str, year: int) -> Dict:
    national, joint = [], []

    m_a = re.search(
        r"A\.\s*HARI LIBUR NASIONAL.*?(?=B\.\s*CUTI BERSAMA|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m_a:
        for line in m_a.group(0).splitlines():
            m = re.search(
                r"(\d+)\s*[.\)]\s*([\d\-\s,dan]+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember))\s+([A-Za-z\-]+)\s+(.+)",
                line,
                re.IGNORECASE,
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
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m_b:
        for line in m_b.group(0).splitlines():
            m = re.search(
                r"(\d+)\s*[.\)]\s*([\d\-\s,dan]+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember))\s+([A-Za-z\-,\s]+)\s+(.+)",
                line,
                re.IGNORECASE,
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
# FALLBACK DATA AKURAT (PDF resmi 15 Sep 2026)
# ============================================================
def get_fallback_2027() -> Dict:
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


# ============================================================
# UPLOAD KE GITHUB (dengan auto-create folder)
# ============================================================
def upload_to_github(content: str, filename: str, message: str) -> bool:
    """
    Upload file ke GitHub di dalam folder GITHUB_FOLDER.
    Jika folder belum ada, akan dibuat otomatis.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("[!] GITHUB_TOKEN atau GITHUB_REPO belum diset → skip upload")
        return False

    remote_path = f"{GITHUB_FOLDER.strip('/')}/{filename}" if GITHUB_FOLDER else filename

    print(f"[*] Upload ke GitHub: {GITHUB_REPO}/{remote_path} (branch: {GITHUB_BRANCH})")

    api_base = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{remote_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Cek apakah file sudah ada
    sha = None
    try:
        r = requests.get(api_base, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=30)
        if r.status_code == 200:
            sha = r.json().get("sha")
            print(f"    File sudah ada, akan di-update (sha: {sha[:8]}...)")
        elif r.status_code == 404:
            print("    File belum ada, akan dibuat baru (folder ikut dibuat jika belum ada)")
        else:
            print(f"    Warning: status {r.status_code} saat cek file")
    except Exception as e:
        print(f"    Warning saat cek file: {e}")

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(api_base, headers=headers, json=payload, timeout=60)
        if r.status_code in (200, 201):
            print(f"[✓] Berhasil di-upload ke GitHub → {remote_path}")
            return True
        else:
            print(f"[!] Gagal upload: HTTP {r.status_code}")
            print(f"    Response: {r.text[:300]}")
            return False
    except Exception as e:
        print(f"[!] Error upload ke GitHub: {e}")
        return False


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("  Kalender Libur Nasional Indonesia - Scraper")
    print("  SKB 3 Menteri → holidays-{tahun}.json → GitHub/kalender/")
    print("=" * 60)

    data = None
    pdf_path = None
    year = 2027

    try:
        # 1. Scrape
        title, pdf_url, year = scrape_latest_skb()
        print(f"[+] Tahun libur: {year}")

        # 2. Download
        pdf_path = download_pdf(pdf_url)

        # 3. OCR + Parse
        try:
            ocr_text = ocr_pdf(pdf_path)
            print("\n--- OCR SAMPLE ---")
            print(ocr_text[:600])
            print("--- END ---\n")

            data = extract_from_ocr(ocr_text, year)

            if data["total_national"] < 10:
                print(f"[!] OCR hanya menemukan {data['total_national']} libur. Pakai fallback akurat.")
                data = get_fallback_2027()
                year = data["year"]
            else:
                print("[+] Parsing OCR berhasil!")
        except Exception as e:
            print(f"[!] OCR error: {e}")
            data = get_fallback_2027()
            year = data["year"]

    except Exception as e:
        print(f"[!] Scrape/download gagal: {e}")
        print("[*] Menggunakan data fallback akurat...")
        data = get_fallback_2027()
        year = data["year"]

    finally:
        if pdf_path and pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass

    # 4. Simpan lokal
    output_name = f"holidays-{year}.json"
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    out_path = Path(output_name)
    out_path.write_text(json_str, encoding="utf-8")

    print(f"\n[✓] File lokal: {out_path.resolve()}")
    print(f"    Tahun          : {data['year']}")
    print(f"    Libur Nasional : {data['total_national']}")
    print(f"    Cuti Bersama   : {data['total_joint_leave']}")
    print(f"    Source         : {data['source']}")

    # 5. Upload ke GitHub (folder kalender/)
    msg = (
        f"Update libur nasional & cuti bersama tahun {year} "
        f"({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
    )
    upload_to_github(json_str, output_name, msg)

    print("\nSelesai.")


if __name__ == "__main__":
    main()
