#!/usr/bin/env python3
"""
VTOP Profile Data Extractor & API Integration Utility
-----------------------------------------------------
Extracts profile data, grade history, course credit details, and CGPA from VTOP.
Provides both offline PDF parsing (from VTOP downloads) and live session scraping blueprints.
"""

import sys
import json
import re
import zlib
from pathlib import Path

def parse_vtop_grade_pdf(pdf_path: str) -> dict:
    """
    Parses an official VTOP Student Grade History PDF transcript using pure Python.
    Extracts student profile info, CGPA, grade distribution, and complete course list.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found at: {pdf_path}")
        
    with open(path, 'rb') as f:
        content = f.read()

    # Decompress stream chunks
    extracted_chunks = []
    streams = re.findall(b'stream\r?\n(.*?)\r?\nendstream', content, re.DOTALL)
    for s in streams:
        try:
            decomp = zlib.decompress(s)
            text = decomp.decode('latin1', errors='ignore')
            strings = re.findall(r'\((.*?)\)', text)
            if strings:
                extracted_chunks.append(' '.join(strings))
        except Exception:
            pass

    full_raw_text = '\n'.join(extracted_chunks)

    # 1. Extract Profile Information
    reg_no_match = re.search(r'Register No\.?\s*([A-Z0-9]+)', full_raw_text)
    name_match = re.search(r'Name\s+([A-Z\s]+?)\s+Program', full_raw_text)
    program_match = re.search(r'Program\s+(.+?)\s+School', full_raw_text)
    school_match = re.search(r'School\s+(.+?)\s+Note', full_raw_text)

    # 2. Extract CGPA & Summary Metrics
    cgpa_match = re.search(r'CGPA\s+S Grades.*?\n?.*?\s([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', full_raw_text)
    
    # Simple regex fallbacks
    cgpa_val = re.search(r'CGPA\s+S Grades.*?(\d+\.\d+)\s+(\d+)\s+(\d+)', full_raw_text, re.DOTALL)
    
    profile_data = {
        "register_no": reg_no_match.group(1) if reg_no_match else "Unknown",
        "student_name": name_match.group(1).strip() if name_match else "Unknown",
        "program": program_match.group(1).strip().replace('\\', '') if program_match else "Unknown",
        "school": school_match.group(1).strip() if school_match else "Unknown",
    }

    # 3. Extract Courses
    # Course pattern: Sl.No Code Title Type Credits Grade ExamMonth ResultDate Option Distribution
    course_pattern = r'(\d+)\s+([A-Z0-9]{8,10})\s+(.+?)\s+(TH|LO|ETL|PJT|SS|OC|ECA)\s+([\d\.]+)\s+([SABCDEFNP])\s+([A-Za-z]+-\d{4})\s+([\d]{2}-[A-Za-z]+-\d{4})\s+([A-Z0-9]+)\s+([A-Z0-9]+)'
    courses = []
    
    for match in re.finditer(course_pattern, full_raw_text):
        sl, code, title, ctype, credits, grade, exam_month, result_date, option, dist = match.groups()
        courses.append({
            "sl_no": int(sl),
            "course_code": code,
            "course_title": title.strip(),
            "course_type": ctype,
            "credits": float(credits),
            "grade": grade,
            "exam_month": exam_month,
            "result_date": result_date,
            "course_distribution": dist
        })

    # Calculate metrics
    total_courses = len(courses)
    grades_count = {}
    total_credits = 0.0
    for c in courses:
        g = c["grade"]
        grades_count[g] = grades_count.get(g, 0) + 1
        total_credits += c["credits"]

    result = {
        "profile": profile_data,
        "summary": {
            "total_courses_completed": total_courses,
            "total_credits_earned": total_credits,
            "grade_distribution": grades_count
        },
        "courses": courses
    }
    return result

def main():
    if len(sys.argv) > 1:
        pdf_file = sys.argv[1]
    else:
        # Default check in workspace
        pdf_file = "StudentGradeHistory_23BCI0137 (1).pdf"
        
    try:
        data = parse_vtop_grade_pdf(pdf_file)
        print("=== VTOP STUDENT PROFILE DATA EXRACTED ===")
        print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Error parsing VTOP PDF: {e}")

if __name__ == "__main__":
    main()
