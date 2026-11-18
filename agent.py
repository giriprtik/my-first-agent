import os
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError, ClientError
from tavily import TavilyClient
from tools import save_research_note  # Import your sandboxed tool

# 1. Load environment variables
load_dotenv()

gemini_client = genai.Client(
    http_options=types.HttpOptions(
        timeout=30000,
        retry_options=types.HttpRetryOptions(attempts=1)
    )
)
tavily_client = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY", ""))

# 2. Define the Web Search Tool
def search_web(query: str) -> str:
    """Searches the live web for recent news, facts, and current events."""
    print(f"[TOOL LOG] Searching web for: '{query}'")
    response = tavily_client.search(query=query, max_results=3)
    
    results = []
    for item in response.get("results", []):
        results.append(f"Title: {item['title']}\nURL: {item['url']}\nContent: {item['content']}\n")
    return "\n---\n".join(results)

# 3. Configure the Agent's Brain & Guidelines
system_prompt = """
You are a precise Research Assistant with restricted file-saving access.
- Use `search_web` whenever the user asks about recent events, live data, or facts you are unsure about.
- Do NOT use `search_web` for simple greetings, basic math, or follow-up questions answerable from chat history.
- Only use `save_research_note` when the user explicitly asks you to save, write, or export a note/file.
- Never attempt to overwrite existing files or access directories outside your sandbox.
- Always summarize findings in 3 concise bullet points and include source URLs.
- If the search returns no useful data, say "I could not find verified information on that."
"""

MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-4-hydrogen",
    "gemini-3.7-flash"
]

def create_agent_session(model_name: str, history=None):
    return gemini_client.chats.create(
        model=model_name,
        history=history,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[search_web, save_research_note],  # Both tools attached here
            temperature=0.2,
        ),
    )

def send_with_fallback(chat, prompt: str):
    for model_name in MODELS:
        print(f"[STATUS] Trying {model_name}...")
        try:
            if getattr(chat, "_model", None) != model_name:
                chat = create_agent_session(model_name, history=chat.get_history())
            
            start_time = time.perf_counter()
            response = chat.send_message(prompt)
            latency = time.perf_counter() - start_time
            
            usage_metadata = getattr(response, "usage_metadata", None)
            prompt_tokens = getattr(usage_metadata, "prompt_token_count", 0) if usage_metadata else 0
            candidates_tokens = getattr(usage_metadata, "candidates_token_count", 0) if usage_metadata else 0
            total_tokens = getattr(usage_metadata, "total_token_count", 0) if usage_metadata else 0
            
            if prompt_tokens is None:
                prompt_tokens = 0
            if candidates_tokens is None:
                candidates_tokens = 0
            if total_tokens is None:
                total_tokens = 0

            print(f"[METRICS] Model: {model_name} | Latency: {latency:.2f}s | Tokens: {total_tokens} (in: {prompt_tokens}, out: {candidates_tokens})")
            
            metrics_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": model_name,
                "latency_sec": latency,
                "prompt_tokens": prompt_tokens,
                "candidates_tokens": candidates_tokens,
                "total_tokens": total_tokens
            }
            
            metrics_path = Path(__file__).parent / "agent_metrics.jsonl"
            with open(metrics_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(metrics_entry) + "\n")
                
            return chat, response.text
        except Exception as e:
            print(f"[WARNING] {model_name} failed: {e}")
    raise RuntimeError("All models failed. See warnings above.")

def run_agent(user_prompt: str) -> str:
    chat = create_agent_session(MODELS[0])
    _, text = send_with_fallback(chat, user_prompt)
    return text

# 4. Interactive Terminal Chat Loop
if __name__ == "__main__":
    chat = create_agent_session(MODELS[0])
    print("Research Agent Ready! Type 'exit' or 'quit' to stop.\n")

    while True:
        user_input = input("You: ").strip()
        
        if not user_input:
            continue
        if user_input.lower() in ["exit", "quit"]:
            print("Goodbye!")
            break
            
        try:
            chat, reply = send_with_fallback(chat, user_input)
            print(f"\nAgent:\n{reply}\n")
        except Exception as e:
            print(f"\n[ERROR] {e}\n")