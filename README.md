# Pharma Report Pipeline

自动把 **多 Sheet Excel 原始实验数据 → 结构化关键字段 → LLM 生成段落 → Word 报告**  
- **每张 Sheet 独立 Prompt**：不同实验/统计表都能随时增删  
- **抽取 / 生成 只各用一个 Generic Agent**：核心 Python 永远不动  
- **LLM 热切换**：配置里写好 `model_name` / `base_url` / Key，代码 0 改  
- **结果落 Word**：使用占位符 `{{INTRO}}` `{{METHOD}}` … 自动替换

---

## 📂 目录结构

```text
pharma_report/
├─ main.py
├─ requirements.txt
├─ agents/
│  ├─ registry.py
│  ├─ extract_generic.py
│  └─ generate/
│      └─ base.py
├─ llm_client.py
├─ configs/
│  ├─ sheet_tasks.yaml
│  ├─ paragraph_tasks.yaml
│  ├─ doc_placeholders.yaml
│  └─ llm.yaml
├─ prompts/              # Jinja2 Prompt 模板
└─ templates/
   └─ report_template.docx
```

---

## ⚡ 快速上手

### 1. 安装

```bash
python -m venv venv && source venv/bin/activate  # 可选
pip install -r requirements.txt
```

### 2. 配置 LLM

三选一（推荐①或②）：

| 方式 | 步骤 |
|------|------|
| **① 写入 `configs/llm.yaml`** | 在对应 `provider` 段写 `key_value: YOUR_KEY` |
| **② `.env` + python-dotenv** | `.env` 内容：`OPENAI_API_KEY=sk-xxx` |
| **③ 系统环境变量** | Windows `setx`, Linux/macOS `export` |

### 3. 运行

```bash
python main.py ./input/test_input.xlsx -o ./output/report.docx
```

---

## 🗂️ 配置文件

### `sheet_tasks.yaml`

```yaml
RawData_A:
  prompt: prompts/extract_pk_params.txt
  keys: { Cmax: number, Tmax: number }
  provider: openai
```

### `paragraph_tasks.yaml`

```yaml
IntroParagraph:
  keys: [Cmax, Tmax]
  prompt: prompts/gen_intro.txt
  provider: qwen
```

### `llm.yaml`

```yaml
openai:
  model_name: gpt-4o-mini
  base_url:   https://api.openai.com/v1
  key_env:    OPENAI_API_KEY

qwen:
  model_name: qwen2.5-32b-instruct
  base_url:   https://dashscope.aliyuncs.com/compatible-mode/v1
  key_value:  YOUR_QWEN_KEY
```

---

## 🛠️ 常见任务

| 任务 | 做法 |
|------|------|
| **新增 Sheet** | 在 `sheet_tasks.yaml` 添配置 + 新 Prompt |
| **新增段落** | 在 `paragraph_tasks.yaml` 添配置 + Word 占位符 |
| **切换模型** | 全局 `LLM_PROVIDER=qwen` 或 YAML `provider:` |
| **加字段 / 改类型** | 只改 `sheet_tasks.yaml -> keys` |
| **Prompt 调优** | 直接编辑 `prompts/*.txt` |

---

## 🔒 部署要点

- **密钥不入 Git**：用 `.env` 或私有 `llm.yaml`
- **Docker**：`ENV LLM_PROVIDER=openai OPENAI_API_KEY=xxx`
- **多模型并发**：不同进程分别 `apply_provider()`，互不覆盖

---

## 📝 License

Distributed under the **Apache License 2.0**.  
See [LICENSE](LICENSE) for full license text.