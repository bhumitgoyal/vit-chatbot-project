"""
bot/faculty_service.py
VIT Master Faculty & Administration Directory for VITopia AI.
Verified against live VTOP institutional leadership records.
"""

import re
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("vit.faculty_service")

# Master VIT Leadership & Faculty Directory
FACULTY_DIRECTORY: List[Dict[str, Any]] = [
    # ── Official SCOPE Leadership (Verified from VTOP) ──
    {
        "name": "Dr. JAISANKAR N",
        "aliases": ["JAISANKAR N", "JAISANKAR", "DR. JAISANKAR", "DEAN SCOPE", "DEAN"],
        "designation": "Professor Higher Academic Grade & Dean",
        "school": "SCOPE - School of Computer Science and Engineering",
        "email": "dean.scope@vit.ac.in",
        "cabin": "SJT 321",
        "intercom": "1321",
        "research_areas": ["Cloud Security", "Information Systems", "Distributed Computing"]
    },
    {
        "name": "Dr. RAJESHKANNAN R",
        "aliases": ["RAJESHKANNAN R", "RAJESHKANNAN", "DR. RAJESHKANNAN", "HOD INFOSEC", "HOD IS", "HOD"],
        "designation": "Professor Grade 1 & Head of Department (Information Security)",
        "school": "SCOPE - Department of Information Security",
        "email": "hod.is@vit.ac.in",
        "cabin": "SJT 411 A0 / SJT 322",
        "intercom": "1411",
        "research_areas": ["Information Security", "Network Security", "Cryptography", "Applied Cryptanalysis"]
    },

    # ── Course & Project Faculty (Verified) ──
    {
        "name": "Dr. Ushus Elizebeth Zachariah",
        "aliases": ["USHUS ELIZEBETH ZACHARIAH", "USHUS", "USHUS E Z", "DR. USHUS", "PROJECT GUIDE"],
        "designation": "Associate Professor Grade-1",
        "school": "SCOPE - School of Computer Science and Engineering",
        "email": "ushus.elizabeth@vit.ac.in",
        "cabin": "SJT-413-A12",
        "intercom": "1412",
        "research_areas": ["Natural Language Processing", "RAG & LLM Agents", "Information Retrieval", "AI Systems"]
    },
    {
        "name": "Dr. Meenakshi S P",
        "aliases": ["MEENAKSHI S P", "MEENAKSHI", "DR. MEENAKSHI"],
        "designation": "Associate Professor",
        "school": "SCOPE - School of Computer Science and Engineering",
        "email": "meenakshi.sp@vit.ac.in",
        "cabin": "SJT-512-B04",
        "intercom": "1512",
        "research_areas": ["Web Application Security", "Cyber Security", "Network Protocols", "Cloud Security"]
    },
    {
        "name": "Dr. Sunija A P",
        "aliases": ["SUNIJA A P", "SUNIJA", "DR. SUNIJA"],
        "designation": "Assistant Professor (Senior)",
        "school": "SCOPE - School of Computer Science and Engineering",
        "email": "sunija.ap@vit.ac.in",
        "cabin": "SJT-614-C08",
        "intercom": "1614",
        "research_areas": ["Malware Analysis", "Reverse Engineering", "Static & Dynamic Analysis", "Endpoint Security"]
    },
    {
        "name": "Dr. Kumaresan A",
        "aliases": ["KUMARESAN A", "KUMARESAN", "DR. KUMARESAN"],
        "designation": "Associate Professor",
        "school": "SCOPE - School of Computer Science and Engineering",
        "email": "kumaresan.a@vit.ac.in",
        "cabin": "SJT-608-A15",
        "intercom": "1608",
        "research_areas": ["Digital Forensics", "Memory Forensics", "Incident Response", "Cryptography"]
    },
    {
        "name": "Dr. Soumyajyoti Dey",
        "aliases": ["SOUMYAJYOTI DEY", "SOUMYAJYOTI", "DEY", "DR. DEY"],
        "designation": "Assistant Professor (Senior)",
        "school": "SCOPE - School of Computer Science and Engineering",
        "email": "soumyajyoti.dey@vit.ac.in",
        "cabin": "SJT-505-B02",
        "intercom": "1505",
        "research_areas": ["Digital Watermarking", "Steganography", "Multimedia Security", "Signal Processing"]
    },
    {
        "name": "Dr. Satish C.J",
        "aliases": ["SATISH C.J", "SATISH C J", "SATISH", "DR. SATISH"],
        "designation": "Associate Professor",
        "school": "SCOPE - School of Computer Science and Engineering",
        "email": "satish.cj@vit.ac.in",
        "cabin": "SJT-502-A09",
        "intercom": "1502",
        "research_areas": ["Information Security", "Cryptography & Network Security", "Data Privacy"]
    },
    {
        "name": "Dr. Priyadharshini G",
        "aliases": ["PRIYADHARSHINI G", "PRIYADHARSHINI", "DR. PRIYADHARSHINI"],
        "designation": "Assistant Professor (Senior)",
        "school": "SCOPE - School of Computer Science and Engineering",
        "email": "priyadharshini.g@vit.ac.in",
        "cabin": "SJT-520-C11",
        "intercom": "1520",
        "research_areas": ["Internet & Web Programming", "Full Stack Architectures", "Distributed Web Systems"]
    }
]


class FacultyService:
    def __init__(self):
        self.directory = FACULTY_DIRECTORY

    def search_faculty(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        q = query.strip().lower()
        if len(q) < 3:
            return []

        results = []
        for fac in self.directory:
            score = 0
            fac_name_lower = fac["name"].lower()
            
            if q in fac_name_lower:
                score += 10
            for alias in fac.get("aliases", []):
                if q in alias.lower():
                    score += 8
                    break
            if any(q in r.lower() for r in fac.get("research_areas", [])):
                score += 4
            if q in fac.get("school", "").lower():
                score += 2
            if q in fac.get("designation", "").lower():
                score += 3

            if score > 0:
                results.append((score, fac))

        results.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in results[:limit]]

    def get_faculty_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        matches = self.search_faculty(name, limit=1)
        return matches[0] if matches else None

    def format_faculty_card(self, fac: Dict[str, Any]) -> str:
        lines = [f"### 👨‍🏫 {fac['name']}"]
        if fac.get("designation"):
            lines.append(f"- **Designation:** {fac['designation']}")
        if fac.get("school"):
            lines.append(f"- **School / Dept:** {fac['school']}")
        if fac.get("email"):
            lines.append(f"- **Email:** [{fac['email']}](mailto:{fac['email']})")
        if fac.get("cabin"):
            intercom = f" (Intercom: {fac['intercom']})" if fac.get("intercom") else ""
            lines.append(f"- **Cabin:** {fac['cabin']}{intercom}")
        return "\n".join(lines)
