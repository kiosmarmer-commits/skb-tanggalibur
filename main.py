"""
scrape_skb_holidays_railway.py
Scraping SKB Hari Libur Nasional & Cuti Bersama dari JDIH Menpan.
Output: hari-libur-{tahun}.json yang otomatis di-commit ke GitHub.
Deploy: Railway (dengan Selenium Standalone Chrome service terpisah)
"""

import json
import os
import re
import time
import base64
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from github import Github, InputGitTreeElement

# ========== KONFIGURASI (Environment Variables) ==========
JDIH_URL = "https://jdih.menpan.go.id/dokumen-hukum/jenis?jenis=keputusan%20bersama%20menteri"
SELENIUM_URL = os.environ.get(
    "SELENIUM_REMOTE_URL",
    "http://standalone-chrome.railway.internal:4444/wd/hub"
)
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]          # format: "username/repo-name"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

TARGET_YEARS = [2026, 2027]  # tahun ini & tahun depan


# ========== 1. SETUP SELENIUM REMOTE WEBDRIVER ==========
def setup_driver():
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--headless=new")

    driver = webdriver.Remote(
        command_executor=SELENIUM_URL,
        options=options,
    )
    return driver


# ========== 2. SCRAPING HALAMAN JDIH ==========
def scrape_jdih(driver):
    """Buka halaman JDIH dan kembalikan daftar link dokumen SKB."""
    driver.get(JDIH_URL)
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='dokumen-hukum']"))
    )
    time.sleep(5)  # tunggu Livewire render

    links = []
    for a in driver.find_elements(By.CSS_SELECTOR, "a[href*='dokumen-hukum']"):
        href = a.get_attribute("href") or ""
        text = (a.text or "").strip()
        if "hari libur nasional" in text.lower() and "cuti bersama" in text.lower():
            links.append({"url": href, "judul": text})

    return links


# ========== 3. FILTER SKB SESUAI TAHUN ==========
def filter_skb(links, years):
    """Ambil link SKB yang judulnya mengandung tahun target."""
    hasil = {}
    for link in links:
        judul = link["judul"].lower()
        for th in years:
            if str(th) in judul:
                hasil[th] = link
    return hasil


# ========== 4. UNDUH PDF ==========
def download_pdf(driver, url, tahun):
    """Buka halaman detail dokumen, cari tautan PDF, unduh."""
    driver.get(url)
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    time.sleep(5)

    pdf_url = None
    # Cari tautan PDF
    for el in driver.find_elements(By.CSS_SELECTOR, "a, iframe, embed"):
        src = el.get_attribute("href") or el.get_attribute("src") or ""
        if ".pdf" in src.lower():
            pdf_url = src
            break

    if not pdf_url:
        for el in driver.find_elements(
            By.XPATH, "//a[contains(., 'Download') or contains(., 'Unduh')]"
        ):
            href = el.get_attribute("href") or ""
            if ".pdf" in href.lower():
                pdf_url = href
                break

    if not pdf_url:
        print(f"[!] PDF tidak ditemukan untuk {tahun} di {url}")
        return None

    r = requests.get(pdf_url, timeout=60)
    r.raise_for_status()
    pdf_path = OUTPUT_DIR / f"skb-{tahun}.pdf"
    pdf_path.write_bytes(r.content)
    print(f"[✓] PDF {tahun} diunduh: {pdf_path}")
    return pdf_path


# ========== 5. EKSTRAK TEKS PDF ==========
def extract_text(pdf_path):
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text


# ========== 6. PARSING TEKS SKB ==========
def parse_skb(text, tahun):
    """
    Parsing teks SKB untuk mengambil:
    - sumber (nomor SKB)
    - libur_nasional: [{tanggal, hari, keterangan}]
    - cuti_bersama: [{tanggal, hari, keterangan}]
    """
    # Normalisasi
    text = re.sub(r"\s+", " ", text)

    # Ambil nomor SKB
    sumber_match = re.search(r"Nomor\s*:?\s*(\d+)\s*Tahun\s*(\d{4})", text, re.I)
    sumber = sumber_match.group(0) if sumber_match else f"SKB {tahun}"

    # Pola tanggal: "1 Januari 2026", "16 Januari 2026", dll.
    bulan = (
        "Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|"
        "September|Oktober|November|Desember"
    )
    pola_tanggal = re.compile(rf"(\d{{1,2}})\s+({bulan})\s+({tahun})", re.I)

    entries = []
    for m in pola_tanggal.finditer(text):
        tgl = f"{m.group(1)} {m.group(2)} {m.group(3)}"
        start = m.end()
        ket = text[start:start + 120].strip()
        ket = re.split(r"[.;]", ket)[0].strip()
        entries.append({"tanggal_raw": tgl, "keterangan": ket})

    libur_nasional = []
    cuti_bersama = []

    idx_libur = text.upper().find("HARI LIBUR NASIONAL")
    idx_cuti = text.upper().find("CUTI BERSAMA")

    for e in entries:
        tgl = e["tanggal_raw"]
        ket = e["keterangan"]
        try:
            dt = datetime.strptime(tgl, "%d %B %Y")
            tgl_iso = dt.strftime("%Y-%m-%d")
            hari = dt.strftime("%A")
        except ValueError:
            tgl_iso = tgl
            hari = ""

        item = {"tanggal": tgl_iso, "hari": hari, "keterangan": ket}

        pos = text.find(tgl)
        if idx_libur != -1 and idx_cuti != -1:
            if idx_libur < pos < idx_cuti:
                libur_nasional.append(item)
            elif pos > idx_cuti:
                cuti_bersama.append(item)
        elif idx_libur != -1:
            libur_nasional.append(item)
        else:
            libur_nasional.append(item)

    return {
        "tahun": tahun,
        "sumber": sumber,
        "libur_nasional": libur_nasional,
        "cuti_bersama": cuti_bersama,
    }


# ========== 7. COMMIT KE GITHUB ==========
def commit_to_github(files_to_commit, commit_message):
    """
    Commit satu atau lebih file ke GitHub menggunakan PyGithub.
    files_to_commit: list of dict { "path": ..., "content": ... }
    """
    g = Github(GITHUB_TOKEN)
    repo = g.get_repo(GITHUB_REPO)

    # Ambil branch terbaru
    branch_ref = repo.get_git_ref(f"heads/{GITHUB_BRANCH}")
    branch_sha = branch_ref.object.sha
    base_tree = repo.get_git_tree(branch_sha)

    element_list = []
    for file_info in files_to_commit:
        path = file_info["path"]
        content = file_info["content"]

        # Encode konten ke base64 (wajib untuk API GitHub)
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        element = InputGitTreeElement(
            path=path,
            mode="100644",
            type="blob",
            content=content_b64,
        )
        element_list.append(element)

    # Buat tree baru
    new_tree = repo.create_git_tree(element_list, base_tree)
    parent = repo.get_git_commit(branch_sha)

    # Buat commit
    new_commit = repo.create_git_commit(commit_message, new_tree, [parent])

    # Update branch reference
    branch_ref.edit(new_commit.sha)
    print(f"[✓] Commit berhasil: {new_commit.sha[:8]} — {commit_message}")


# ========== MAIN ==========
def main():
    driver = None
    try:
        print("[*] Menghubungkan ke Selenium Remote WebDriver...")
        driver = setup_driver()
        print(f"[✓] Terhubung ke {SELENIUM_URL}")

        print("[*] Scraping halaman JDIH...")
        links = scrape_jdih(driver)
        print(f"[✓] Ditemukan {len(links)} dokumen SKB")

        skb_map = filter_skb(links, TARGET_YEARS)
        print(f"[✓] SKB yang cocok: {list(skb_map.keys())}")

        files_to_commit = []

        for tahun, link in skb_map.items():
            print(f"\n[*] Memproses SKB {tahun}...")
            pdf_path = download_pdf(driver, link["url"], tahun)
            if not pdf_path:
                continue

            text = extract_text(pdf_path)
            data = parse_skb(text, tahun)

            # Simpan JSON lokal
            out_path = OUTPUT_DIR / f"hari-libur-{tahun}.json"
            json_content = json.dumps(data, indent=2, ensure_ascii=False)
            out_path.write_text(json_content, encoding="utf-8")
            print(f"[✓] JSON lokal disimpan: {out_path}")

            # Siapkan untuk commit ke GitHub
            files_to_commit.append(
                {
                    "path": f"output/hari-libur-{tahun}.json",
                    "content": json_content,
                }
            )

        # Commit semua file JSON ke GitHub
        if files_to_commit:
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            commit_message = (
                f"Auto-update hari libur Indonesia [{timestamp}] "
                f"({', '.join(str(t) for t in skb_map.keys())})"
            )
            commit_to_github(files_to_commit, commit_message)
        else:
            print("[!] Tidak ada file yang perlu di-commit.")

    except Exception as e:
        print(f"[ERROR] {e}")
        raise

    finally:
        if driver:
            driver.quit()
            print("[*] Driver ditutup.")


if __name__ == "__main__":
    main()
