#!/usr/bin/env python3
"""
Local VTOP Session Scraper (Run locally on your system)
-------------------------------------------------------
This script handles VTOP session creation, CSRF token retrieval,
and CAPTCHA prompt directly on your terminal.
"""

import os
import sys
import requests
from bs4 import BeautifulSoup

VTOP_BASE_URL = "https://vtop.vit.ac.in/vtop"

def login_vtop(username, password):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    print(f"[+] Initializing login for registration number: {username}")
    try:
        init_res = session.get(f"{VTOP_BASE_URL}/initialProcess", timeout=10)
        soup = BeautifulSoup(init_res.text, "html.parser")
        
        csrf_input = soup.find("input", {"name": "_csrf"})
        csrf_token = csrf_input["value"] if csrf_input else ""

        print("[+] Fetching CAPTCHA image...")
        captcha_res = session.get(f"{VTOP_BASE_URL}/captcha", timeout=10)
        captcha_file = "vtop_captcha.png"
        with open(captcha_file, "wb") as f:
            f.write(captcha_res.content)
        print(f"[!] Saved CAPTCHA to '{captcha_file}'. Please open it and inspect the code.")

        captcha_code = input("Enter the CAPTCHA text: ").strip()

        login_payload = {
            "_csrf": csrf_token,
            "uname": username,
            "passwd": password,
            "captchaCheck": captcha_code
        }

        print("[+] Submitting authentication request...")
        login_res = session.post(f"{VTOP_BASE_URL}/processLogin", data=login_payload, timeout=10)

        if "Logout" in login_res.text or "Welcome" in login_res.text:
            print("✅ Login Successful! Session is active.")
            return session
        else:
            print("❌ Login failed. Please check your credentials or CAPTCHA text.")
            return None

    except Exception as e:
        print(f"[-] Network connection error: {e}")
        return None

if __name__ == "__main__":
    reg_no = input("Enter Registration No (e.g. 23BCI0137): ").strip() or "23BCI0137"
    pwd = input("Enter VTOP Password: ").strip()
    
    if pwd:
        session = login_vtop(reg_no, pwd)
    else:
        print("Password cannot be empty.")
