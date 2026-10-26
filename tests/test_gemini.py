"""Tests pour le fournisseur Google Gemini."""

import unittest
from unittest.mock import patch, MagicMock
from app.llm.gemini_provider import GeminiProvider
from app.llm.base import GenerationResult


class TestGeminiProvider(unittest.TestCase):
    def test_gemini_native_success(self):
        p = GeminiProvider()
        fake_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"fit_summary": "Excellent match", "matching_skills": ["Python"], "relevant_cv_points": ["Python Dev 2023"], "missing_skills": ["Docker"], "short_message": "Bonjour, voici ma candidature.", "cover_letter": "Madame, Monsieur, je postule avec enthousiasme."}'
                    }]
                }
            }]
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.Client.post", return_value=mock_resp):
            res = p.generate(
                messages=[{"role": "user", "content": "test"}],
                model="gemini-1.5-flash",
                api_key="test-key",
                timeout=10,
            )
            self.assertEqual(res.fit_summary, "Excellent match")
            self.assertEqual(res.matching_skills, ["Python"])
            self.assertEqual(res.missing_skills, ["Docker"])

    def test_gemini_fallback_to_openai_endpoint_when_native_fails(self):
        p = GeminiProvider()
        openai_mock_result = GenerationResult(
            fit_summary="OpenAI fallback match",
            matching_skills=["Python"],
            relevant_cv_points=["Python Dev 2023"],
            missing_skills=["Docker"],
            short_message="Bonjour via fallback.",
            cover_letter="Madame, Monsieur via fallback.",
        )

        with patch.object(p, "_generate_native", side_effect=Exception("Native 403")), \
             patch("app.llm.base.chat_completion", return_value=openai_mock_result) as mock_cc:
            res = p.generate(
                messages=[{"role": "user", "content": "test"}],
                model="gemini-1.5-flash",
                api_key="test-key",
                timeout=10,
            )
            self.assertEqual(res.fit_summary, "OpenAI fallback match")
            mock_cc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
