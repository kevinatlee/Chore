import json
import os
import sys
import urllib.request


request = urllib.request.Request(
    "http://127.0.0.1:8000/health/",
    headers={
        "Host": os.environ.get("APP_FQDN", "localhost"),
        "X-Forwarded-Proto": "https",
    },
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        healthy = response.status == 200 and json.load(response) == {"status": "ok"}
except Exception:
    healthy = False
raise SystemExit(0 if healthy else 1)
