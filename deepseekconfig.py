import os


API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not API_KEY:
    raise RuntimeError("Missing DEEPSEEK_API_KEY environment variable.")

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"
AVAILABLE_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
]

# DeepSeek V4 thinking controls.
ENABLE_THINKING_MODE = True
REASONING_EFFORT = "max"

# Auxiliary calls should stay quick and cheap unless explicitly enabled.
SUMMARY_MODEL = MODEL
ROUTER_MODEL = MODEL
SUMMARY_THINKING_ENABLED = False
ROUTER_THINKING_ENABLED = False
