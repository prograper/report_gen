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
# CFG_DIR    = ROOT / "configs"
CFG_DIR   = ROOT
PROMPT_DIR = ROOT / "prompts"
TPL_DIR    = ROOT / "templates"

# ───── 实用函数 ───────────────────────────────────────────
def load_yaml(cfg_dir: str, fname: str):
    with open(CFG_DIR / cfg_dir / fname, encoding="utf-8") as f:
        return yaml.safe_load(f)

def read_prompt(path: str):
    return (ROOT / path).read_text(encoding="utf-8")

def write_docx(placeholder_map: dict, paragraph_map: dict, out_path: str):
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

# -------- 路径解析工具 --------
def resolve(path: str, data: dict):
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur

# ───── Pipeline ─────────────────────────────────────────
def run_pipeline(excel_path: str, out_doc: str, config_dir: str):
    # ───── 读取配置 ───────────────────────────────────────────
    # 每张 Sheet 抽取任务
    sheet_cfg   = load_yaml(config_dir, "sheet_tasks.yaml")
    # 段落生成任务
    paragraphs  = load_yaml(config_dir, "paragraph_tasks.yaml")
    placeholder_map = load_yaml(config_dir, "doc_placeholders.yaml")
    
    xls = pd.ExcelFile(excel_path)
    # 全局嵌套 {Sheet: {field: val}}
    extracted = {}
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
        # extracted.update(extractor.extract())
        # extractor.extract() 仍返回 {"Tmax":..., "Cmax":...}
        extracted[sheet] = extractor.extract()

    # 2) 生成段落
    para_out = {pid: [] for pid in paragraphs}
    for pid, task in paragraphs.items():
        # 取所需字段
        # ctx = {k: extracted.get(k) for k in task["keys"]}
        # if None in ctx.values():
        #     print(f"⚠️  段落 {pid} 缺字段，已跳过")
        #     continue
        # generator = get_generator("GenericParagraphGenerator")(
        #     prompt_path = task["prompt"],
        #     context     = ctx,

        # 只做存在性检查；模板里直接通过 data.<Sheet>.<Field> 访问
        missing = [k for k in task["keys"] if resolve(k, extracted) is None]
        if missing:
            print(f"⚠️  段落 {pid} 缺字段 {missing}，已跳过")
            continue
        generator = get_generator("GenericParagraphGenerator")(
            prompt_path = task["prompt"],
            # ← 只放一层 data，如果在prompt里不想加data前缀，可以context = extracted
            context     = {"data": extracted},
            provider = "qwen"
        )
        para_out[pid].append(generator.generate())

    # 3) 写入 Word
    write_docx(placeholder_map, para_out, out_doc)
    print(f"✓ 已生成报告：{out_doc}")

# ───── CLI ──────────────────────────────────────────────
if __name__ == "__main__":
    if "DASHSCOPE_API_KEY" not in os.environ:
        sys.exit("✗ 请先 set DASHSCOPE_API_KEY=sk-...")
    import argparse
    ap = argparse.ArgumentParser(description="自动生成报告流水线")
    ap.add_argument("excel", help="原始 Excel 路径")
    ap.add_argument("-o", "--out", default="report_out.docx", help="输出 Word 文件名")
    ap.add_argument("-c", "--config", default="configs", help="配置文件目录")
    args = ap.parse_args()

    run_pipeline(args.excel, args.out, args.config)
