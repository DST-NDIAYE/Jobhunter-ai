"""Nouveaux essais sur surcharge (429/503) et modèle de secours, sans réseau ni attente réelle."""

import unittest
from unittest.mock import patch

import httpx

from app import llm
from app.config import Settings
from app.llm.base import ProviderError, RetryableProviderError, chat_completion
from app.llm.gemini_provider import GeminiProvider


class ScriptedProvider:
    """Joue une suite de résultats (exception ou valeur) et note le modèle de chaque appel."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.models = []

    def generate(self, *, model, **kwargs):
        self.models.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RetryTests(unittest.TestCase):
    def run_with(self, outcomes, fallback=""):
        provider = ScriptedProvider(outcomes)
        settings = Settings(llm_api_key="cle-test", llm_fallback_model=fallback)
        with patch.dict(llm.PROVIDERS, {"fake": provider}), patch("app.llm.time.sleep") as sleep:
            try:
                return llm.generate_with_retry("fake", "principal", settings, messages=[]), provider, sleep
            except ProviderError as exc:
                return exc, provider, sleep

    def test_overload_is_retried_with_increasing_delays(self):
        busy = RetryableProviderError("surcharge")
        result, provider, sleep = self.run_with([busy, busy, "réponse"])
        self.assertEqual(result, "réponse")
        self.assertEqual(provider.models, ["principal"] * 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 5.0])

    def test_three_failures_then_fallback_model(self):
        busy = RetryableProviderError("surcharge")
        result, provider, _ = self.run_with([busy, busy, busy, "réponse de secours"], fallback="secours")
        self.assertEqual(result, "réponse de secours")
        self.assertEqual(provider.models, ["principal"] * 3 + ["secours"])

    def test_without_fallback_the_overload_error_is_raised(self):
        busy = RetryableProviderError("surcharge")
        result, provider, _ = self.run_with([busy, busy, busy])
        self.assertIsInstance(result, RetryableProviderError)
        self.assertEqual(len(provider.models), 3)

    def test_other_errors_are_not_retried_but_use_fallback(self):
        result, provider, sleep = self.run_with([ProviderError("modèle retiré"), "ok"], fallback="fake/secours")
        self.assertEqual(result, "ok")
        # Le préfixe « fournisseur/ » du modèle de secours est retiré.
        self.assertEqual(provider.models, ["principal", "secours"])
        sleep.assert_not_called()


def respond(status):
    def post(self, url, *args, **kwargs):
        return httpx.Response(status, json={"error": {"code": status}}, request=httpx.Request("POST", url))
    return post


class StatusClassificationTests(unittest.TestCase):
    def call_chat(self):
        chat_completion("https://exemple.invalid/v1/chat/completions", messages=[], model="m",
                        api_key="cle-test", timeout=5)

    def call_gemini(self):
        GeminiProvider().generate(messages=[{"role": "user", "content": "x"}], model="gemini-test",
                                  api_key="cle-test", timeout=5)

    def test_429_and_503_are_retryable(self):
        for status in (429, 503):
            with self.subTest(status=status), patch("httpx.Client.post", respond(status)):
                with self.assertRaises(RetryableProviderError):
                    self.call_chat()
                with self.assertRaises(RetryableProviderError):
                    self.call_gemini()

    def test_404_is_not_retryable(self):
        with patch("httpx.Client.post", respond(404)):
            for call in (self.call_chat, self.call_gemini):
                with self.assertRaises(ProviderError) as ctx:
                    call()
                self.assertNotIsInstance(ctx.exception, RetryableProviderError)


if __name__ == "__main__":
    unittest.main()
