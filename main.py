"""
入口脚本：解析 Excel → 结构化抽取 → 生成段落/直填变量 → 写入 Word
健壮性增强：
  - 软失败边界（配置/抽取/段落/渲染）
  - 配置预检（不崩溃，只跳过问题项）
  - 错误收集器，输出 logs/run_summary.json
  - resolve(default/strict)，fill 模式更宽松
  - 类型清洗（number / array[string] / string + 百分号转小数）
  - fill 模式缺值在嵌套 dict 中补默认，避免模板渲染出错
"""

from __future__ import annotations

import os, sys, yaml, pandas as pd, json, traceback
from pathlib import Path
from typing import Any, Dict
from docxtpl import DocxTemplate
from agents.registry import get_extractor
from agents.generate import get_generator
import logging
from logging_setup import setup_logging   # 你已有的日志初始化

# ───── 目录常量 ───────────────────────────────────────────
ROOT       = Path(__file__).parent
CFG_DIR    = ROOT

# 日志
setup_logging(ROOT)
USER_LOG   = logging.getLogger("user")
SYS_LOG    = logging.getLogger("system")
CONFIG_LOG = logging.getLogger("config")

# ───── 实用函数 ───────────────────────────────────────────
def load_yaml(cfg_dir: Path, fname: str) -> dict:
    with open(cfg_dir / "business_configs" / fname, encoding="utf-8") as f:
        return yaml.safe_load(f)

def write_docx(config_dir: Path, report_name: str, render_ctx: dict):
    tpl = DocxTemplate(config_dir / "template" / "report_template.docx")
    tpl.render(render_ctx)
    out_dir = config_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (report_name + ".docx")
    tpl.save(out_path)
    SYS_LOG.info(f"Word 渲染完成：{out_path}")

# -------- 路径解析（升级） --------
def resolve(path: str, data: dict, default: Any | None = None, strict: bool = True):
    """
    解析 'Sheet.Field.Sub' 到嵌套 dict。
    strict=True: 任一层不存在 → 返回 None
    strict=False: 任一层不存在 → 返回 default，并记录一次 warning（由调用方决定）
    """
    cur: Any = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None if strict else default
    return cur

# -------- 在嵌套 dict 中确保路径存在并赋值（用于 fill 缺值兜底） --------
def ensure_path_set(data: dict, path: str, value: Any):
    cur = data
    parts = path.split(".")
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value

# -------- Excel 查找 --------
def first_excel_as_excelfile(dir_path: str | Path, pattern: str = "*.xls*") -> pd.ExcelFile:
    dir_path = Path(dir_path)
    matches = sorted(dir_path.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"目录 {dir_path} 下没有找到任何 {pattern} 文件！")
    if len(matches) > 1:
        SYS_LOG.warning(f"发现 {len(matches)} 个 Excel，仅使用第一个：{matches[0].name}")
    return pd.ExcelFile(matches[0])

# -------- 错误收集器 --------
class ErrorCollector:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, level: str, where: str, msg: str, detail: str | None = None):
        rec = {"level": level, "where": where, "msg": msg}
        if detail:
            rec["detail"] = detail
        self.items.append(rec)
        log = USER_LOG if level.lower() in ("warn", "warning") else SYS_LOG
        if level.lower() in ("warn", "warning"):
            log.warning(f"[{where}] {msg}")
        else:
            log.error(f"[{where}] {msg}")

    def summary(self) -> dict:
        counts = {"errors": 0, "warnings": 0}
        for it in self.items:
            if it["level"].lower().startswith("warn"):
                counts["warnings"] += 1
            else:
                counts["errors"] += 1
        return {"counts": counts, "items": self.items}

    def dump(self, root: Path):
        logs_dir = root / "logs"
        logs_dir.mkdir(exist_ok=True)
        (logs_dir / "run_summary.json").write_text(
            json.dumps(self.summary(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

# -------- 类型清洗 --------
def coerce_types(sheet: str, values: dict, type_spec: dict, percent_as_fraction: bool = True) -> dict:
    """
    根据 sheet_tasks.yaml -> keys 的类型说明，把抽取结果转成期望类型。
    - number: 去空格/逗号，支持 '85%'；若 percent_as_fraction=True 则转 0.85，否则 85.0
    - array[string]: 不是 list 就包一层 + str()
    - string: str()
    """
    out: dict = {}
    for k, typ in type_spec.items():
        v = values.get(k, None)
        if v is None:
            out[k] = None
            continue

        try:
            if typ == "number":
                s = str(v).strip()
                is_percent = s.endswith("%")
                s = s.replace(",", "").replace("%", "").strip()
                num = float(s)
                if is_percent:
                    num = num / 100.0 if percent_as_fraction else num
                    CONFIG_LOG.debug(f"[COERCE] {sheet}.{k}: percent detected -> {num}")
                out[k] = num
            elif typ == "array[string]":
                if isinstance(v, list):
                    out[k] = [str(x) for x in v]
                else:
                    out[k] = [str(v)]
            else:  # string / default
                out[k] = str(v)
        except Exception as e:
            # 转换失败：置 None 并记录
            out[k] = None
            SYS_LOG.warning(f"[COERCE-FAIL] {sheet}.{k} 类型 {typ} 转换失败（值={v}）：{e}")

    return out

# -------- 配置预检 --------
def validate_configs(config_dir: Path, sheet_cfg: dict, paragraphs: dict, xls: pd.ExcelFile, ec: ErrorCollector):
    """
    发现问题只记录 warning，不抛异常。返回 planned_skips dict.
    planned_skips = {
        "sheets": set([...]),
        "paragraphs": set([...])
    }
    """
    planned = {"sheets": set(), "paragraphs": set()}

    # 1) 模板 docx 存在性
    tpl_path = config_dir / "template" / "report_template.docx"
    if not tpl_path.exists():
        ec.add("error", "CONFIG", f"模板不存在：{tpl_path}")
        # 致命，但仍让后续执行到渲染阶段再 fail，便于收集更多信息

    # 2) sheet_tasks.yaml 基本检查
    for sname, cfg in (sheet_cfg or {}).items():
        if not isinstance(cfg, dict):
            ec.add("warn", "CONFIG", f"sheet {sname} 配置不是对象，将跳过")
            planned["sheets"].add(sname)
            continue
        # prompt
        p_rel = cfg.get("prompt")
        if not p_rel:
            ec.add("warn", "CONFIG", f"sheet {sname} 缺少 prompt，将跳过")
            planned["sheets"].add(sname);  continue
        p_abs = config_dir / "prompts" / p_rel
        if not p_abs.exists():
            ec.add("warn", "CONFIG", f"sheet {sname} 的 prompt 文件不存在：{p_abs}，将跳过")
            planned["sheets"].add(sname)
        # keys
        keys = cfg.get("keys", {})
        if not isinstance(keys, dict) or not keys:
            ec.add("warn", "CONFIG", f"sheet {sname} 的 keys 非法或为空，将跳过")
            planned["sheets"].add(sname)
        # 类型合法性
        for k, t in keys.items():
            if t not in ("string", "number", "array[string]"):
                ec.add("warn", "CONFIG", f"sheet {sname}.{k} 非支持类型 {t}，按 string 处理")

    # 3) Excel sheet 对齐（只提示）
    excel_sheets = set(xls.sheet_names)
    for sname in sheet_cfg.keys():
        if sname not in excel_sheets:
            ec.add("warn", "CONFIG", f"Excel 中不存在 Sheet：{sname}（将跳过该 sheet）")
            planned["sheets"].add(sname)

    # 4) paragraph_tasks.yaml 检查
    for pid, task in (paragraphs or {}).items():
        if not isinstance(task, dict):
            ec.add("warn", "CONFIG", f"段落 {pid} 配置不是对象，将跳过")
            planned["paragraphs"].add(pid);  continue

        mode = task.get("mode") or ("generate" if "prompt" in task else "fill")
        if mode not in ("generate", "fill"):
            ec.add("warn", "CONFIG", f"段落 {pid} 的 mode 非 generate/fill，将跳过")
            planned["paragraphs"].add(pid);  continue

        keys = task.get("keys", [])
        if keys and not isinstance(keys, list):
            ec.add("warn", "CONFIG", f"段落 {pid} 的 keys 不是列表，将忽略 keys")
            keys = []

        if mode == "generate":
            p_rel = task.get("prompt")
            if not p_rel:
                ec.add("warn", "CONFIG", f"段落 {pid} 缺少 prompt，将跳过")
                planned["paragraphs"].add(pid);  continue
            p_abs = config_dir / "prompts" / p_rel
            if not p_abs.exists():
                ec.add("warn", "CONFIG", f"段落 {pid} 的 prompt 文件不存在：{p_abs}，将跳过")
                planned["paragraphs"].add(pid)

        # keys 的路径格式（粗检）
        for k in keys or []:
            if "." not in k:
                ec.add("warn", "CONFIG", f"段落 {pid} 的 key 缺少路径分隔：{k}（应为 Sheet.Field）")

    return planned

# ───── Pipeline ─────────────────────────────────────────
def run_pipeline(config_dir: str, report_name: str):
    config_dir = Path(config_dir)
    SYS_LOG.info(f"开始运行流水线，配置目录={config_dir}")

    ec = ErrorCollector()

    # 1) 读取业务配置（软失败：出错尽量继续）
    try:
        sheet_cfg   = load_yaml(config_dir, "sheet_tasks.yaml")
        paragraphs  = load_yaml(config_dir, "paragraph_tasks.yaml")
        SYS_LOG.info(f"载入配置：sheet={len(sheet_cfg)}，paragraphs={len(paragraphs)}")
        CONFIG_LOG.debug(f"[CONFIG] sheet_tasks.yaml: {sheet_cfg}")
        CONFIG_LOG.debug(f"[CONFIG] paragraph_tasks.yaml: {paragraphs}")
    except Exception as e:
        ec.add("error", "CONFIG", f"读取 YAML 失败：{e}", traceback.format_exc())
        # YAML 都读不到就没法继续
        ec.dump(ROOT)
        sys.exit("✗ 无法读取配置，请检查 business_configs/*.yaml")

    # 2) 读取 Excel（硬失败：确实没有就退出）
    try:
        xls = first_excel_as_excelfile(config_dir / "input", "*.xls*")
    except Exception as e:
        ec.add("error", "INPUT", str(e), traceback.format_exc())
        ec.dump(ROOT)
        sys.exit(f"✗ {e}")

    # 3) 预检（只产生日志与 planned_skips）
    planned = validate_configs(config_dir, sheet_cfg, paragraphs, xls, ec)

    # 4) 抽取变量（嵌套命名空间：{Sheet: {field: val}}）
    extracted: dict[str, dict] = {}
    for sheet in xls.sheet_names:
        if sheet not in sheet_cfg:
            SYS_LOG.info(f"跳过未配置的 Sheet：{sheet}")
            continue
        if sheet in planned["sheets"]:
            SYS_LOG.warning(f"跳过存在问题的 Sheet：{sheet}")
            continue

        cfg = sheet_cfg[sheet]
        try:
            df  = xls.parse(sheet)
            SYS_LOG.info(f"开始抽取 Sheet：{sheet}")

            extractor = get_extractor("GenericExtractor")(
                df          = df,
                keys        = cfg["keys"],
                prompt_path = config_dir / "prompts" / cfg["prompt"],
                config_dir  = config_dir,
                provider    = cfg.get("provider", "qwen"),
                sheet_name  = sheet,
            )
            raw_values = extractor.extract() or {}

            # 类型清洗（避免模板里 float/round 爆掉）
            cleaned = coerce_types(sheet, raw_values, cfg.get("keys", {}), percent_as_fraction=True)
            extracted[sheet] = cleaned

            # 用户摘要
            kv_line = ", ".join(f"{k}={cleaned[k]}" for k in list(cleaned.keys())[:10])
            USER_LOG.info(f"[抽取完成] {sheet}：{kv_line}{' ...' if len(cleaned)>10 else ''}")

        except Exception as e:
            ec.add("error", f"EXTRACT:{sheet}", f"抽取失败：{e}", traceback.format_exc())
            continue

    # 5) 处理段落：支持 generate / fill 双模式
    gen_ctx: dict[str, str] = {}

    for pid, task in (paragraphs or {}).items():
        if pid in planned["paragraphs"]:
            SYS_LOG.warning(f"跳过存在问题的段落/占位符：{pid}")
            continue

        mode = task.get("mode") or ("generate" if "prompt" in task else "fill")
        keys = task.get("keys", [])

        try:
            # 缺 key 检查：generate 严格，fill 宽松（继续并补默认）
            missing = [k for k in (keys or []) if resolve(k, extracted, strict=True) is None]
            if missing and mode == "generate":
                ec.add("warn", f"PARA:{pid}", f"缺字段 {missing}，已跳过生成")
                continue

            if mode == "generate":
                provider    = task.get("provider", "qwen")
                prompt_path = config_dir / "prompts" / task["prompt"]

                # 记录用于生成的关键上下文
                ctx_vals = {k: resolve(k, extracted, strict=True) for k in keys or []}
                CONFIG_LOG.debug(f"[GEN-VALUES] {pid}\n{json.dumps(ctx_vals, ensure_ascii=False, indent=2)}")

                generator = get_generator("GenericParagraphGenerator")(
                    prompt_path = prompt_path,
                    context     = extracted,   # 模板里可 {{ Sheet.Field }}
                    config_dir  = config_dir,
                    provider    = provider,
                    paragraph_id= pid,
                )
                text = generator.generate()
                gen_ctx[pid] = text
                USER_LOG.info(f"[生成完成] {pid}：{(text[:200] + '...') if len(text)>200 else text}")

            else:  # fill
                # 宽松：即便缺 key 也不终止；为缺失路径补默认值，避免模板渲染报错
                for miss in missing:
                    ensure_path_set(extracted, miss, "-")
                    ec.add("warn", f"FILL:{pid}", f"缺字段 {miss}，已用默认 '-' 补位")

                # 记录 fill 的实际值
                if keys:
                    val_map = {k: resolve(k, extracted, strict=False, default="-") for k in keys}
                    CONFIG_LOG.debug(f"[FILL-VALUES] pid={pid}\n{json.dumps(val_map, ensure_ascii=False, indent=2)}")
                    summary = ", ".join(f"{k}={val_map[k]}" for k in val_map)
                    USER_LOG.info(f"[直填值] {pid} → {summary[:500] + ' ...' if len(summary)>500 else summary}")
                else:
                    SYS_LOG.info(f"[直填变量] {pid}（未声明 keys，跳过值记录）")

        except Exception as e:
            ec.add("error", f"PARA:{pid}", f"处理失败（mode={mode}）：{e}", traceback.format_exc())
            continue

    # 6) 渲染 Word（硬失败：模板缺失会在这里炸）
    try:
        # 注意：若占位符名与 Sheet 名冲突，生成型段落优先
        render_ctx = {**extracted, **gen_ctx}
        CONFIG_LOG.debug(f"[RENDER-CTX] keys={list(render_ctx.keys())}")
        write_docx(config_dir, report_name, render_ctx)
        SYS_LOG.info("流水线结束")
    except Exception as e:
        ec.add("error", "RENDER", f"渲染失败：{e}", traceback.format_exc())

    # 7) 写运行摘要
    ec.dump(ROOT)
    sums = ec.summary()["counts"]
    SYS_LOG.info(f"Run Summary: errors={sums['errors']}, warnings={sums['warnings']}")
    USER_LOG.info("运行完成，详情见 logs/user.log / system.log / config.log / run_summary.json")

# ───── CLI ──────────────────────────────────────────────
if __name__ == "__main__":
    if "DASHSCOPE_API_KEY" not in os.environ:
        logging.getLogger("system").error("缺少 DASHSCOPE_API_KEY 环境变量")
        sys.exit("✗ 请先 set DASHSCOPE_API_KEY=sk-...")

    import argparse
    ap = argparse.ArgumentParser(description="自动生成报告流水线")
    ap.add_argument("-c", "--config", default="configs", help="配置文件目录")
    ap.add_argument("-n", "--name", default="生成报告文件", help="报告名称")
    args = ap.parse_args()

    run_pipeline(args.config, args.name)
