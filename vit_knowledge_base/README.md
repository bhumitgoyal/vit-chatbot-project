# VIT (Vellore Institute of Technology) Chatbot Knowledge Base

Welcome to the comprehensive, structured Knowledge Base for **Vellore Institute of Technology (VIT)**. This dataset has been created specifically for training, prompt augmentation (RAG), and context indexing for AI Chatbots, Virtual Assistants, and Admissions Portals.

---

## 📁 Repository / Directory Structure

```text
vit_knowledge_base/
├── README.md                              # Guide, Chatbot System Prompt, RAG Setup
├── 01_overview_accreditations_campuses.md # History, NIRF/QS Rankings, Accreditations & Campuses
├── 02_academics_programs_schools.md       # Schools, B.Tech/PG/PhD Programs, FFCS System & VTOP
├── 03_admissions_viteee_counseling.md     # VITEEE Pattern, Eligibility, Counseling & STARS Scheme
├── 04_fee_structure_and_scholarships.md   # Group A/B Fees (Cats 1-5), Hostels, Mess & Scholarships
├── 05_placements_and_careers.md           # CDC, Placement Stats (2024-2026), Super Dream, Recruiters
├── 06_campus_life_hostels_rules_fests.md  # Riviera, graVITas, Hostel Rules, Clubs & Facilities
├── 07_chatbot_qa_dataset.json             # Structured JSON with 50+ Q&A Pairs for Chatbot RAG
└── complete_vit_knowledge_base.md         # Single-file master context document
```

---

## 🤖 Recommended Chatbot System Prompt

When initializing your LLM/Chatbot (e.g. OpenAI GPT-4, Gemini, Claude, Llama-3), use the following system prompt:

```text
You are "VIT-Buddy", the official virtual assistant for Vellore Institute of Technology (VIT). Your job is to assist prospective students, current students, parents, and alumni with accurate, courteous, and precise information regarding VIT.

Core Knowledge Guidelines:
1. Always distinguish between VIT Campuses (Vellore, Chennai, AP Amaravati, Bhopal) when providing location-specific rules or fee details.
2. Clearly explain the Category system (Categories 1 through 5) for VITEEE tuition fee allocation based on merit ranks.
3. For academic questions, reference the Fully Flexible Credit System (FFCS) and VTOP portal.
4. Maintain a polite, helpful, encouraging, and institutional tone.
5. If details depend on specific year updates (e.g., exact VITEEE exam dates), provide the standard framework and direct the user to the official website (https://vit.ac.in).
```

---

## 💡 How to Use This Knowledge Base with RAG Pipelines

1. **Chunking Strategy**:
   - Split Markdown documents by `##` or `###` headings.
   - Recommended chunk size: `500 - 1000 tokens` with `100 token overlap`.
2. **Metadata Tagging**:
   - Tag chunks with metadata: `campus: ["Vellore", "Chennai", "AP", "Bhopal"]`, `topic: ["Fees", "Admissions", "Placements", "Hostel", "FFCS"]`.
3. **JSON QA Dataset**:
   - `07_chatbot_qa_dataset.json` can be directly imported into vector databases (ChromaDB, Pinecone, Qdrant, Weaviate, FAISS) or used for fine-tuning LLMs using Instruction Tuning formats.

---

## 📌 Main Official Portals & Contact Links
- **Official Website**: [https://vit.ac.in](https://vit.ac.in)
- **Student Portal**: VTOP ([https://vtop.vit.ac.in](https://vtop.vit.ac.in))
- **Admissions Portal**: [https://viteee.vit.ac.in](https://viteee.vit.ac.in)
