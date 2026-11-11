from google.adk.agents import LlmAgent

root_agent = LlmAgent(
    name="orchestrator",
    model="gemini-3.8-flash",
    instruction="Sen bir perakende mağaza operasyon asistanısın. Türkçe ve kısa cevap ver.",
)
