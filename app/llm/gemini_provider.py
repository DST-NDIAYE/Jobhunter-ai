import json
import re
import httpx
from app.llm.base import GenerationResult, ProviderError


class GeminiProvider:
    """Fournisseur Google Gemini : supporte l'API native generateContent et le endpoint OpenAI de Google AI Studio."""

    def generate(self, *, messages: list[dict], model: str, api_key: str, timeout: float) -> GenerationResult:
        clean_model = model.replace("gemini/", "").strip() or "gemini-1.5-flash"

        # 1. Tentative via l'API native Google AI Studio (fiable, supportée sur toutes les clés)
        try:
            return self._generate_native(messages=messages, model=clean_model, api_key=api_key, timeout=timeout)
        except Exception:
            pass

        # 2. Fallback via le endpoint OpenAI de Google AI Studio
        try:
            from app.llm.base import chat_completion
            return chat_completion(
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                messages=messages,
                model=clean_model,
                api_key=api_key,
                timeout=timeout,
                openai=False,
            )
        except Exception:
            raise ProviderError("Service Gemini indisponible ou réponse invalide.") from None

    def _generate_native(self, *, messages: list[dict], model: str, api_key: str, timeout: float) -> GenerationResult:
        system_text = ""
        user_text = ""
        for m in messages:
            if m.get("role") == "system":
                system_text += m.get("content", "") + "\n"
            else:
                user_text += m.get("content", "") + "\n"

        schema = GenerationResult.model_json_schema()
        instructions = (
            f"Consignes système :\n{system_text}\n\n"
            f"Données et contexte :\n{user_text}\n\n"
            f"Réponds UNIQUEMENT avec un objet JSON valide correspondant au schéma suivant (sans markdown superflu) :\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": instructions}],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        with httpx.Client(timeout=httpx.Timeout(min(max(timeout, 1), 60), connect=10), follow_redirects=False) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise ValueError("Aucun candidat retourné par Gemini.")
            parts = candidates[0].get("content", {}).get("parts") or []
            if not parts:
                raise ValueError("Contenu vide retourné par Gemini.")
            raw_text = parts[0].get("text", "").strip()
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                raw_text = re.sub(r"\s*```$", "", raw_text)
            return GenerationResult.model_validate_json(raw_text)

