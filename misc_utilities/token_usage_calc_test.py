import os
import requests
import time
from typing import Dict, Optional

# --- 1. Set your test credentials ---
os.environ["LITELLM_URL"] = "https://litellm.prod.outshift.ai/v1/chat/completions"
# Using Teammate B's key for testing the agent 'user' tag tracking
TEST_API_KEY = "sk-<YOUR_TEST_KEY_HERE>" 
TEST_AGENT_ID = "financeguard"

def fetch_proxy_usage(api_key: str, user_id: Optional[str] = None, filter_model: Optional[str] = None) -> dict:
    """Fetches cumulative token usage from the LiteLLM Proxy."""
    base_url = os.environ["LITELLM_URL"].replace("/v1/chat/completions", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        if user_id:
            url = f"{base_url}/user/info?user_id={user_id}"
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                return {"total_tokens": response.json().get("user_info", {}).get("total_tokens", 0)}
                
        elif filter_model:
            url = f"{base_url}/key/info"
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                for metric in response.json().get("info", {}).get("model_metrics", []):
                    if metric.get("model") == filter_model:
                        return {"total_tokens": metric.get("total_tokens", 0)}
    except Exception as e:
        print(f"Proxy query failed: {e}")
        
    return {"total_tokens": 0}

# --- 2. Execute the Offline Delta Test ---
print("Taking baseline snapshot...")
baseline = fetch_proxy_usage(TEST_API_KEY, user_id=TEST_AGENT_ID).get("total_tokens", 0)
print(f"Baseline tokens: {baseline}")

print("\nSimulating LLM activity (Making a direct proxy call)...")
# Make a direct call to force the proxy to register new tokens
test_call = requests.post(
    os.environ["LITELLM_URL"],
    headers={"Authorization": f"Bearer {TEST_API_KEY}", "Content-Type": "application/json"},
    json={
        "model": "vertex_ai/gemini-2.5-flash",
        "messages": [{"role": "user", "content": "Say 'hello world' and nothing else."}],
        "user": TEST_AGENT_ID # Tagging the request just like the Hermes YAML does
    }
)

if test_call.status_code == 200:
    call_usage = test_call.json().get("usage", {}).get("total_tokens", 0)
    print(f"Direct API call reported {call_usage} tokens.")
else:
    print(f"Call failed: {test_call.text}")

print("\nWaiting 3 seconds for LiteLLM proxy database to sync...")
time.sleep(3)

print("Taking post-run snapshot...")
final_snapshot = fetch_proxy_usage(TEST_API_KEY, user_id=TEST_AGENT_ID).get("total_tokens", 0)
print(f"Final tokens: {final_snapshot}")

# --- 3. Calculate the Delta ---
session_delta = final_snapshot - baseline
print("\n" + "="*50)
print(f" OFFLINE TEST RESULT: {session_delta} tokens consumed in this run.")
print("="*50)