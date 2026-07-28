import httpx, json

print("=== Test 3: 流式 + 工具调用 ===")
text_content = ""
tool_calls_found = False
input_json_found = False

with httpx.Client() as client:
    with client.stream("POST", "http://127.0.0.1:8000/v1/messages", json={
        "model": "tju-llm",
        "max_tokens": 512,
        "stream": True,
        "messages": [{"role": "user", "content": "计算 1+1 等于多少？"}],
        "tools": [{"name": "calc", "description": "数学计算",
                   "input_schema": {"type": "object", "properties": {
                       "expr": {"type": "string"}},
                       "required": ["expr"]}}]
    }, timeout=60) as r:
        for line in r.iter_lines():
            if line.startswith("event: "):
                ev = line[7:]
                if ev == "content_block_delta":
                    pass
            elif line.startswith("data: "):
                data = line[6:]
                if "input_json_delta" in data:
                    input_json_found = True
                if "tool_use" in data:
                    tool_calls_found = True
                if data.strip() not in ("[DONE]", "", "{}"):
                    text_content += data + "\n"

if input_json_found:
    print("[OK] 流式 tool_use: input_json_delta 事件正常!")
elif tool_calls_found:
    print("[OK] tool_use 出现在响应中!")
else:
    print("[INFO] 模型直接回复（未调用工具）")

# 展示事件摘要
events = [l for l in text_content.split("\n") if l]
for e in events[:5]:
    try:
        obj = json.loads(e)
        t = obj.get("type", "")
        if t == "content_block_delta":
            dt = obj.get("delta", {})
            dt_type = dt.get("type", "")
            print(f"  - delta type: {dt_type}")
        elif t == "content_block_start":
            cb = obj.get("content_block", {})
            print(f"  - block start: type={cb.get('type')}")
        elif t == "message_delta":
            sr = obj.get("delta", {}).get("stop_reason")
            print(f"  - message_delta: stop_reason={sr}")
    except:
        pass
