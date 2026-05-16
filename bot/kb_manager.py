import os
import logging
from google.cloud import firestore
import vertexai
from vertexai.preview import rag
from vertexai.generative_models import GenerativeModel
import asyncio

logger = logging.getLogger("gohappy.kb_manager")

class KnowledgeBaseManager:
    """Manages the Master KB in Firestore, generating updates via Gemini, and syncing to Vertex AI RAG."""
    
    def __init__(self):
        self.db_id = os.environ.get("FIRESTORE_DB", "(default)")
        self.project = os.environ["GCP_PROJECT_ID"]
        self.firestore_project = os.environ.get("FIRESTORE_PROJECT_ID", self.project)
        self.location = os.environ.get("GCP_LOCATION", "us-central1")
        self.corpus_name = os.environ["VERTEX_RAG_CORPUS"]

        self.db = firestore.AsyncClient(
            project=self.firestore_project,
            database=self.db_id,
        )
        self.col = self.db.collection("system_data")
        self.doc_ref = self.col.document("knowledge_base")
        
        vertexai.init(project=self.project, location=self.location)
        self.model = GenerativeModel(os.environ.get("GEMINI_MODEL", "gemini-1.5-pro-002"))

    async def get_master_kb(self) -> str:
        """Fetch the current master KB from Firestore."""
        snap = await self.doc_ref.get()
        if snap.exists:
            return snap.to_dict().get("content", "")
        else:
            # Fallback to SYSTEM_PROMPT if not found
            from bot.llm import SYSTEM_PROMPT
            return SYSTEM_PROMPT

    async def generate_update(self, admin_input: str) -> str:
        """Use Gemini to apply the admin input to the existing KB and return the new markdown."""
        current_kb = await self.get_master_kb()
        
        prompt = f"""
You are managing the knowledge base for the GoHappy Club chatbot.
Here is the current master knowledge base document:

```markdown
{current_kb}
```

The admin has requested the following update:
"{admin_input}"

Your task is to integrate this update into the knowledge base document appropriately. 
- YOU MUST preserve absolutely ALL existing content from the previous knowledge base without modification, unless it directly contradicts the admin's requested update.
- Only modify the required part and leave everything else entirely intact. Do not delete, truncate, or summarize unrelated sections.
- Output ONLY the full updated markdown document. Do not include introductory text, conversational filler, or markdown code fences (```markdown) around the output. 
The output should be exactly the raw markdown content of the new knowledge base.
"""
        response = await self.model.generate_content_async(prompt)
        new_kb = response.text.strip()
        # Remove markdown fences if model included them
        if new_kb.startswith("```markdown"):
            new_kb = new_kb[len("```markdown"):].strip()
        if new_kb.startswith("```"):
            new_kb = new_kb[3:].strip()
        if new_kb.endswith("```"):
            new_kb = new_kb[:-3].strip()
            
        return new_kb

    async def save_pending_update(self, new_kb_content: str):
        """Save a pending update to Firestore for approval."""
        await self.col.document("kb_pending").set({
            "content": new_kb_content,
            "timestamp": firestore.SERVER_TIMESTAMP
        })

    async def approve_and_sync(self) -> bool:
        """
        Approves the pending update:
        1. Promotes pending to master in Firestore
        2. Syncs to Vertex AI RAG Corpus (deletes old files, uploads new)
        """
        pending_snap = await self.col.document("kb_pending").get()
        if not pending_snap.exists:
            logger.error("No pending KB update found.")
            return False
            
        new_content = pending_snap.to_dict().get("content", "")
        
        # 1. Save locally to upload
        file_path = "/tmp/GoHappyClub_KnowledgeBase.md"
        with open(file_path, "w") as f:
            f.write(new_content)
            
        # 2. Sync to Vertex AI RAG
        try:
            # Delete existing files in corpus to replace them
            files = list(rag.list_files(corpus_name=self.corpus_name))
            for file_info in files:
                logger.info("Deleting old RAG file: %s", file_info.name)
                rag.delete_file(name=file_info.name)
                
            # Upload new file with retry logic to handle flaky Vertex AI JSON errors
            logger.info("Uploading new KB to RAG Corpus: %s", self.corpus_name)
            for attempt in range(3):
                try:
                    rag.upload_file(
                        corpus_name=self.corpus_name,
                        path=file_path,
                        display_name="GoHappyClub_KnowledgeBase.md"
                    )
                    break # Success
                except Exception as upload_e:
                    logger.warning("Upload attempt %d failed: %s", attempt + 1, upload_e)
                    if attempt == 2:
                        raise upload_e
                    await asyncio.sleep(2)
        except Exception as e:
            logger.error("Failed to sync to Vertex AI RAG: %s", e)
            return False
            
        # 3. Promote in Firestore
        await self.doc_ref.set({"content": new_content, "updated_at": firestore.SERVER_TIMESTAMP})
        await self.col.document("kb_pending").delete()
        
        return True
