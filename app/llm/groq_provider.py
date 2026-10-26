from app.llm.base import chat_completion


class GroqProvider:
    """Fournisseur Groq gratuit et ultra-rapide (Llama 3.3 70B, etc.)."""

    def generate(self, **kwargs):
        return chat_completion(
            "https://api.groq.com/openai/v1/chat/completions",
            openai=True,
            **kwargs,
        )
