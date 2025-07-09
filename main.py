"""
入口脚本：解析 Excel → 结构化抽取 → 生成段落 → 写入 Word
兼容任何遵循 OpenAI Chat Completion & Function-Calling 协议的 LLM。
"""

import os, sys, yaml, pandas as pd
from pathlib import Path
from docx import Document
from agents.registry import get_extractor
from agents.generate import get_generator

# ───── 目录常量 ───────────────────────────────────────────
ROOT       = Path(__file__).parent
CFG_DIR    = ROOT / "configs"
PROMPT_DIR = ROOT / "prompts"
TPL_DIR    = ROOT / "templates"

# ───── 实用函数 ───────────────────────────────────────────
def load_yaml(fname: str):
    with open(CFG_DIR / fname, encoding="utf-8") as f:
        return yaml.safe_load(f)

def read_prompt(path: str):
    return (ROOT / path).read_text(encoding="utf-8")

def write_docx(paragraph_map: dict, out_path: str):
    doc = Document(TPL_DIR / "report_template.docx")
    for para_id, texts in paragraph_map.items():
        placeholder = "{{" + placeholder_map[para_id] + "}}"
        for p in doc.paragraphs:
            if placeholder in p.text:
                p.text = p.text.replace(placeholder, "")
                for seg in texts:
                    run = p.add_run(seg)
                    run.add_break()
                break
    doc.save(out_path)

# ───── 读取配置 ───────────────────────────────────────────
sheet_cfg   = load_yaml("sheet_tasks.yaml")       # 每张 Sheet 抽取任务
paragraphs  = load_yaml("paragraph_tasks.yaml")   # 段落生成任务
placeholder_map = load_yaml("doc_placeholders.yaml")

# ───── Pipeline ─────────────────────────────────────────
def run_pipeline(excel_path: str, out_doc: str):
    xls = pd.ExcelFile(excel_path)
    extracted = {}                      # 全局字段仓库
    # 1) 遍历 Sheet 抽取
    for sheet in xls.sheet_names:
        if sheet not in sheet_cfg:
            continue
        cfg     = sheet_cfg[sheet]
        df      = xls.parse(sheet)
        extractor = get_extractor("GenericExtractor")(
            df          = df,
            keys        = cfg["keys"],
            prompt_path = cfg["prompt"],
            provider = "qwen"
        )
        extracted.update(extractor.extract())

    # 2) 生成段落
    para_out = {pid: [] for pid in paragraphs}
    for pid, task in paragraphs.items():
        # 取所需字段
        ctx = {k: extracted.get(k) for k in task["keys"]}
        if None in ctx.values():
            print(f"⚠️  段落 {pid} 缺字段，已跳过")
            continue
        generator = get_generator("GenericParagraphGenerator")(
            prompt_path = task["prompt"],
            context     = ctx,
            provider = "qwen"
        )
        para_out[pid].append(generator.generate())

    # 3) 写入 Word
    write_docx(para_out, out_doc)
    print(f"✓ 已生成报告：{out_doc}")

# ───── CLI ──────────────────────────────────────────────
if __name__ == "__main__":
    if "DASHSCOPE_API_KEY" not in os.environ:
        sys.exit("✗ 请先 set DASHSCOPE_API_KEY=sk-...")
    import argparse
    ap = argparse.ArgumentParser(description="药学报告流水线")
    ap.add_argument("excel", help="原始 Excel 路径")
    ap.add_argument("-o", "--out", default="report_out.docx", help="输出 Word 文件名")
    args = ap.parse_args()

    run_pipeline(args.excel, args.out)
