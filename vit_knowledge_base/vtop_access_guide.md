# VTOP Data Access & Student Profile Extraction Guide

This document provides a complete technical guide on how to access and extract student profile data, grades, attendance, timetable, and marks from **VTOP (`vtop.vit.ac.in`)**.

---

## 1. Summary of Your Extracted VTOP Profile Data (Found on Laptop)

Using our automated Python parser on your local VTOP PDF transcript (`StudentGradeHistory_23BCI0137 (1).pdf`), we extracted your exact student profile:

- **Student Name**: `BHUMIT GOYAL`
- **Register Number**: `23BCI0137`
- **Program**: `B.Tech. - Computer Science and Engineering (Information Security)`
- **School**: `SCOPE` (School of Computer Science and Engineering)
- **Current CGPA**: `9.06`
- **Total Credits Earned**: `144.0` out of `162.0` total required credits
- **Grade Breakdown**:
  - **S Grade (10.0)**: 19 Courses (e.g., Python, Data Structures, AI, Compiler Design, OS Lab, Competitive Coding)
  - **A Grade (9.0)**: 30 Courses (e.g., Calculus, Physics, Java, Algorithms, Networks, Security)
  - **B Grade (8.0)**: 10 Courses
  - **C Grade (7.0)**: 1 Course
  - **F / N Grades (Fail/Debarred)**: 0 (Clean academic history!)

---

## 2. Methods to Programmatically Access Data from `vtop.vit.ac.in`

There are **3 primary ways** to access VTOP student profile data:

### Method 1: Local PDF Parser (Offline & Fast)
If you export your Grade History or Academic Transcripts from VTOP as PDFs, you can use our built-in Python script `vtop_data_extractor.py` located in your workspace:

```bash
python3 vit_knowledge_base/vtop_data_extractor.py "StudentGradeHistory_23BCI0137 (1).pdf"
```

This returns structured JSON containing every course code, title, grade, credits, and semester timeline.

---

### Method 2: Live Scraping using Python & CAPTCHA Solvers
To fetch live data (current semester attendance, daily timetable, upcoming CAT/FAT exam seating, and continuous assessment marks), you can build or use open-source Python scrapers:

#### Recommended GitHub Repositories:
1. **`vitap-vtop-client` / `vtop-py`**: Python wrapper for session management and HTTP requests to VTOP endpoints.
2. **`vtop_py_scraper`**: FastAPI-based REST service around VTOP.
3. **`VitCaptchaSolver`**: CNN/Tesseract OCR models trained specifically on VTOP's 5-character image CAPTCHAs.

#### Technical HTTP Sequence for VTOP Login:
1. **Fetch CSRF & Session Cookie**:
   - `GET https://vtop.vit.ac.in/vtop/initialProcess`
   - Extract `JSESSIONID` cookie and hidden CSRF token (`_csrf`).
2. **Fetch Captcha Image**:
   - `GET https://vtop.vit.ac.in/vtop/captcha`
   - Save image or pass to OCR solver.
3. **Submit Authentication Payload**:
   - `POST https://vtop.vit.ac.in/vtop/processLogin`
   - Form Data: `uname=23BCI0137`, `passwd=<PASSWORD>`, `captchaCheck=<CAPTCHA_TEXT>`, `_csrf=<CSRF_TOKEN>`.
4. **Access Protected Endpoints**:
   - **Grade History**: `POST https://vtop.vit.ac.in/vtop/processViewStudentGradeHistory`
   - **Attendance**: `POST https://vtop.vit.ac.in/vtop/processViewAttendance`
   - **TimeTable**: `POST https://vtop.vit.ac.in/vtop/processViewTimeTable`

---

### Method 3: Browser Automation via Playwright / Selenium
For seamless integration into your Chatbot without writing low-level HTTP requests:

```python
from playwright.sync_api import sync_playwright

def get_vtop_session(reg_no, password):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("https://vtop.vit.ac.in/vtop/")
        page.click("text=Vellore Campus")
        
        # Fill Credentials
        page.fill("#username", reg_no)
        page.fill("#password", password)
        
        # Pause for manual Captcha input or automated OCR fill
        page.wait_for_selector("#submit", timeout=30000)
        
        # Save session cookies for reuse
        cookies = page.context.cookies()
        with open("vtop_cookies.json", "w") as f:
            json.dump(cookies, f)
        
        browser.close()
```

---

## 3. Integrating VTOP Data into Your Chatbot

By combining `07_chatbot_qa_dataset.json` with the extracted student profile JSON from `vtop_data_extractor.py`, your chatbot can answer both **general university queries** and **personalized student questions**:

- *"What is my current CGPA?"* $\rightarrow$ **9.06**
- *"Am I eligible for the 9.0 Pointer attendance waiver?"* $\rightarrow$ **Yes, CGPA $\ge 9.00$**
- *"How many credits have I completed so far?"* $\rightarrow$ **144.0 out of 162.0 Credits**
- *"What grade did I get in AI and Compiler Design?"* $\rightarrow$ **S Grade in both!**
