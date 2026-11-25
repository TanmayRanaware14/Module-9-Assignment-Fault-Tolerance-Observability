#!/usr/bin/env python3
"""Checkout service. Calls Payment and records logs, latency, and counts."""

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("CHECKOUT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHECKOUT_PORT", "9001"))
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://127.0.0.1:9002/payments")
TIMEOUT = float(os.environ.get("CHECKOUT_TIMEOUT", "1"))

lock = threading.Lock()
success_count = 0
failure_count = 0
last_latency_ms = 0.0


def metrics():
    with lock:
        total = success_count + failure_count
        error_rate = (failure_count / total) if total else 0.0
        return {
            "service": "checkout",
            "success_count": success_count,
            "failure_count": failure_count,
            "request_count": total,
            "error_rate": round(error_rate, 2),
            "last_latency_ms": round(last_latency_ms, 1),
            "timeout_seconds": TIMEOUT,
        }


def record(trace_id, status, latency_ms):
    global success_count, failure_count, last_latency_ms
    with lock:
        if status == "success":
            success_count += 1
        else:
            failure_count += 1
        last_latency_ms = latency_ms
        total = success_count + failure_count
        error_rate = (failure_count / total) if total else 0.0
        snapshot = {
            "success_count": success_count,
            "failure_count": failure_count,
            "error_rate": error_rate,
        }
    print(
        f"[checkout] trace_id={trace_id} status={status} latency_ms={latency_ms:.1f} "
        f"success_count={snapshot['success_count']} failure_count={snapshot['failure_count']} "
        f"error_rate={snapshot['error_rate']:.2f}",
        flush=True,
    )
    return snapshot


class CheckoutHandler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "Content-Length must be an integer."
        if length <= 0:
            return None, "Request body is required."
        try:
            data = json.loads(self.rfile.read(length).decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "Request body must be JSON."
        if not isinstance(data, dict):
            return None, "Request body must be a JSON object."
        return data, None

    def do_GET(self):
        try:
            if self.path == "/health":
                self._send(200, {"status": "ok", "service": "checkout"})
                return
            if self.path == "/metrics":
                self._send(200, metrics())
                return
            self._send(404, {"error": "not_found"})
        except Exception as exc:
            print(f"[checkout] request failed: {exc}", flush=True)
            self._send(500, {"error": "internal_error"})

    def do_POST(self):
        try:
            if self.path != "/orders":
                self._send(404, {"error": "not_found"})
                return
            self._checkout()
        except Exception as exc:
            print(f"[checkout] request failed: {exc}", flush=True)
            self._send(500, {"error": "internal_error", "message": "Checkout is still running."})

    def _checkout(self):
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex[:12]
        data, error = self._read_json()
        if error:
            record(trace_id, "failure", (time.perf_counter() - started) * 1000)
            self._send(400, {"error": "bad_request", "message": error, "trace_id": trace_id})
            return
        order_id = data.get("order_id")
        amount = data.get("amount")
        if not isinstance(order_id, str) or not order_id:
            record(trace_id, "failure", (time.perf_counter() - started) * 1000)
            self._send(400, {"error": "bad_request", "message": "order_id is required.", "trace_id": trace_id})
            return
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            record(trace_id, "failure", (time.perf_counter() - started) * 1000)
            self._send(400, {"error": "bad_request", "message": "amount must be positive.", "trace_id": trace_id})
            return

        print(
            f"[checkout] trace_id={trace_id} event=request_start order_id={order_id} "
            f"amount={amount} timeout_seconds={TIMEOUT}",
            flush=True,
        )
        payload = json.dumps({"order_id": order_id, "amount": amount}).encode()
        request = urllib.request.Request(
            PAYMENT_URL,
            data=payload,
            headers={"Content-Type": "application/json", "X-Trace-Id": trace_id},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            record(trace_id, "failure", latency_ms)
            print(
                f"[checkout] trace_id={trace_id} event=timeout order_id={order_id} "
                f"error={exc}",
                flush=True,
            )
            self._send(
                504,
                {
                    "error": "payment_timeout",
                    "trace_id": trace_id,
                    "order_id": order_id,
                    "message": "Payment did not respond before the checkout timeout. Checkout is still running.",
                },
            )
            return

        latency_ms = (time.perf_counter() - started) * 1000
        record(trace_id, "success", latency_ms)
        self._send(
            200,
            {
                "order_id": order_id,
                "status": "paid",
                "trace_id": trace_id,
                "payment_status": body.get("status"),
                "latency_ms": round(latency_ms, 1),
            },
        )

    def log_message(self, fmt, *args):
        return


def main():
    server = ThreadingHTTPServer((HOST, PORT), CheckoutHandler)
    print(f"[checkout] listening on http://{HOST}:{PORT}", flush=True)
    print(f"[checkout] payment url {PAYMENT_URL} timeout_seconds={TIMEOUT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
