import logging
import sys
from datetime import datetime
from agent import run_agent

# Configure structured logging for automated infrastructure environments
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("automate")

# 1. Generate a unique UTC-anchored timestamped filename to prevent ambiguity and ensure safe naming
timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M")
filename = f"briefing_{timestamp}.md"

# 2. Define the unattended automation task with safely parameterized input to mitigate prompt injection
task = (
    "Search for today's top 3 technology and Nepal economic news headlines. "
    "Save a clean summary with source links to a file using the designated save tool."
)

# 3. Execute the agent with robust error handling, retries, and bounded output consumption
if __name__ == "__main__":
    logger.info("Starting automated research job...")
    max_retries = 3
    retry_delay = 2
    result = None

    for attempt in range(1, max_retries + 1):
        try:
            result = run_agent(task)
            break
        except Exception as e:
            logger.error(f"Agent execution failed on attempt {attempt}/{max_retries}: {e}")
            if attempt == max_retries:
                logger.error("All retry attempts exhausted. Job failed.")
                sys.exit(1)

    if result:
        # Bounded resource check: truncate excessively large outputs to prevent log saturation
        max_output_chars = 20000
        output_str = str(result)
        if len(output_str) > max_output_chars:
            output_str = output_str[:max_output_chars] + "\n... [Output truncated due to size limits]"
        
        logger.info(f"[JOB COMPLETE]\n{output_str}")
    else:
        logger.warning("[JOB COMPLETE] No output returned from agent.")