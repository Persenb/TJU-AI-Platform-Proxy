"""
╔══════════════════════════════════════════════════════════════════╗
║  天大 AI 平台 HTTP 中转服务（全功能版）                          ║
║  Anthropic /v1/messages  ↔  OpenAI /chat/completions 协议转换   ║
║  支持: 流式 + 非流式 + Tool Use + 多模态 Content Block          ║
╚══════════════════════════════════════════════════════════════════╝

使 Claude Code 可以通过本地代理访问天津大学 AI 平台的 OpenAI 兼容接口。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📦 依赖安装
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    pip install fastapi uvicorn httpx

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔑 设置 Token（首次必做）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Windows (cmd)
    set TJU_API_KEY=你的天大平台Token

    # Windows (PowerShell)
    $env:TJU_API_KEY="你的天大平台Token"

    # Linux / macOS / Git Bash
    export TJU_API_KEY=你的天大平台Token

    可选：用 TJU_MODEL 指定上游模型名（否则透传客户端模型名）
    export TJU_MODEL=gpt-4o

    可选：限制上游输出上限（防止超时，默认 32000）
    export MAX_OUTPUT_TOKENS=32000

    可选：启用系统提示词优化，剥离 Claude 特有内容
    export SYSTEM_PROMPT_OPTIMIZE=1

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀 启动服务
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    python tju_proxy.py

    # 或指定端口（默认 8000）
    python tju_proxy.py --port 8000

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔧 Claude Code 配置指令
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    方式一（推荐）：设置环境变量后启动 Claude Code
        set ANTHROPIC_BASE_URL=http://127.0.0.1:8000
        set ANTHROPIC_AUTH_TOKEN=sk-placeholder
        claude

    方式二：在项目 .claude/settings.json 中添加：
        {
            "model": "claude-sonnet-4-20250514",
            "baseUrl": "http://127.0.0.1:8000",
            "apiKey": "sk-placeholder"
        }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧪 curl 测试命令
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    非流式测试：
        curl -s http://127.0.0.1:8000/v1/messages              \
            -H "Content-Type: application/json"                  \
            -d '{
                "model": "tju-llm",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "你好"}]
            }'

    流式测试：
        curl -s http://127.0.0.1:8000/v1/messages              \
            -H "Content-Type: application/json"                  \
            -d '{
                "model": "tju-llm",
                "max_tokens": 256,
                "stream": true,
                "messages": [{"role": "user", "content": "你好"}]
            }'

    工具调用测试：
        curl -s http://127.0.0.1:8000/v1/messages              \
            -H "Content-Type: application/json"                  \
            -d '{
                "model": "tju-llm",
                "max_tokens": 512,
                "stream": true,
                "messages": [{"role": "user", "content": "计算 1+1"}],
                "tools": [{"name":"calc","description":"计算器",
                    "input_schema":{"type":"object","properties":{
                        "expr":{"type":"string"}},"required":["expr"]}}]
            }'

    健康检查：
        curl http://127.0.0.1:8000/health
"""

import os
import sys
import uuid
import logging
from typing import Optional

import httpx
import orjson
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager


def _j(data: dict) -> str:
    """orjson 快速序列化（比 json.dumps 快 4-10 倍）"""
    return orjson.dumps(data, option=orjson.OPT_APPEND_NEWLINE).decode().rstrip("\n")


# ============================================================
# 全局 HTTP 客户端（连接池复用，避免每次请求重建 TCP/TLS）
# ============================================================
_http_client = None  # type: ignore


def get_http_client():  # type: ignore
    """返回全局 HTTP 客户端（lifespan 中初始化，此处只读）"""
    return _http_client

# ============================================================
# .env 文件加载（不依赖 python-dotenv）
# ============================================================

def _load_env_file(path: str = ".env") -> None:
    """简易 .env 解析器：每行 KEY=VALUE，跳过 # 注释和空行"""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip("\"'").strip()
                if key not in os.environ:  # 环境变量优先
                    os.environ[key] = val
    except FileNotFoundError:
        pass  # 没有 .env 不影响运行

# 使用脚本所在目录的绝对路径，确保从任何位置运行都能找到 .env
_base_dir = os.path.dirname(os.path.abspath(__file__))
_load_env_file(os.path.join(_base_dir, ".env"))


# ============================================================
# 配置区
# ============================================================

TJU_API_KEY = os.environ.get("TJU_API_KEY", "")
TJU_API_URL = "https://ai.tju.edu.cn/api/v3/chat/completions"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8000"))
TJU_MODEL_OVERRIDE = os.environ.get("TJU_MODEL", "")

# 系统提示词优化：设为 "1" 时自动剥离 Claude 特有身份标识和 thinking 指令
SYSTEM_PROMPT_OPTIMIZE = os.environ.get("SYSTEM_PROMPT_OPTIMIZE", "") == "1"

# 上游最大输出 token 上限（上游模型通常有输出长度限制）
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tju-proxy")

# ============================================================
# 系统提示词优化（可选）
# ============================================================

def _optimize_system_prompt(text: str) -> str:
    """
    当 SYSTEM_PROMPT_OPTIMIZE=1 时启用。
    剥离 Claude 特有内容，让上游模型（如 Qwen）更准确理解任务，同时减少 token 消耗。

    当前处理：
    - 移除 "You are Claude" / "created by Anthropic" 等身份标识
    - 移除 thinking 块相关指令（上游模型不支持 Anthropic thinking）
    """
    if not text or not SYSTEM_PROMPT_OPTIMIZE:
        return text

    import re

    # 1. 移除 Claude 身份标识段落（通常在第一句）
    #    "You are Claude, an AI assistant created by Anthropic..."
    text = re.sub(
        r'(?i)you\s+are\s+claude\s*,?\s*(?:an\s+)?(?:ai\s+)?(?:assistant|model|agent)'
        r'\s*(?:,?\s*(?:created|built|made|developed)\s+by\s+anthropic[^.\n]*)?[.\n]',
        '', text, count=1
    )

    # 2. 移除 thinking 块相关指令（上游模型不支持，留着浪费 token 还可能产生幻觉）
    text = re.sub(
        r'(?im)^(?:you\s+should\s+)?think\s+(?:step\s+by\s+step|carefully|about|through)'
        r'[^.\n]*[.\n]?\s*',
        '', text
    )
    text = re.sub(
        r'(?is)use\s*<thinking>.*?</thinking>\s*tags?\s*',
        '', text
    )
    text = re.sub(
        r'(?i)before\s+(answering|responding|replying|acting)\s*,\s*think[^.\n]*[.\n]',
        '', text
    )

    # 3. 清理多余空行
    result = '\n'.join(
        line for line in text.split('\n')
        if line.strip()
    ).strip()

    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0),
        follow_redirects=False,
        http2=True,
        limits=httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=30.0,
        ),
    )
    log.info("HTTP 连接池已初始化（HTTP/2 + Keep-Alive 30s）")
    yield
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
        log.info("HTTP 连接池已关闭")


app = FastAPI(title="TJU AI Platform Proxy", version="2.0.0", lifespan=lifespan)


def _generate_id(prefix: str = "msg") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _map_finish_reason(fr: Optional[str]) -> Optional[str]:
    """OpenAI finish_reason -> Anthropic stop_reason"""
    return {"stop": "end_turn", "length": "max_tokens",
            "content_filter": "content_filter", "tool_calls": "tool_use"}.get(fr) if fr else None


# ============================================================
# 消息内容块转换（Anthropic <-> OpenAI）
# ============================================================

def _extract_text_from_blocks(blocks: list) -> str:
    """从 Anthropic content block 列表中提取纯文本"""
    texts = []
    for b in blocks:
        if isinstance(b, dict):
            if b.get("type") == "text":
                texts.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                # tool_result 的 content 可能是字符串或 block 列表
                tc = b.get("content", "")
                if isinstance(tc, list):
                    texts.append(_extract_text_from_blocks(tc))
                elif tc:
                    texts.append(str(tc))
    return "\n".join(texts).strip() if texts else ""


def _has_tool_result(blocks: list) -> bool:
    """检查 content block 列表是否包含 tool_result"""
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)


def _has_tool_use(blocks: list) -> bool:
    """检查 content block 列表是否包含 tool_use"""
    return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)


def _convert_anthropic_tools_to_openai(tools: list) -> list:
    """
    Anthropic tools 格式 -> OpenAI tools 格式

    Anthropic: [{"name": "calc", "description": "...", "input_schema": {...}}]
    OpenAI:    [{"type": "function", "function": {"name": "calc", "description": "...", "parameters": {...}}}]
    """
    oai_tools = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        oai_tools.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        })
    return oai_tools


def _convert_tool_choice(tc) -> dict | str:
    """
    Anthropic tool_choice -> OpenAI tool_choice
    {"type": "auto"} -> "auto"
    {"type": "any"} -> "required"   (Claude Code 用 "any" 表示强制调用)
    {"type": "tool", "name": "xxx"} -> {"type": "function", "function": {"name": "xxx"}}
    """
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict):
        t = tc.get("type", "auto")
        if t == "any":
            return "required"
        elif t == "tool":
            return {"type": "function", "function": {"name": tc.get("name", "")}}
        elif t == "auto":
            return "auto"
        elif t == "none":
            return "none"
    return "auto"


def _convert_openai_tool_calls_to_anthropic(tool_calls: list) -> list:
    """
    OpenAI tool_calls -> Anthropic tool_use content blocks

    OpenAI:  [{"id": "call_xxx", "type": "function",
               "function": {"name": "calc", "arguments": '{"expr":"1+1"}'}}]
    Anthropic: [{"type": "tool_use", "id": "toolu_xxx", "name": "calc", "input": {"expr": "1+1"}}]
    """
    blocks = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        try:
            args = orjson.loads(tc.get("function", {}).get("arguments", "{}"))
        except (orjson.JSONDecodeError, TypeError, ValueError):
            args = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", _generate_id("toolu")),
            "name": tc.get("function", {}).get("name", ""),
            "input": args,
        })
    return blocks


def _convert_messages_anthropic_to_openai(anthropic_messages: list) -> list:
    """
    逐条转换 messages：
    - tool_use block -> OpenAI assistant message 的 tool_calls 字段
    - tool_result block -> OpenAI role:tool 消息
    - 其他文本 block -> 展平到 content 字符串
    """
    oai_messages = []

    for msg in anthropic_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 如果 content 是字符串，直接使用
        if isinstance(content, str):
            oai_messages.append({"role": role, "content": content})
            continue

        # 如果 content 是列表，需要检查各种 block 类型
        if isinstance(content, list):
            # ---- case: tool_result -> role:tool ----
            if role == "user" and _has_tool_result(content):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_content = block.get("content", "")
                        if isinstance(tool_content, list):
                            tool_content = _extract_text_from_blocks(tool_content)
                        elif not isinstance(tool_content, str):
                            tool_content = str(tool_content)
                        oai_messages.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": tool_content,
                        })
                continue

            # ---- case: tool_use -> assistant + tool_calls ----
            if role == "assistant" and _has_tool_use(content):
                text = _extract_text_from_blocks(content)
                oai_msg = {"role": "assistant", "content": text or None}
                tool_calls = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", _generate_id("call")),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": _j(block.get("input", {})),
                            },
                        })
                if tool_calls:
                    oai_msg["tool_calls"] = tool_calls
                oai_messages.append(oai_msg)
                continue

            # ---- 默认：纯文本 blocks ----
            text = _extract_text_from_blocks(content)
            oai_messages.append({"role": role, "content": text or ""})
            continue

        # fallback
        oai_messages.append({"role": role, "content": str(content) if content else ""})

    return oai_messages


# ============================================================
# 协议转换：Anthropic -> OpenAI
# ============================================================

def convert_anthropic_to_openai(anthropic_body: dict) -> tuple[dict, str]:
    """
    Anthropic /v1/messages -> OpenAI /chat/completions
    """
    openai_messages = []

    # ---- 1. system -> role:system 首条 ----
    system_content = anthropic_body.get("system")
    if system_content:
        if isinstance(system_content, list):
            texts = [b.get("text", "") for b in system_content
                     if isinstance(b, dict) and b.get("type") == "text"]
            system_text = "\n".join(texts).strip()
        else:
            system_text = str(system_content).strip()
        # 可选的系统提示词优化（剥离 Claude 特有内容）
        optimized = _optimize_system_prompt(system_text)
        if optimized:
            openai_messages.append({"role": "system", "content": optimized})
            if SYSTEM_PROMPT_OPTIMIZE:
                saved = len(system_text) - len(optimized)
                if saved > 0:
                    log.info("系统提示词优化: 减少 %d 字符 (%d -> %d)", saved, len(system_text), len(optimized))

    # ---- 2. messages 转换 ----
    raw_messages = anthropic_body.get("messages", [])
    openai_messages.extend(_convert_messages_anthropic_to_openai(raw_messages))

    # ---- 3. 模型名 ----
    original_model = anthropic_body.get("model", "")
    upstream_model = TJU_MODEL_OVERRIDE or original_model

    # ---- 4. 构建请求体（限制 max_tokens 上限避免上游超时） ----
    requested_tokens = anthropic_body.get("max_tokens", MAX_OUTPUT_TOKENS)
    capped_tokens = min(requested_tokens, MAX_OUTPUT_TOKENS)
    openai_body: dict = {
        "model": upstream_model,
        "messages": openai_messages,
        "max_tokens": capped_tokens,
        "stream": anthropic_body.get("stream", False),
    }
    if capped_tokens != requested_tokens:
        log.info("max_tokens 已截断: %d -> %d (上限 %d)", requested_tokens, capped_tokens, MAX_OUTPUT_TOKENS)

    # 可选参数透传
    for key in ("temperature", "top_p"):
        if key in anthropic_body:
            openai_body[key] = anthropic_body[key]

    # stop_sequences -> stop
    if "stop_sequences" in anthropic_body:
        openai_body["stop"] = anthropic_body["stop_sequences"]
    elif "stop" in anthropic_body:
        openai_body["stop"] = anthropic_body["stop"]

    # ---- 5. tools 转换 ----
    if "tools" in anthropic_body and anthropic_body["tools"]:
        openai_body["tools"] = _convert_anthropic_tools_to_openai(anthropic_body["tools"])

    # ---- 6. tool_choice 转换 ----
    if "tool_choice" in anthropic_body:
        openai_body["tool_choice"] = _convert_tool_choice(anthropic_body["tool_choice"])

    return openai_body, original_model


# ============================================================
# 非流式响应 → SSE 事件（让上游走非流式，假装流式给 Claude Code）
# ============================================================

def _nonstream_to_sse(anthropic_resp: dict) -> list[str]:
    """
    将非流式 Anthropic 响应转换为 SSE 事件列表，让 Claude Code 以为收到了流式。
    这样上游可以走非流式请求（更快），而 Claude Code 侧不受影响。
    """
    events = []

    # message_start
    events.append(
        "event: message_start\n"
        f"data: {_j({'type': 'message_start', 'message': {
            'id': anthropic_resp.get('id', _generate_id()),
            'type': 'message',
            'role': 'assistant',
            'content': [],
            'model': anthropic_resp.get('model', ''),
            'stop_reason': None,
            'stop_sequence': None,
            'usage': {'input_tokens': 0, 'output_tokens': 0},
        }})}\n\n"
    )

    content_blocks = anthropic_resp.get("content", [])
    for idx, block in enumerate(content_blocks):
        block_type = block.get("type", "text")

        # content_block_start
        events.append(
            "event: content_block_start\n"
            f"data: {_j({'type': 'content_block_start', 'index': idx, 'content_block': {
                'type': block_type,
                **({'id': block.get('id', _generate_id('toolu')), 'name': block.get('name', ''), 'input': {}} if block_type == 'tool_use' else {'text': ''}),
            }})}\n\n"
        )

        # content_block_delta（一次性发送全部内容）
        if block_type == "text":
            text = block.get("text", "")
            if text:
                events.append(
                    "event: content_block_delta\n"
                    f"data: {_j({'type': 'content_block_delta', 'index': idx, 'delta': {
                        'type': 'text_delta', 'text': text,
                    }})}\n\n"
                )
        elif block_type == "tool_use":
            inp = block.get("input", {})
            if inp:
                events.append(
                    "event: content_block_delta\n"
                    f"data: {_j({'type': 'content_block_delta', 'index': idx, 'delta': {
                        'type': 'input_json_delta',
                        'partial_json': _j(inp),
                    }})}\n\n"
                )

        # content_block_stop
        events.append(
            "event: content_block_stop\n"
            f"data: {_j({'type': 'content_block_stop', 'index': idx})}\n\n"
        )

    # message_delta（携带真实用量）
    events.append(
        "event: message_delta\n"
        f"data: {_j({'type': 'message_delta', 'delta': {
            'stop_reason': anthropic_resp.get('stop_reason'),
            'stop_sequence': None,
        }, 'usage': {
            'input_tokens': anthropic_resp.get('usage', {}).get('input_tokens', 0),
            'output_tokens': anthropic_resp.get('usage', {}).get('output_tokens', 0),
        }})}\n\n"
    )

    # message_stop
    events.append("event: message_stop\ndata: {}\n\n")

    return events


# ============================================================
# 协议转换：OpenAI -> Anthropic（非流式）
# ============================================================

def convert_openai_to_anthropic_nonstream(openai_resp: dict, original_model: str) -> dict:
    """
    OpenAI 非流式响应 -> Anthropic /v1/messages 响应
    支持 text + tool_calls 混合
    """
    choice = openai_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason")

    # content 构建
    content_blocks = []

    # 文本部分
    text = message.get("content") or ""
    if text:
        content_blocks.append({"type": "text", "text": text})

    # tool_calls 部分 -> tool_use blocks
    tool_calls = message.get("tool_calls")
    if tool_calls:
        content_blocks.extend(_convert_openai_tool_calls_to_anthropic(tool_calls))

    # 如果没有任何内容，给空文本（上游有些模型可能返回 content=null）
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    # usage 重命名
    usage = openai_resp.get("usage", {})
    anthropic_usage = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }

    return {
        "id": _generate_id(),
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": original_model,
        "stop_reason": _map_finish_reason(finish_reason),
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


# ============================================================
# 流式转换：OpenAI SSE -> Anthropic SSE（支持 Tool Calls）
# ============================================================

class SseConverter:
    """
    状态机：将 OpenAI SSE 逐块转换为 Anthropic SSE。

    状态流转:
      INIT -> (收到首个 delta) -> TEXT_BLOCK / TOOL_BLOCK
      TEXT_BLOCK -> (finish_reason) -> DONE
      TEXT_BLOCK -> (出现 tool_calls) -> TOOL_BLOCK
      TOOL_BLOCK -> (finish_reason) -> DONE
    """
    STATE_INIT = 0
    STATE_TEXT = 1
    STATE_TOOL = 2
    STATE_DONE = 3

    def __init__(self, original_model: str):
        self.model = original_model
        self.state = self.STATE_INIT
        self.message_id = _generate_id()
        self.input_tokens = 0
        self.output_tokens = 0
        self.block_index = 0
        self.tool_call_index = 0  # 跟踪 OpenAI tool_call index
        self.current_tool_name = ""
        self.current_tool_id = ""

    def _emit_message_start(self) -> str:
        return (
            "event: message_start\n"
            f"data: {_j({'type': 'message_start', 'message': {
                'id': self.message_id,
                'type': 'message',
                'role': 'assistant',
                'content': [],
                'model': self.model,
                'stop_reason': None,
                'stop_sequence': None,
                'usage': {'input_tokens': 0, 'output_tokens': 0},
            }})}\n\n"
        )

    def _emit_text_block_start(self) -> str:
        return (
            "event: content_block_start\n"
            f"data: {_j({'type': 'content_block_start', 'index': self.block_index, 'content_block': {
                'type': 'text',
                'text': '',
            }})}\n\n"
        )

    def _emit_tool_block_start(self, tool_id: str, name: str) -> str:
        self.current_tool_id = tool_id
        self.current_tool_name = name
        return (
            "event: content_block_start\n"
            f"data: {_j({'type': 'content_block_start', 'index': self.block_index, 'content_block': {
                'type': 'tool_use',
                'id': tool_id,
                'name': name,
                'input': {},
            }})}\n\n"
        )

    def _emit_text_delta(self, text: str) -> str:
        return (
            "event: content_block_delta\n"
            f"data: {_j({'type': 'content_block_delta', 'index': self.block_index, 'delta': {
                'type': 'text_delta',
                'text': text,
            }})}\n\n"
        )

    def _emit_input_json_delta(self, partial: str) -> str:
        return (
            "event: content_block_delta\n"
            f"data: {_j({'type': 'content_block_delta', 'index': self.block_index, 'delta': {
                'type': 'input_json_delta',
                'partial_json': partial,
            }})}\n\n"
        )

    def _emit_block_stop(self) -> str:
        return (
            "event: content_block_stop\n"
            f"data: {_j({'type': 'content_block_stop', 'index': self.block_index})}\n\n"
        )

    def _emit_message_delta(self, stop_reason: Optional[str]) -> str:
        return (
            "event: message_delta\n"
            f"data: {_j({'type': 'message_delta', 'delta': {
                'stop_reason': stop_reason,
                'stop_sequence': None,
            }, 'usage': {
                'input_tokens': max(self.input_tokens, 0),
                'output_tokens': max(self.output_tokens, 1),
            }})}\n\n"
        )

    def _emit_message_stop(self) -> str:
        return "event: message_stop\ndata: {}\n\n"

    def _close_current_block(self) -> list:
        """关闭当前 content block，递增 index"""
        events = []
        if self.state in (self.STATE_TEXT, self.STATE_TOOL):
            events.append(self._emit_block_stop())
            self.block_index += 1
        self.state = self.STATE_INIT
        return events

    def process_chunk(self, chunk: dict) -> list:
        """处理一个 OpenAI SSE chunk，返回 Anthropic SSE 事件列表"""
        events = []
        choices = chunk.get("choices", [])

        # ---- 处理上游用量信息（在 finish_reason 后单独发送的 usage chunk） ----
        if not choices:
            usage = chunk.get("usage")
            if usage:
                self.input_tokens = max(self.input_tokens, usage.get("prompt_tokens", 0))
                self.output_tokens = max(self.output_tokens, usage.get("completion_tokens", 0))
            return events

        delta = choices[0].get("delta", {})
        finish_reason = choices[0].get("finish_reason")

        # ---- 阶段：收到 finish_reason -> 收尾 ----
        if finish_reason and self.state not in (self.STATE_DONE, self.STATE_INIT):
            events.extend(self._close_current_block())
            stop_reason = _map_finish_reason(finish_reason)
            events.append(self._emit_message_delta(stop_reason))
            events.append(self._emit_message_stop())
            self.state = self.STATE_DONE
            return events

        # ---- 阶段：INIT -> 首次触发 ----
        if self.state == self.STATE_INIT:
            events.append(self._emit_message_start())

            # 检查是否有 tool_calls
            tool_calls = delta.get("tool_calls")
            content = delta.get("content")
            role = delta.get("role")

            if tool_calls:
                # 工具调用块
                tc = tool_calls[0]
                tc_id = tc.get("id", _generate_id("toolu"))
                tc_name = tc.get("function", {}).get("name", self.current_tool_name)
                events.append(self._emit_tool_block_start(tc_id, tc_name))
                self.state = self.STATE_TOOL
                # 可能有初始参数
                args = tc.get("function", {}).get("arguments", "")
                if args:
                    self.output_tokens += 1
                    events.append(self._emit_input_json_delta(args))
            elif content is not None:
                # 文本块
                events.append(self._emit_text_block_start())
                self.state = self.STATE_TEXT
                if content:
                    self.output_tokens += 1
                    events.append(self._emit_text_delta(content))
            elif role:
                # 有些模型先发 role 再发内容
                events.append(self._emit_text_block_start())
                self.state = self.STATE_TEXT

            return events

        # ---- 阶段：TEXT -> 文本增量（可能转型为 TOOL） ----
        if self.state == self.STATE_TEXT:
            # 检查是否转型为 tool 块（上游先发 text block 头部，再发 tool_calls）
            tool_calls = delta.get("tool_calls")
            if tool_calls:
                # 关闭当前文本块
                events.append(self._emit_block_stop())
                self.block_index += 1
                # 开启工具块
                tc = tool_calls[0]
                tc_id = tc.get("id", _generate_id("toolu"))
                tc_name = tc.get("function", {}).get("name", "")
                events.append(self._emit_tool_block_start(tc_id, tc_name))
                self.state = self.STATE_TOOL
                self.current_tool_id = tc_id
                self.current_tool_name = tc_name
                args = tc.get("function", {}).get("arguments", "")
                if args:
                    self.output_tokens += 1
                    events.append(self._emit_input_json_delta(args))
                return events
            # 普通文本增量
            content = delta.get("content")
            if content is not None and content != "":
                self.output_tokens += 1
                events.append(self._emit_text_delta(content))
            return events

        # ---- 阶段：TOOL -> 工具参数增量 ----
        if self.state == self.STATE_TOOL:
            tool_calls = delta.get("tool_calls")
            if tool_calls:
                tc = tool_calls[0]
                args = tc.get("function", {}).get("arguments", "")
                if args:
                    self.output_tokens += 1
                    events.append(self._emit_input_json_delta(args))
            return events

        return events

    def finish(self) -> list:
        """流意外结束时补发结束事件"""
        events = []
        if self.state not in (self.STATE_DONE, self.STATE_INIT):
            events.append(self._emit_block_stop())
            events.append(self._emit_message_delta("end_turn"))
            events.append(self._emit_message_stop())
            self.state = self.STATE_DONE
        return events


# ============================================================
# 核心端点：/v1/messages
# ============================================================

@app.post("/v1/messages")
async def handle_messages(request: Request):
    # ---- 1. 解析请求体 ----
    try:
        body = await request.json()
    except Exception as e:
        return Response(
            content=_j({"error": {"message": f"无法解析请求体 JSON: {str(e)}"}}),
            status_code=400, media_type="application/json",
        )

    # ---- 2. 协议转换 ----
    try:
        openai_body, original_model = convert_anthropic_to_openai(body)
    except Exception as e:
        log.exception("协议转换失败")
        return Response(
            content=_j({"error": {"message": f"协议转换内部错误: {str(e)}"}}),
            status_code=500, media_type="application/json",
        )

    is_stream = openai_body.get("stream", False)
    log.info("<- req: model=%s stream=%s msgs=%d max_tokens=%s tools=%s",
             original_model, is_stream,
             len(openai_body.get("messages", [])),
             openai_body.get("max_tokens"),
             "yes" if "tools" in openai_body else "no")

    # ---- 3. 检查 Token ----
    if not TJU_API_KEY:
        return Response(
            content=_j({"error": {"message": "服务端未配置 TJU_API_KEY"}}),
            status_code=500, media_type="application/json",
        )

    # ---- 4. 向上游发起请求（上游走非流式，更快返回；由代理转成 SSE 给 Claude Code） ----
    upstream_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TJU_API_KEY}",
        "Accept": "application/json",
    }
    # 关键：不管客户端要不要流式，上游始终走非流式
    openai_body["stream"] = False

    client = get_http_client()
    try:
        upstream_resp = await client.post(TJU_API_URL, headers=upstream_headers, json=openai_body)
    except httpx.TimeoutException:
        return Response(content=_j({"error": {"message": "上游 AI 平台请求超时"}}),
                        status_code=504, media_type="application/json")
    except httpx.RequestError as e:
        return Response(content=_j({"error": {"message": f"无法连接上游 AI 平台: {str(e)}"}}),
                        status_code=502, media_type="application/json")

    # ---- 5. 处理上游 HTTP 错误 ----
    if upstream_resp.status_code >= 400:
        err_body = b""
        try:
            err_body = await upstream_resp.aread()
        except Exception:
            err_body = upstream_resp.content[:2000]
        err_detail = err_body.decode("utf-8", errors="replace")[:500]
        log.error("上游返回错误 %s: %s", upstream_resp.status_code, err_detail)
        return Response(
            content=_j({"error": {"message": f"上游错误 (HTTP {upstream_resp.status_code})",
                                           "detail": err_detail}}),
            status_code=upstream_resp.status_code, media_type="application/json",
        )

    # ---- 6. 解析上游完整 JSON 响应 ----
    try:
        raw_body = await upstream_resp.aread()
        openai_resp = orjson.loads(raw_body)
    except orjson.JSONDecodeError as e:
        return Response(content=_j({"error": {"message": f"上游响应解析失败: {str(e)}"}}),
                        status_code=502, media_type="application/json")
    except Exception as e:
        return Response(content=_j({"error": {"message": f"上游响应处理异常: {str(e)}"}}),
                        status_code=502, media_type="application/json")

    # ---- 7. 转换为 Anthropic 格式 ----
    anthropic_resp = convert_openai_to_anthropic_nonstream(openai_resp, original_model)
    upstream_model_name = openai_resp.get("model", "?")
    log.info("-> resp: id=%s model=%s stop_reason=%s usage=%s content_blocks=%d",
             anthropic_resp.get("id", "?"),
             upstream_model_name,
             anthropic_resp.get("stop_reason"),
             anthropic_resp.get("usage"),
             len(anthropic_resp.get("content", [])))

    # ---- 8. 如果客户端请求流式，把整块响应拆成 SSE 事件 ----
    if is_stream:
        sse_events = _nonstream_to_sse(anthropic_resp)

        async def generate():
            for ev in sse_events:
                yield ev

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- 9. 非流式：直接返回 ----
    return anthropic_resp


# ============================================================
# 健康检查
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "TJU AI Platform Proxy",
        "version": "2.0.0",
        "endpoint": "/v1/messages",
        "upstream": TJU_API_URL,
        "token_configured": bool(TJU_API_KEY),
        "features": ["streaming", "tool_use", "content_blocks"],
    }


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    import uvicorn

    port = LISTEN_PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    if not TJU_API_KEY:
        log.warning("=" * 50)
        log.warning("  TJU_API_KEY 未设置！")
        log.warning("  请通过环境变量设置 Token 后再启动。")
        log.warning("=" * 50)

    log.info("-" * 50)
    log.info(" TJU AI Platform Proxy v2.0 (Full Edition)")
    log.info("-" * 50)
    log.info(" Listen:  http://%s:%d", LISTEN_HOST, port)
    log.info(" Upstream: %s", TJU_API_URL)
    log.info(" Stream:   supported")
    log.info(" Tool Use: supported")
    log.info(" Token:    %s", "OK" if TJU_API_KEY else "MISSING!")
    log.info(" Optimize: %s", "ON (strip Claude identity)" if SYSTEM_PROMPT_OPTIMIZE else "OFF")
    log.info(" Max Tokens: %d", MAX_OUTPUT_TOKENS)
    if TJU_MODEL_OVERRIDE:
        log.info(" Model:    %s (override)", TJU_MODEL_OVERRIDE)
    else:
        log.info(" Model:    passthrough")
    log.info("-" * 50)

    uvicorn.run(app, host=LISTEN_HOST, port=port, log_level="info", access_log=False)
