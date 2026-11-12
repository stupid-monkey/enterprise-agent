import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


# Project root
BASE_DIR = Path(__file__).resolve().parents[2]

# 加载项目根目录的 .env
load_dotenv(BASE_DIR / ".env")

# 统一模型名称
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-6-sol")


def create_llm_client(*, timeout: float = 60.0, max_retries: int = 2):

    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if not api_key:
        raise ValueError("未找到 OPENAI_API_KEY，请检查项目根目录的 .env 文件。")

    options = dict(
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
    )
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if base_url:
        options["base_url"] = base_url
    client = OpenAI(**options)

    return client
