from app.llm.base import chat_completion


class OpenAIProvider:
    def generate(self, **kwargs):
        return chat_completion("https://api.openai.com/v1/chat/completions", openai=True, **kwargs)
