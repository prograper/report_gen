from pathlib import Path
import json, os, pandas as pd
from jinja2 import Template
from agents.registry import register_extractor
# ← 取 client、model
from llm_client import apply_provider

TYPE_MAP = {
    "number": {"type": "number"},
    "string": {"type": "string"},
    "array[string]": {"type": "array", "items": {"type": "string"}},
}

def df_to_text(df: pd.DataFrame) -> str:
    return df.to_csv(index=False)

@register_extractor
class GenericExtractor:
    """
    DataFrame + prompt + keys  →  JSON
    provider 缺省看环境变量 LLM_PROVIDER，默认 openai
    """

    def __init__(self, df, keys: dict, prompt_path: str | Path, provider: str | None = None, config_dir: Path = Path("")):
        self.df          = df
        self.keys        = keys
        self.prompt_path = Path(prompt_path)
        provider         = provider or os.getenv("LLM_PROVIDER", "openai")
        # 👈 获取 client
        self.client, self.model_name = apply_provider(provider, config_dir)

    # ---------- helpers ----------
    def _build_schema(self):
        props = {k: TYPE_MAP.get(t, {"type": "string"}) for k, t in self.keys.items()}
        return {
            "name": "extract",
            "parameters": {"type": "object",
                           "properties": props,
                           "required": list(props)}
        }

    def _render_prompt(self) -> str:
        tpl = Template(self.prompt_path.read_text(encoding="utf-8"))
        return tpl.render(table=df_to_text(self.df), keys=list(self.keys))

    # ---------- public ----------
    def extract(self) -> dict:
        prompt  = self._render_prompt()
        schema  = self._build_schema()
        resp = self.client.chat.completions.create(
            model         = self.model_name,
            messages      = [{"role": "system", "content": prompt}],
            functions     = [schema],
            function_call = {"name": "extract"},
        )
        return json.loads(resp.choices[0].message.function_call.arguments)
