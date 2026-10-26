"""Vérifications ciblées, sans réseau réel ni accès à la base personnelle."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from app.application_generator import generate_preparation
from app.config import Settings
from app.database import init_db, add_job
from app.applications import get_offer, save_preparation
from app.llm import resolve_provider
from app.llm.base import GenerationResult


CV = "Alex Exemple\nProjet Python : analyse de données."
JOB = {"external_id": "test-llm", "title": "Data Analyst", "company": "Exemple",
       "description": "Python et SQL", "personal_notes": "NE PAS TRANSMETTRE"}
RESULT = {"fit_summary": "Le projet Python est pertinent.", "matching_skills": ["Python"],
          "relevant_cv_points": ["Projet Python : analyse de données."], "missing_skills": ["SQL"],
          "short_message": "Bonjour,\nVotre poste m'intéresse.\nMon CV mentionne un projet Python.\nÉchangeons sur vos besoins.",
          "cover_letter": "Votre offre porte sur l'analyse de données. Mon CV présente un projet Python. Je reste disponible pour en discuter."}


class LLMChecks(unittest.TestCase):
    def generate(self, settings=None, **kwargs):
        with patch("app.application_generator.extract_cv_text", return_value=CV):
            return generate_preparation(JOB, settings or Settings(), **kwargs)

    def test_local_and_auto_without_key_never_call_network(self):
        with patch("httpx.Client.post", side_effect=AssertionError("Réseau interdit")) as post:
            for provider in ("local", "auto", "mistral", "openai"):
                self.assertEqual(self.generate(Settings(llm_provider=provider))["provider"], "local")
            post.assert_not_called()
        settings = Settings(llm_provider="auto", llm_model="openai/gpt-4o-mini", llm_api_key="dummy-test-key")
        self.assertEqual(resolve_provider(settings), ("openai", "gpt-4o-mini"))
        self.assertEqual(resolve_provider(settings, "local"), (None, ""))

    def test_both_providers_structured_response_and_minimal_payload(self):
        for provider in ("openai", "mistral"):
            with self.subTest(provider=provider):
                settings = Settings(llm_provider=provider, llm_model="test-model", llm_api_key="dummy-test-key")
                response = httpx.Response(200, request=httpx.Request("POST", "https://example.test"),
                                         json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(RESULT)}}]})
                with patch("httpx.Client.post", return_value=response) as post:
                    draft = self.generate(settings)
                self.assertEqual(draft["provider"], provider)
                self.assertEqual(draft["analysis"]["missing_skills"], ["SQL"])
                payload = post.call_args.kwargs["json"]
                self.assertNotIn("dummy-test-key", json.dumps(payload))
                self.assertNotIn("NE PAS TRANSMETTRE", json.dumps(payload))
                self.assertEqual(payload["response_format"]["type"], "json_schema")
                self.assertEqual(post.call_count, 1)

    def test_provider_errors_and_invalid_answers_fall_back(self):
        settings = Settings(llm_provider="openai", llm_model="test-model", llm_api_key="dummy-test-key")
        for error in (httpx.ReadTimeout("secret error"), httpx.HTTPError("quota"), ValueError("bad JSON")):
            with self.subTest(error=type(error).__name__), patch("httpx.Client.post", side_effect=error):
                draft = self.generate(settings)
                self.assertEqual(draft["provider"], "local")
                self.assertIn("version locale", draft["notice"])
                self.assertNotIn("secret error", str(draft))
        for content in ('{}', '{"short_message": 12}', 'not JSON'):
            response = httpx.Response(200, request=httpx.Request("POST", "https://example.test"),
                                     json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
            with patch("httpx.Client.post", return_value=response):
                self.assertEqual(self.generate(settings)["provider"], "local")

    def test_invented_skill_or_cv_quote_is_rejected(self):
        settings = Settings(llm_provider="mistral", llm_model="test-model", llm_api_key="dummy-test-key")
        for change in ({"matching_skills": ["Python", "SQL"]},
                       {"relevant_cv_points": ["Directeur pendant 10 ans"]}):
            result = GenerationResult(**{**RESULT, **change})
            with patch("app.llm.mistral_provider.MistralProvider.generate", return_value=result):
                self.assertEqual(self.generate(settings)["provider"], "local")

    def test_saved_content_and_provenance_survive_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            init_db(path)
            add_job(JOB, path)
            draft = self.generate()
            save_preparation(JOB["external_id"], draft["message"], draft["letter"], db_path=path, generated=draft)
            saved = get_offer(JOB["external_id"], db_path=path)
            regenerated = self.generate(variant=1)
            self.assertNotEqual(saved["application_message"], regenerated["message"])
            init_db(path)
            self.assertEqual(saved, get_offer(JOB["external_id"], db_path=path))
            self.assertEqual(saved["application_generation_provider"], "local")
            self.assertEqual(saved["application_generation_model"], "")


if __name__ == "__main__":
    unittest.main()
