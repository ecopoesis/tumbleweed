"""The agent runtime: conversation loop, tool registry and LLM providers (from S03 on).

Never imports `gi` and never imports the shell: the shell depends on the agent through the
interfaces defined here (ARCHITECTURE section 10).
"""
