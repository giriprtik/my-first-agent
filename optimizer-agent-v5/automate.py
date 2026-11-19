from datetime import datetime
from agent import run_agent

# 1. Generate a unique timestamped filename so it never hits the overwrite block
timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
filename = f"briefing_{timestamp}.md"

# 2. Define the unattended automation task
task = (
    f"Search for today's top 3 technology and Nepal economic news headlines. "
    f"Save a clean summary with source links to a file named '{filename}'."
)

# 3. Execute the agent
if __name__ == "__main__":
    print(f"[{timestamp}] Starting automated research job...")
    result = run_agent(task)
    print(f"\n[JOB COMPLETE]\n{result}\n")
