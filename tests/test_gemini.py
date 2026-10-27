"""Tests pour le fournisseur Google Gemini."""

import unittest
from unittest.mock import patch, MagicMock

from pydantic import BaseModel

from app.llm.base import ProviderError
from app.llm.gemini_provider import GeminiProvider


class Reponse(BaseModel):
    statut: str
    mots: list[str]


class TestGeminiProvider(unittest.TestCase):
    def test_gemini_native_success(self):
        p = GeminiProvider()
        fake_response = {"candidates": [{"content": {"parts": [{"text": '{"statut": "ok", "mots": ["Python"]}'}]}}]}
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.Client.post", return_value=mock_resp):
            res = p.generate(messages=[{"role": "user", "content": "test"}], model="gemini-test-flash",
                             api_key="test-key", timeout=10, schema=Reponse)
        self.assertEqual((res.statut, res.mots), ("ok", ["Python"]))

    def test_gemini_fallback_to_openai_endpoint_when_native_fails(self):
        p = GeminiProvider()
        with patch.object(p, "_generate_native", side_effect=Exception("Native 403")), \
             patch("app.llm.base.chat_completion", return_value=Reponse(statut="secours", mots=[])) as mock_cc:
            res = p.generate(messages=[{"role": "user", "content": "test"}], model="gemini-test-flash",
                             api_key="test-key", timeout=10, schema=Reponse)
        self.assertEqual(res.statut, "secours")
        mock_cc.assert_called_once()

    def test_missing_model_is_a_clear_error(self):
        with self.assertRaisesRegex(ProviderError, "Aucun modèle Gemini"):
            GeminiProvider().generate(messages=[], model="", api_key="k", timeout=5, schema=Reponse)


if __name__ == "__main__":
    unittest.main()
