import os
from dotenv import load_dotenv
load_dotenv()
from monitor.runner_soccer import run_soccer

if __name__ == "__main__":
    run_soccer()
