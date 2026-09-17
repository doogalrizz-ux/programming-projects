"""
Standalone connectivity check for the Gemini API -- deliberately not
wired into config.py/.env, so it never touches your real API key file.
Set GEMINI_API_KEY as a one-off environment variable in your own
terminal instead (never paste a real key into chat with an assistant).

Usage (PowerShell):
    cd "D:\\Programing Projects\\HomeGhost"
    .venv\\Scripts\\activate
    $env:GEMINI_API_KEY = "paste-your-real-key-here"
    python test_gemini_connection.py

A fast SUCCESS means the network path to Google's API is fine and any
bot-side issue is elsewhere. A fast FAILURE with a clear error usually
means an auth/quota problem. Hanging well past ~15s and then timing out
points at something (firewall, VPN, security software) silently
blocking or dropping the connection.
"""
import os
import sys
import time

from google import genai
from google.genai import types

key = os.environ.get("GEMINI_API_KEY")
if not key:
    print("Set GEMINI_API_KEY as an environment variable first -- see the docstring at the top of this file.")
    sys.exit(1)

client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=15_000))

print("Sending a test request to Gemini...")
start = time.time()
try:
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents="Reply with a five-word-or-fewer greeting.",
    )
    elapsed = time.time() - start
    print(f"SUCCESS in {elapsed:.1f}s: {response.text!r}")
except Exception as e:
    elapsed = time.time() - start
    print(f"FAILED after {elapsed:.1f}s: {type(e).__name__}: {e}")
