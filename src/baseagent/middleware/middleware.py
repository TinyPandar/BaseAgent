

class Middleware:
    def __init__(self):
        def before_model(self, state):
            pass
        def after_model(self, state, response):
            pass
        def before_tool(self, state, tool_call):
            pass
        def after_tool(self, state, tool_call, tool_result):
            pass
