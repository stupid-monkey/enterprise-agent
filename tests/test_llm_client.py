import os
import unittest
from unittest.mock import patch

from app.llm.client import create_llm_client


class LlmClientTests(unittest.TestCase):
    def test_optional_compatible_base_url_is_forwarded(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
        }), patch("app.llm.client.OpenAI") as client_class:
            create_llm_client()
        self.assertEqual(client_class.call_args.kwargs["base_url"], "https://example.invalid/v1")

    def test_default_endpoint_when_base_url_is_empty(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "",
        }), patch("app.llm.client.OpenAI") as client_class:
            create_llm_client()
        self.assertNotIn("base_url", client_class.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
