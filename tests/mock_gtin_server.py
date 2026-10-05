"""Local provider mock for E2E: GTIN_TEST_BASE=http://127.0.0.1:8768

Valid key = MOCK_GTIN_KEY (default TEST_KEY).
GTIN 0000000000000 / containing '000000' → not found.
GTIN containing '429' → 429.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))
from fixtures import (  # noqa: E402
    BARCODELOOKUP_EMPTY,
    BARCODELOOKUP_OK,
    OFF_MISS,
    OFF_OK,
    UPCITEMDB_DENIED,
    UPCITEMDB_EMPTY,
    UPCITEMDB_OK,
)

VALID = os.getenv("MOCK_GTIN_KEY", "TEST_KEY")


class H(BaseHTTPRequestHandler):
    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if code == 429:
            self.send_header("Retry-After", "0")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path

        if path in ("/prod/v1/lookup", "/prod/trial/lookup"):
            if path.startswith("/prod/v1") and self.headers.get("user_key") != VALID:
                return self._json(401, UPCITEMDB_DENIED)
            upc = q.get("upc") or ""
            if "429" in upc:
                return self._json(429, {"code": "EXCEED_LIMIT", "message": "rate"})
            if "000000" in upc or upc == "9999999999999":
                return self._json(200, UPCITEMDB_EMPTY)
            return self._json(200, UPCITEMDB_OK)

        if path == "/v3/products":
            if q.get("key") != VALID:
                return self._json(403, {"message": f"Invalid API key: {q.get('key')}"})
            bc = q.get("barcode") or ""
            if "000000" in bc:
                return self._json(200, BARCODELOOKUP_EMPTY)
            return self._json(200, BARCODELOOKUP_OK)

        if path.startswith("/api/v2/product/"):
            code = path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".json")
            if code == "3017620422003":
                return self._json(200, OFF_OK)
            return self._json(200, OFF_MISS)

        self._json(404, {"error": f"unknown path {path}"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(os.getenv("PORT", "8768"))), H).serve_forever()
