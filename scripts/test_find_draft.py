import sys
from pathlib import Path

# Add the gateway to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.policies.write import find_draft, validate_draft
from gateway.protocol.normalized import NormalizedResponse

text = """● I provided a concise overview of concurrency models...
  {
    "content": "Reviewed concurrency fundamentals",
    "knowledge": {
      "title": "Concurrency Models Overview",
      "summary": "Threads/processes enable true parallelism",
      "outcome": "insight_found",
      "key_points": [
        "Async best for I/O-bound"
      ],
      "next_steps": [
        "Apply to specific codebase pattern if needed"
      ]
    }
  }

  To save this, type ESDS_APPROVE bc4a8f14
"""

class DummyResponse:
    def __init__(self, t):
        self.text = t

draft = find_draft(DummyResponse(text))
print("Draft found:", draft)
if draft:
    try:
        validate_draft(draft)
        print("Draft valid!")
    except Exception as e:
        print("Validation error:", e)
