from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"

CHROMA_DIR = DATA_DIR / "chroma_db"

COLLECTION_NAME = "company_policy"

EMBEDDING_MODEL = "BAAI/bge-small-zh"

LLM_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-sol")

RETRIEVAL_DISTANCE_THRESHOLD = 0.36
