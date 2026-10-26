from app.llm.base import chat_completion


class MistralProvider:
    def generate(self, **kwargs):
        return chat_completion("https://api.mistral.ai/v1/chat/completions", **kwargs)
