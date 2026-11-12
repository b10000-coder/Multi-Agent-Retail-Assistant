from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool

from retail_agent import config, prompts
from retail_agent.specialists.analytics import sales_analytics_agent
from retail_agent.specialists.catalog import product_catalog_agent
from retail_agent.specialists.policy import policy_qa_agent

# Hub-and-spoke: specialists are tools of the orchestrator, so control stays here.
root_agent = LlmAgent(
    name="orchestrator",
    model=config.MODEL_ID,
    instruction=prompts.ORCHESTRATOR_INSTRUCTION,
    tools=[
        AgentTool(agent=sales_analytics_agent),
        AgentTool(agent=policy_qa_agent),
        AgentTool(agent=product_catalog_agent),
    ],
)