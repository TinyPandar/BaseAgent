import json


from baseagent.tools.tools import TOOLS, TOOL_REGISTRY
from baseagent.llm import client, MODEL
from baseagent.middleware import Middleware

def run_agent(user_input: str, max_steps: int = 8):
    middleware = Middleware()
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个电商客服 Agent。"
                "优先使用工具查询事实，不要编造商品信息和售后规则。"
            ),
        },
        {
            "role": "user",
            "content": user_input,
        },
    ]
    # Agent 循环
    for step in range(max_steps):
        #=========================
        # 调用 DeepSeek 处理
        #=========================
        middleware.before_model(messages)
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        message = response.choices[0].message
        middleware.after_model(messages, response)
        # 重要：把 assistant 的消息加入上下文
        messages.append(message)

        # =========================
        # 没调用工具 → Agent 完成
        # =========================

        if not message.tool_calls:
            return message.content

        # =========================
        # 执行所有 Tool Calls
        # =========================

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            try:
                arguments = json.loads(
                    tool_call.function.arguments
                )
            except json.JSONDecodeError:
                result = {
                    "error": "invalid tool arguments"
                }
            else:
                tool = TOOL_REGISTRY.get(tool_name)
                if tool is None:
                    result = {
                        "error": f"unknown tool: {tool_name}"
                    }
                else:
                    middleware.before_tool(messages, tool_call)
                    try:
                        result = tool(**arguments)
                    except Exception as e:
                        result = {
                            "error": str(e)
                        }
            middleware.after_tool(messages, tool_call, result)
            # Tool 执行结果重新塞回上下文
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(
                        result,
                        ensure_ascii=False,
                    ),
                }
            )

    return "Agent exceeded maximum steps."