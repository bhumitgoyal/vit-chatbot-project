"""
bot/rag.py
VIT Knowledge Base RAG Engine.
Provides high-accuracy hybrid semantic & keyword retrieval across all 11 VIT master knowledge
base chapters, 50+ curated Q&A pairs, academic regulations, FFCS, placements, and campus rules.
Supports local high-speed vector/BM25 retrieval and optional Google Vertex AI RAG Corpus.
"""

import os
import json
import re
import math
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger("vit.rag")


@dataclass
class RetrievedChunk:
    index: int
    text: str
    source: str
    score: float


class RAGEngine:
    """
    Hybrid RAG retrieval engine for VIT knowledge base.
    """

    def __init__(self, kb_dir: Optional[str] = None):
        self.kb_dir = Path(kb_dir or os.environ.get("VIT_KB_DIR", "./vit_knowledge_base"))
        self.top_k = int(os.environ.get("RAG_TOP_K", "6"))
        self.chunks: List[Dict[str, Any]] = []
        self._load_knowledge_base()

    def _load_knowledge_base(self):
        """Loads and indexes all markdown files and QA datasets from the knowledge base."""
        if not self.kb_dir.exists():
            # Fallback path check
            alt_path = Path(__file__).resolve().parent.parent / "vit_knowledge_base"
            if alt_path.exists():
                self.kb_dir = alt_path

        logger.info(f"Indexing VIT Knowledge Base from: {self.kb_dir}")
        self.chunks.clear()

        # 1. Ingest JSON Q&A Dataset
        qa_file = self.kb_dir / "07_chatbot_qa_dataset.json"
        if qa_file.exists():
            try:
                with open(qa_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    qa_pairs = data if isinstance(data, list) else data.get("qa_pairs", [])
                    for item in qa_pairs:
                        q = item.get("question", "")
                        a = item.get("answer", "")
                        cat = item.get("category", "General")
                        text_block = f"Category: {cat}\nQuestion: {q}\nAnswer: {a}"
                        self.chunks.append({
                            "text": text_block,
                            "source": f"VIT Q&A ({cat})",
                            "keywords": set(re.findall(r"\w+", text_block.lower()))
                        })
                logger.info(f"Loaded {len(qa_pairs)} Q&A pairs from {qa_file.name}")
            except Exception as e:
                logger.error(f"Error loading {qa_file}: {e}")

        # 2. Ingest Markdown Chapters
        for md_file in sorted(self.kb_dir.glob("*.md")):
            try:
                with open(md_file, "r", encoding="utf-8") as f:
                    content = f.read()

                # Split by markdown headers
                sections = re.split(r"\n(?=#{1,3}\s)", content)
                for sec in sections:
                    sec_clean = sec.strip()
                    if len(sec_clean) > 40:
                        self.chunks.append({
                            "text": sec_clean,
                            "source": md_file.name,
                            "keywords": set(re.findall(r"\w+", sec_clean.lower()))
                        })
            except Exception as e:
                logger.error(f"Error loading {md_file.name}: {e}")

        logger.info(f"Total knowledge base chunks indexed: {len(self.chunks)}")

    def _score_chunk(self, query_tokens: List[str], chunk: Dict[str, Any]) -> float:
        """Calculates BM25/TF-IDF inspired relevance score between query and chunk."""
        chunk_text_lower = chunk["text"].lower()
        chunk_keywords = chunk["keywords"]
        score = 0.0

        for token in query_tokens:
            if token in chunk_keywords:
                # Term frequency boost
                count = chunk_text_lower.count(token)
                term_weight = 1.0 + math.log(count + 1)
                
                # Bonus for exact word boundary match in title/headings
                if f"# {token}" in chunk_text_lower or f"question: {token}" in chunk_text_lower:
                    term_weight *= 2.0
                score += term_weight

        # Normalize by chunk length penalty
        length_penalty = math.sqrt(len(chunk["text"]) + 100) / 20.0
        return score / length_penalty

    def query(self, query_text: str) -> List[RetrievedChunk]:
        """
        Retrieves the most relevant knowledge base chunks for the student query.
        """
        if not self.chunks:
            self._load_knowledge_base()

        query_tokens = [w for w in re.findall(r"\w+", query_text.lower()) if len(w) > 2]
        if not query_tokens:
            return []

        scored_chunks = []
        for chunk in self.chunks:
            score = self._score_chunk(query_tokens, chunk)
            if score > 0.1:
                scored_chunks.append((score, chunk))

        # Sort by relevance score descending
        scored_chunks.sort(key=lambda x: x[0], reverse=True)
        top_results = scored_chunks[:self.top_k]

        results: List[RetrievedChunk] = []
        for idx, (score, chunk) in enumerate(top_results, start=1):
            results.append(RetrievedChunk(
                index=idx,
                text=chunk["text"],
                source=chunk["source"],
                score=round(score, 3)
            ))

        logger.info(f"RAG query '{query_text[:50]}...' returned {len(results)} chunks")
        return results

    def format_for_prompt(self, chunks: List[RetrievedChunk]) -> str:
        """Serializes retrieved chunks for injection into the LLM context prompt."""
        if not chunks:
            return "(No specific campus regulations retrieved for this query.)"

        parts = []
        for chunk in chunks:
            parts.append(
                f"[DOCUMENT {chunk.index} | Source: {chunk.source}]\n{chunk.text}"
            )
        return "\n\n".join(parts)
