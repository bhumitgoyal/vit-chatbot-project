"""
One-shot script: push the local gohappy_club_knowledge_base.md to Vertex AI RAG.
Run from the project root with credentials already set up:
  python sync_kb_to_rag.py
"""

import os
import time
import asyncio
from dotenv import load_dotenv

load_dotenv()

import vertexai
from vertexai.preview import rag

KB_FILE   = os.path.join(os.path.dirname(__file__), "gohappy_club_knowledge_base.md")
PROJECT   = os.environ["GCP_PROJECT_ID"]
LOCATION  = os.environ.get("GCP_LOCATION", "us-central1")
CORPUS    = os.environ["VERTEX_RAG_CORPUS"]


def main():
    print(f"Project  : {PROJECT}")
    print(f"Location : {LOCATION}")
    print(f"Corpus   : {CORPUS}")
    print(f"KB file  : {KB_FILE}\n")

    vertexai.init(project=PROJECT, location=LOCATION)

    # 1. Delete all existing files in the corpus
    files = list(rag.list_files(corpus_name=CORPUS))
    if files:
        print(f"Deleting {len(files)} existing file(s)...")
        for f in files:
            rag.delete_file(name=f.name)
            print(f"  Deleted: {f.name}")
    else:
        print("No existing files found in corpus.")

    # 2. Upload the updated KB file (retry up to 3 times)
    print(f"\nUploading {KB_FILE} ...")
    for attempt in range(1, 4):
        try:
            rag.upload_file(
                corpus_name=CORPUS,
                path=KB_FILE,
                display_name="GoHappyClub_KnowledgeBase.md",
            )
            print("Upload successful.")
            break
        except Exception as e:
            print(f"  Attempt {attempt} failed: {e}")
            if attempt == 3:
                print("All retries exhausted. RAG sync FAILED.")
                raise
            time.sleep(2)

    print("\nDone. Vertex AI RAG corpus updated.")


if __name__ == "__main__":
    main()
