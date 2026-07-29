# TJU AI Platform Proxy

让 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 通过本地代理访问 **天津大学 AI 平台**的 OpenAI 兼容接口。  
将 Anthropic `/v1/messages` 协议转换为 OpenAI `/chat/completions` 协议，实现完整的 Tool Use、流式 SSE、消息体转换。

## 效果

```
Claude Code  ←→  本地代理 (:8000)  ←→  天津大学 AI 平台
(Anthropic 协议)    (协议转换)        (OpenAI 协议)
```

## 功能特性

- ✅ Anthropic → OpenAI 协议双向转换
- ✅ SSE 流式响应（上游非流式请求，代理拼接为 SSE 事件流）
- ✅ Tool Use / Function Calling 完整支持
- ✅ `system` 字段自动合并为 messages 首条
- ✅ `tool_use` / `tool_result` content block 双向转换
- ✅ 连接池复用 + HTTP/2 多路复用 + Keep-Alive
- ✅ orjson 加速 JSON 序列化（4-10x 加速）
- ✅ 上游错误透传（超时 504、连接失败 502）
- ✅ Token 用量透传（input_tokens + output_tokens）

## 环境要求

- Python 3.12+
- 天大 AI 平台有效 Token API_KEY
- 校园网或VPN连接
## 快速开始

### 1. 下载

```bash
git clone https://github.com/Persenb/TJU-AI-Platform-Proxy.git
cd TJU-AI-Platform-Proxy
```

### 2. 安装依赖

```bash
pip install fastapi uvicorn httpx orjson
```

### 3. 配置 Token

**方式一（推荐）：通过 `.env` 文件**

复制环境变量模板并填入 Token：

```bash
cp .env.example .env
```

然后编辑 `.env`，将 `你的KEY` 替换为实际密钥：

```
TJU_API_KEY=你的真实KEY
```

**方式二：直接在源文件中硬编码**

打开 `tju_proxy.py`，找到第 157 行附近代码：

```python
TJU_API_KEY = os.environ.get("TJU_API_KEY", "")
```

改为：
```python
TJU_API_KEY = "你的真实KEY"
```

### 4. 启动代理

```bash
python tju_proxy.py
```

启动后输出：
```
 TJU AI Platform Proxy v2.0 (Full Edition)
 Listen:   http://127.0.0.1:8000
 Upstream: https://ai.tju.edu.cn/api/v3/chat/completions
 Stream:   supported
 Tool Use: supported
 ...
```
代理运行窗口处 Ctrl+C 关闭

### 5. 配置 Claude Code

编辑 `C:\Users\你的用户名\.claude\settings.json` (若没有则创建settings.json)，填写以下内容：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000",
    "ANTHROPIC_AUTH_TOKEN": "sk-placeholder",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT": "1",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_EFFORT_LEVEL": "max"
  },
  "disableWorkflows": true,
  "disableBundledSkills": true,
  "model": "tju-llm",
  "permissions": {
    "deny": ["NotebookEdit", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup"]
  }
}
```

> `ANTHROPIC_AUTH_TOKEN` 代理不验证，填空值或任意填写即可。

#### 配置项说明

| 配置 | 作用 |
|---|---|
| `ANTHROPIC_BASE_URL` | 指向本地代理 `http://127.0.0.1:8000` |
| `ANTHROPIC_AUTH_TOKEN` | 占位符，代理不校验 |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | **关闭非必要上下文**（git diff、文件树、环境信息等），大幅减少 `input_tokens` |
| `CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT=1` | 使用精简版系统提示词，进一步减少 `input_tokens` |
| `CLAUDE_CODE_ATTRIBUTION_HEADER=0` | 关闭请求头中的归属信息，减少请求体积 |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | 禁止自动记忆功能，避免记忆相关 token 消耗 |
| `CLAUDE_CODE_EFFORT_LEVEL=max` | 让模型尽力回答，提升回复质量 |
| `disableWorkflows: true` | 不加载 workflow 技能，减少系统提示词 |
| `disableBundledSkills: true` | 不加载内置技能提示词 |
| `model: "tju-llm"` | 固定模型名为 `tju-llm`，确保与上游一致 |
| `permissions.deny` | 禁用不需要的功能，避免误触 |

### 6. 启动 Claude Code
保持代理窗口运行，在想要使用Claude Code的项目文件夹中打开终端，输入命令：
```bash
claude
```

## 环境配置选项

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `TJU_API_KEY` | 天大平台 APIKEY | `""`（必填） |
| `TJU_MODEL` | 覆盖上游模型名 | 透传客户端模型名 |
| `PROXY_PORT` | 本地监听端口 | `8000` |
| `MAX_OUTPUT_TOKENS` | 上游最大输出 token 上限，防止超时 | `32000` |
| `SYSTEM_PROMPT_OPTIMIZE` | 设置 `1` 启用：自动剥离系统提示词中 Claude 特有身份标识和 thinking 指令，减少 token 消耗，适配非 Claude 模型 | `""`（关闭） |

也可通过命令行参数指定端口：

```bash
python tju_proxy.py --port 8080
```



## 协议转换说明

### 请求方向（Anthropic → OpenAI）

| Anthropic 字段 | OpenAI 字段 | 转换方式 |
|---|---|---|
| `system` | `messages[0] {role:"system"}` | 自动插入 |
| `messages[].content[]` | text / tool_calls / role:tool | 展平转换 |
| `tools[].input_schema` | `tools[].function.parameters` | 格式映射 |
| `tool_choice.any` | `tool_choice: "required"` | 语义等价 |
| `tool_choice.tool` | `{type:"function", function:{name:...}}` | 语义等价 |
| `stop_sequences` | `stop` | 直接透传 |

### 响应方向（OpenAI → Anthropic）

| OpenAI 字段 | Anthropic 字段 | 转换方式 |
|---|---|---|
| `choices[0].message.content` | `content[{type:"text"}]` | 文本块 |
| `choices[0].message.tool_calls` | `content[{type:"tool_use"}]` | 工具调用块 |
| `finish_reason: "stop"` | `stop_reason: "end_turn"` | 语义映射 |
| `finish_reason: "tool_calls"` | `stop_reason: "tool_use"` | 语义映射 |
| `usage.prompt_tokens` | `usage.input_tokens` | 重命名 |
| `usage.completion_tokens` | `usage.output_tokens` | 重命名 |


## ⭐已知限制⭐

- **模型响应速度**：代理本身开销已尽量优化，总响应时间取决于平台模型推理速度，由于每次发送指令会传入大量系统提示词，且无缓存机制，导致响应很慢，并且会随上下文增长变慢，为此在setting中对部分功能进行了限制，以尽量减少input_tokens，可自主修改
- **无 Thinking 块显示**：上游模型不支持 Anthropic `thinking` 内容块，Claude Code 不会显示详细思考过程
- **无提示缓存**：天大平台无 Anthropic 缓存机制，每次请求全额计算 `input_tokens`，即不区分缓存命中和缓存未命中
- **无联网搜索**：上游模型及平台未开放 web search / web crawl 接口，无法联网获取实时信息
- **上游非流式**：代理对天大平台发送非流式请求，等完整响应后拼接 SSE 发给 Claude Code
- **文件读取缓慢**：模型本身能力有限加上少量的中转延迟，难以处理大规模、多文件项目
- **未知缺陷和运行bug**：期待反馈和修改
## 文件结构

```
tju_proxy/
├── tju_proxy.py      ← 主程序（协议转换代理）
├── README.md         ← 本文档
├── .env.example      ← 环境变量模板
├── .env              ← 环境变量（含 Token，已 gitignore）
└── .gitignore        ← Git 忽略规则
```

## 技术栈

- [FastAPI](https://fastapi.tiangolo.com/) — ASGI 框架
- [httpx](https://www.python-httpx.org/) — HTTP 客户端（支持 HTTP/2 + 连接池）
- [orjson](https://github.com/ijl/orjson) — 高速 JSON 序列化
- [uvicorn](https://www.uvicorn.org/) — ASGI 服务器

## License

MIT

- **如果这个项目对你有帮助，欢迎star**
