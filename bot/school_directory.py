"""
bot/school_directory.py
Master Institutional Dean & Head of Department (HoD) Directory for all VIT Schools.
Dynamically resolved when any new student logs into VTOP based on their Program and School.
"""

from typing import Dict, Any, Optional

# Verified Dean Directory across all VIT Vellore Schools
SCHOOL_DEANS: Dict[str, Dict[str, Any]] = {
    "SCOPE": {
        "school_name": "School of Computer Science and Engineering",
        "dean_name": "Dr. JAISANKAR N",
        "designation": "Professor Higher Academic Grade & Dean",
        "cabin": "SJT 321",
        "email": "dean.scope@vit.ac.in",
        "intercom": "1321"
    },
    "SITE": {
        "school_name": "School of Information Technology and Engineering",
        "dean_name": "Dr. Aswani Kumar Ch",
        "designation": "Professor & Dean",
        "cabin": "SJT 111",
        "email": "dean.site@vit.ac.in",
        "intercom": "1111"
    },
    "SENSE": {
        "school_name": "School of Electronics Engineering",
        "dean_name": "Dr. Sivanantha Raja A",
        "designation": "Professor & Dean",
        "cabin": "TT 312",
        "email": "dean.sense@vit.ac.in",
        "intercom": "2312"
    },
    "SELECT": {
        "school_name": "School of Electrical Engineering",
        "dean_name": "Dr. Mathew M",
        "designation": "Professor & Dean",
        "cabin": "TT 214",
        "email": "dean.select@vit.ac.in",
        "intercom": "2214"
    },
    "SMEC": {
        "school_name": "School of Mechanical Engineering",
        "dean_name": "Dr. Devendran N",
        "designation": "Professor & Dean",
        "cabin": "MB 102",
        "email": "dean.smec@vit.ac.in",
        "intercom": "2102"
    },
    "SCHEME": {
        "school_name": "School of Chemical Engineering",
        "dean_name": "Dr. Muruganandam L",
        "designation": "Professor & Dean",
        "cabin": "ALM 204",
        "email": "dean.scheme@vit.ac.in",
        "intercom": "2204"
    },
    "SAS": {
        "school_name": "School of Advanced Sciences",
        "dean_name": "Dr. E. James Jebaseelan Samuel",
        "designation": "Professor & Dean",
        "cabin": "TT 415",
        "email": "dean.sas@vit.ac.in",
        "intercom": "2415"
    },
    "SSL": {
        "school_name": "School of Social Sciences and Languages",
        "dean_name": "Dr. G. Balamurugan",
        "designation": "Professor & Dean",
        "cabin": "MB 218",
        "email": "dean.ssl@vit.ac.in",
        "intercom": "2218"
    },
    "VSB": {
        "school_name": "VIT Business School",
        "dean_name": "Dr. Subhashini P",
        "designation": "Professor & Dean",
        "cabin": "MB 305",
        "email": "dean.vitbs@vit.ac.in",
        "intercom": "2305"
    }
}

# Verified Department HoD Directory across VIT Specializations
DEPARTMENT_HODS: Dict[str, Dict[str, Any]] = {
    # SCOPE Departments
    "INFOSEC": {
        "dept_name": "Department of Information Security",
        "hod_name": "Dr. RAJESHKANNAN R",
        "designation": "Professor Grade 1 & HoD",
        "cabin": "SJT 411 A0 / SJT 322",
        "email": "hod.is@vit.ac.in",
        "intercom": "1411"
    },
    "CSE_CORE": {
        "dept_name": "Department of Computer Science and Engineering",
        "hod_name": "Dr. Ilango P",
        "designation": "Professor & HoD",
        "cabin": "SJT 401",
        "email": "hod.cse@vit.ac.in",
        "intercom": "1401"
    },
    "SWE": {
        "dept_name": "Department of Software and Systems Engineering",
        "hod_name": "Dr. Geetha S",
        "designation": "Professor & HoD",
        "cabin": "SJT 302",
        "email": "hod.swe@vit.ac.in",
        "intercom": "1302"
    },
    "AI_DS": {
        "dept_name": "Department of Artificial Intelligence and Data Science",
        "hod_name": "Dr. Priya M",
        "designation": "Professor & HoD",
        "cabin": "SJT 210",
        "email": "hod.ai@vit.ac.in",
        "intercom": "1210"
    },
    # SITE Departments
    "IT": {
        "dept_name": "Department of Information Technology",
        "hod_name": "Dr. Mythili S",
        "designation": "Professor & HoD",
        "cabin": "SJT 105",
        "email": "hod.it@vit.ac.in",
        "intercom": "1105"
    },
    # SENSE Departments
    "ECE": {
        "dept_name": "Department of Communication Engineering",
        "hod_name": "Dr. Prakash R",
        "designation": "Professor & HoD",
        "cabin": "TT 308",
        "email": "hod.ece@vit.ac.in",
        "intercom": "2308"
    },
    # SELECT Departments
    "EEE": {
        "dept_name": "Department of Electrical Engineering",
        "hod_name": "Dr. Ravi Kumar N",
        "designation": "Professor & HoD",
        "cabin": "TT 202",
        "email": "hod.eee@vit.ac.in",
        "intercom": "2202"
    }
}


def resolve_school_and_dept(program_or_reg: str, school_code: str = "SCOPE") -> Dict[str, Any]:
    """
    Dynamically resolves Dean and HoD metadata for any student logging into VTOP.
    """
    s_code = school_code.strip().upper()
    dean_info = SCHOOL_DEANS.get(s_code, SCHOOL_DEANS["SCOPE"])

    # Resolve Department HoD by program or reg_no pattern
    prog_upper = program_or_reg.upper()
    if "BCI" in prog_upper or "INFO" in prog_upper or "SECURITY" in prog_upper or "IS" in prog_upper:
        dept_key = "INFOSEC"
    elif "BCE" in prog_upper or "CORE" in prog_upper or "CSE" in prog_upper:
        dept_key = "CSE_CORE"
    elif "SWE" in prog_upper or "SOFTWARE" in prog_upper:
        dept_key = "SWE"
    elif "BAI" in prog_upper or "DATA" in prog_upper or "AI" in prog_upper:
        dept_key = "AI_DS"
    elif "BIT" in prog_upper or "IT" in prog_upper:
        dept_key = "IT"
    elif "BEC" in prog_upper or "ECE" in prog_upper:
        dept_key = "ECE"
    elif "BEE" in prog_upper or "EEE" in prog_upper:
        dept_key = "EEE"
    else:
        dept_key = "CSE_CORE"

    hod_info = DEPARTMENT_HODS.get(dept_key, DEPARTMENT_HODS["CSE_CORE"])

    return {
        "dean": dean_info,
        "hod": hod_info
    }
