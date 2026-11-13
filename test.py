from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.tools.mcp import MCPClient

# AWS_IAM Gateway: mcp-proxy-for-aws 가 SigV4 서명
mcp = MCPClient(lambda: stdio_client(StdioServerParameters(
    command="uvx", args=["mcp-proxy-for-aws@latest", "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A262976740991%3Aruntime%2Fkorea_weather-VtyvFj9CQa/invocations?qualifier=DEFAULT", "--region", "us-west-2"])))

with mcp:
    agent = Agent(tools=mcp.list_tools_sync())
    agent("사용 가능한 도구를 알려줘")