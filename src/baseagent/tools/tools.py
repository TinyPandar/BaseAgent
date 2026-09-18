def search_knowledge_base(query: str):
    # 后面这里接你的 RAG
    return {
        "query": query,
        "results": [
            "七天内支持无理由退货。",
            "商品拆封后不影响二次销售可申请退货。",
        ],
    }


def get_product(product_id: str):
    # 后面换 PostgreSQL
    products = {
        "1001": {
            "name": "65W GaN 充电器",
            "price": 129,
            "watt": 65,
        }
    }

    return products.get(product_id, {"error": "product not found"})


TOOL_REGISTRY = {
    "search_knowledge_base": search_knowledge_base,
    "get_product": get_product,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "搜索客服知识库，包括售后规则、FAQ和历史客服案例",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "用于知识库搜索的问题",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": "根据商品ID查询商品详细信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "商品ID",
                    }
                },
                "required": ["product_id"],
            },
        },
    },
]