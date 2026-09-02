import json
import httpx
import sys

base_url = "http://localhost:8080"
api_key = "test-key"

def send_request(prompt: str, session_id: str):
    payload = {
        "model": "claude-3-5-sonnet-20240620",
        "messages": [{"role": "user", "content": prompt}],
        "metadata": {
            "user_id": json.dumps({
                "session_id": session_id,
                "account_uuid": "42bfe041-7129-4bd4-bdf6-faa4aca299ba"
            })
        }
    }
    r = httpx.post(f"{base_url}/v1/messages", json=payload, headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"}, timeout=30.0)
    return r.json()

print("1. Sending ESDS_SUBMIT")
resp1 = send_request("ESDS_SUBMIT", "test_session_123")
print(json.dumps(resp1, indent=2))

text = "".join(b["text"] for b in resp1.get("content", []) if b["type"] == "text")

# Extract the pending ID from the text
import re
match = re.search(r"ESDS_APPROVE (\w+)", text)
if not match:
    print("FAILED to find ESDS_APPROVE in response")
    sys.exit(1)

pending_id = match.group(1)
print(f"Got pending_id: {pending_id}")

print("2. Sending ESDS_APPROVE")
resp2 = send_request(f"ESDS_APPROVE {pending_id}", "test_session_123")
print(json.dumps(resp2, indent=2))

