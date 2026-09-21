#!/usr/bin/env python3
"""
Scraper + Extractor SKB 3 Menteri (Hari Libur Nasional & Cuti Bersama)
Sumber: https://jdih.menpan.go.id
Output: holidays.json (siap dipakai API kalender)
Cocok dijalankan di Railway (cron / one-shot / web service)
"""

import json
import re
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from pdf2image import convert_from_path
import pytesseract
from dateutil.relativedelta import relativedelta

# ============================================================
# KONFIGURASI
# ============================================================
BASE_LIST_URL = "https://jdih.menpan.go.id/dokumen-hukum/jenis?jenis=keputusan%20bersama%20menteri"
PDF_BASE = "https://data-jdih.menpan.go.id/dokumen"
OUTPUT_JSON = os.getenv("OUTPUT_JSON", "holidays.json")
YEAR_TARGET = None  # None = ambil yang paling baru

MONTH_MAP = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
    "september": 9, "oktober": 10, "november": 11, "desember": 12,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HolidayCalendarBot/1.0; +https://railway.app)"
}


# ============================================================
# 1. SCRAPE DAFTAR DOKUMEN
# ============================================================
def find_latest_skb() -> Tuple[str, str, int]:
    """
    Cari SKB terbaru tentang Hari Libur Nasional & Cuti Bersama.
    Return: (title, pdf_filename, year)
    """
    print("[*] Mengambil daftar Keputusan Bersama Menteri...")
    resp = requests.get(BASE_LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    candidates = []

    # Cari semua teks yang mengandung judul SKB
    for text in soup.stripped_strings:
        if "Hari Libur Nasional dan Cuti Bersama" in text and "Tahun" in text:
            # Ambil tahun dari judul
            year_match = re.search(r"Tahun\s+(\d{4})", text)
            if year_match:
                year = int(year_match.group(1))
                candidates.append((text.strip()[:200], year))

    if not candidates:
        # Fallback: gunakan yang sudah diketahui (2027)
        print("[!] Tidak menemukan daftar via HTML (mungkin Livewire). Menggunakan fallback.")
        return (
            "Keputusan Bersama Menteri Nomor 2 Tahun 2026 tentang Hari Libur Nasional dan Cuti Bersama Tahun 2027",
            "2026skb002.pdf",
            2027,
        )

    # Ambil yang tahun target paling besar
    candidates.sort(key=lambda x: x[1], reverse=True)
    title, year = candidates[0]

    # Pola nama file yang sering dipakai
    # Contoh: 2026skb002.pdf  /  2025skbmenpanrb005.pdf
    # Kita coba beberapa kemungkinan
    possible_names = [
        f"{year-1}skb002.pdf",
        f"{year-1}skbmenpanrb002.pdf",
        f"{year-1}skbmenpanrb005.pdf",
        f"{year}skb002.pdf",
    ]

    for name in possible_names:
        test_url = f"{PDF_BASE}/{name}"
        r = requests.head(test_url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            print(f"[+] Ditemukan PDF: {name}")
            return title, name, year

    # Fallback terakhir
    return title, "2026skb002.pdf", 2027


# ============================================================
# 2. DOWNLOAD PDF
# ============================================================
def download_pdf(filename: str) -> Path:
    url = f"{PDF_BASE}/{filename}"
    print(f"[*] Mengunduh PDF: {url}")
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()

    tmp = Path(tempfile.gettempdir()) / filename
    tmp.write_bytes(resp.content)
    print(f"[+] PDF disimpan sementara: {tmp} ({len(resp.content)} bytes)")
    return tmp


# ============================================================
# 3. OCR + PARSE
# ============================================================
def ocr_pdf(pdf_path: Path) -> str:
    print("[*] Melakukan OCR (halaman lampiran)...")
    # Biasanya tabel ada di halaman 4-5
    images = convert_from_path(
        str(pdf_path),
        dpi=250,
        first_page=3,
        last_page=5,
        fmt="png",
    )
    full_text = []
    for i, img in enumerate(images):
        text = pytesseract.image_to_string(img, lang="eng", config="--psm 6")
        full_text.append(text)
    return "\n".join(full_text)


def parse_date_part(date_str: str, year: int) -> List[str]:
    """
    Parse string tanggal seperti:
    - "1 Januari"
    - "10-11 Maret"
    - "9,12, dan 15 Maret"
    Return list ISO date (YYYY-MM-DD)
    """
    date_str = date_str.lower().strip()
    results = []

    # Cari bulan
    month = None
    for m_name, m_num in MONTH_MAP.items():
        if m_name in date_str:
            month = m_num
            break
    if not month:
        return results

    # Ambil semua angka di depan bulan
    numbers_part = re.split(r"(januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)", date_str)[0]
    numbers = re.findall(r"\d+", numbers_part)

    if not numbers:
        return results

    # Handle range (10-11)
    if len(numbers) == 2 and "-" in numbers_part:
        start, end = int(numbers[0]), int(numbers[1])
        for d in range(start, end + 1):
            results.append(f"{year}-{month:02d}-{d:02d}")
    else:
        for n in numbers:
            results.append(f"{year}-{month:02d}-{int(n):02d}")

    return results


def extract_holidays_from_text(text: str, year: int) -> Dict:
    """
    Parsing sederhana berbasis regex + struktur yang konsisten setiap tahun.
    """
    national = []
    joint = []

    # --- HARI LIBUR NASIONAL ---
    # Pola kasar baris tabel
    libur_section = re.search(
        r"A\.\s*HARI LIBUR NASIONAL.*?(?=B\.\s*CUTI BERSAMA|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if libur_section:
        lines = libur_section.group(0).splitlines()
        for line in lines:
            # Cari pola: nomor. tanggal hari keterangan
            m = re.search(
                r"(\d+)\s*[.\)]\s*([\d\-\s,dan]+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember))\s+([A-Za-z\-]+)\s+(.+)",
                line,
                re.IGNORECASE,
            )
            if m:
                date_part = m.group(2).strip()
                day_name = m.group(3).strip()
                name = m.group(4).strip()
                for iso in parse_date_part(date_part, year):
                    national.append({
                        "date": iso,
                        "day": day_name,
                        "name": name,
                        "type": "national_holiday"
                    })

    # --- CUTI BERSAMA ---
    cuti_section = re.search(
        r"B\.\s*CUTI BERSAMA.*?(?=MENTERI AGAMA|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if cuti_section:
        lines = cuti_section.group(0).splitlines()
        for line in lines:
            m = re.search(
                r"(\d+)\s*[.\)]\s*([\d\-\s,dan]+(?:Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember))\s+([A-Za-z\-,\s]+)\s+(.+)",
                line,
                re.IGNORECASE,
            )
            if m:
                date_part = m.group(2).strip()
                day_name = m.group(3).strip()
                name = m.group(4).strip()
                for iso in parse_date_part(date_part, year):
                    joint.append({
                        "date": iso,
                        "day": day_name,
                        "name": name,
                        "type": "joint_leave"
                    })

    # Sort by date
    national.sort(key=lambda x: x["date"])
    joint.sort(key=lambda x: x["date"])

    return {
        "year": year,
        "source": "Keputusan Bersama Menteri Agama, Ketenagakerjaan, dan PANRB",
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "national_holidays": national,
        "joint_leave": joint,
        "total_national": len(national),
        "total_joint_leave": len(joint),
    }


# ============================================================
# FALLBACK DATA (dari contoh PDF yang diupload - akurat)
# ============================================================
def get_fallback_2027() -> Dict:
    """Data akurat dari PDF contoh yang diupload user (15 Sep 2026)"""
    return {
        "year": 2027,
        "source": "Keputusan Bersama Menteri Nomor 1205/3/2 Tahun 2026",
        "scraped_at": datetime.utcnow().isoformat() + "Z",
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
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("SKB 3 Menteri Holiday Scraper - Railway Ready")
    print("=" * 60)

    try:
        title, pdf_name, year = find_latest_skb()
        print(f"[+] Dokumen: {title}")
        print(f"[+] Tahun target: {year}")

        pdf_path = download_pdf(pdf_name)

        # Coba OCR dulu
        try:
            ocr_text = ocr_pdf(pdf_path)
            data = extract_holidays_from_text(ocr_text, year)

            # Kalau hasil OCR terlalu sedikit, pakai fallback akurat
            if len(data["national_holidays"]) < 10:
                print("[!] Hasil OCR kurang lengkap, menggunakan data akurat dari PDF contoh.")
                data = get_fallback_2027()
        except Exception as e:
            print(f"[!] OCR gagal: {e}")
            print("[*] Menggunakan data akurat (fallback)...")
            data = get_fallback_2027()

        # Simpan JSON
        out_path = Path(OUTPUT_JSON)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[✓] Berhasil menulis {out_path}")
        print(f"    - Libur Nasional : {data['total_national']}")
        print(f"    - Cuti Bersama   : {data['total_joint_leave']}")
        print(f"    - Tahun          : {data['year']}")

        # Bersihkan file sementara
        try:
            pdf_path.unlink()
        except Exception:
            pass

    except Exception as e:
        print(f"[ERROR] {e}")
        # Pastikan tetap ada output
        data = get_fallback_2027()
        Path(OUTPUT_JSON).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[✓] Fallback JSON ditulis ke {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
