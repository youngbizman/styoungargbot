import os
from dotenv import load_dotenv
load_dotenv()
from monitor.runner_ufc import run_ufc

if __name__ == "__main__":
    run_ufc()
