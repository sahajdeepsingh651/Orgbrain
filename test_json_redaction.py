from gateway.policies.pii import scan_text
vault = {}
text = """Here is the JSON:
```json
{
  "full_name": "Alice Smith",
  "bank_account": "HDFC-000012345"
}
```"""
redacted = scan_text(text, vault)
print(redacted)
