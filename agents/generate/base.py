import os
from jinja2 import Template
from agents.registry import register_generator
from llm_client import apply_provider

@register_generator
class GenericParagraphGenerator:
    """
    prompt_path + context  → 段落文本
    """

    def __init__(self, prompt_path: str, context: dict, provider: str | None = None):
        self.prompt_path = prompt_path
        self.context     = context
        provider         = provider or os.getenv("LLM_PROVIDER", "openai")
        self.client, self.model_name = apply_provider(provider)

    # ---------- core ----------
    def generate(self) -> str:
        # prompt = Template(open(self.prompt_path, encoding="utf-8").read()).render(**self.context)

        # self.context 现在形如 {"data": {...嵌套...}}
        prompt = Template(
            open(self.prompt_path, encoding="utf-8").read()
        ).render(**self.context)
        resp   = self.client.chat.completions.create(
                    model    = self.model_name,
                    messages = [{"role": "system", "content": prompt}]
                 )
        return resp.choices[0].message.content.strip()
