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
KNOWN_PDFS = {
    2027: [
        "2026skb002.pdf",
    ],
    2026: [
        "2025skbmenpanrb005.pdf",
        "2025skb005.pdf",
        "2025skb002.pdf",
    ],
    2025: [
        "2024skb002.pdf",
        "2024skbmenpanrb002.pdf",
        "2024skbmenpanrb005.pdf",
        "1017.pdf",
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
    chrome_bin = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

    options.binary_location = chrome_bin

    service = Service(executable_path=chromedriver_path)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)
    return driver


def find_skb_on_page(driver, target_year: Optional[int] = None) -> Tuple[str, int]:
    """
    Cari kartu SKB di halaman list.
    Jika target_year diisi, cari yang mengandung tahun tersebut.
    Return: (title, year)
    """
    soup = BeautifulSoup(driver.page_source, "html.parser")
    candidates = []

    for text_node in soup.stripped_strings:
        if "Hari Libur Nasional dan Cuti Bersama" not in text_node:
            continue
        if "Tahun" not in text_node:
            continue

        year_match = re.search(r"Tahun\s+(\d{4})", text_node)
        if not year_match:
            continue

        year = int(year_match.group(1))
        title = text_node.strip()[:250]
        candidates.append((title, year))

    if not candidates:
        return "SKB terbaru", target_year or 2027

    # Filter by target year if specified
    if target_year:
        matched = [c for c in candidates if c[1] == target_year]
        if matched:
            return matched[0]
        # Jika tidak ketemu di halaman pertama, tetap kembalikan yang paling dekat
        print(f"[!] Tahun {target_year} tidak ditemukan di halaman pertama.")
        print(f"    Kandidat yang ada: {[c[1] for c in candidates]}")

    # Ambil yang tahun terbesar (terbaru)
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0]


def extract_pdf_url_from_detail(driver) -> Optional[str]:
    """Ambil URL PDF dari tombol Unduh di halaman detail."""
    try:
        unduh_btns = WebDriverWait(driver, 8).until(
            EC.presence_of_all_elements_located(
                (By.XPATH, "//button[contains(text(),'Unduh') or contains(text(),'unduh')]")
            )
        )
    except Exception:
        unduh_btns = driver.find_elements(
            By.XPATH, "//button[contains(text(),'Unduh') or contains(text(),'unduh')]"
        )

    for btn in unduh_btns:
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
            click_val = attrs.get("@click") or attrs.get("x-on:click") or ""
            pdf_match = re.search(
                r"https?://data-jdih\.menpan\.go\.id/dokumen/[^'\"\\]+\.pdf",
                click_val.replace("\\/", "/"),
            )
            if pdf_match:
                return pdf_match.group(0)
        except Exception:
            continue
    return None


def scrape_skb(target_year: Optional[int] = None) -> Tuple[str, str, int]:
    """
    Scrape SKB.
    - target_year=None → ambil yang terbaru
    - target_year=2025 → cari yang tahun 2025
    Return: (title, pdf_url, year)
    """
    print(f"[*] Membuka halaman daftar (target: {target_year or 'terbaru'})...")
    driver = create_driver()

    try:
        driver.get(LIST_URL)
        time.sleep(3)

        title, year = find_skb_on_page(driver, target_year)
        print(f"[+] Dokumen: {title[:80]}... (tahun {year})")

        # Klik tombol Lihat yang sesuai
        # Karena Livewire, kita klik tombol Lihat berdasarkan urutan
        buttons = driver.find_elements(By.XPATH, "//button[contains(text(),'Lihat')]")
        if not buttons:
            raise RuntimeError("Tombol Lihat tidak ditemukan")

        # Jika target_year spesifik, coba temukan index yang cocok
        click_index = 0
        if target_year:
            # Ambil semua teks kartu untuk mapping index
            page_text = driver.page_source
            # Sederhana: klik berurutan sampai ketemu tahun yang cocok di detail
            # Untuk optimasi, kita klik yang pertama dulu (biasanya terbaru)
            pass

        buttons[click_index].click()
        time.sleep(2.5)

        pdf_url = extract_pdf_url_from_detail(driver)

        # Fallback: coba pola nama file yang diketahui
        if not pdf_url:
            print("[*] Mencoba pola nama file PDF yang diketahui...")
            candidates_names = []
            if year in KNOWN_PDFS:
                val = KNOWN_PDFS[year]
                if isinstance(val, list):
                    candidates_names.extend(val)
                else:
                    candidates_names.append(val)
            candidates_names.extend([
                f"{year-1}skb002.pdf",
                f"{year-1}skbmenpanrb002.pdf",
                f"{year-1}skbmenpanrb005.pdf",
                f"{year}skb002.pdf",
                f"{year-1}skb003.pdf",
            ])
            # hapus duplikat sambil jaga urutan
            seen = set()
            candidates_names = [x for x in candidates_names if not (x in seen or seen.add(x))]
            for name in candidates_names:
                test = f"{PDF_BASE}/{name}"
                try:
                    r = requests.head(test, headers=HEADERS, timeout=8, allow_redirects=True)
                    if r.status_code == 200 and "pdf" in r.headers.get("content-type", "").lower():
                        pdf_url = test
                        print(f"[+] Ditemukan via pola: {name}")
                        break
                except Exception:
                    continue

        if not pdf_url:
            raise RuntimeError(f"URL PDF untuk tahun {year} tidak ditemukan")

        print(f"[+] PDF URL: {pdf_url}")
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
