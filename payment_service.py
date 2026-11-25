#!/usr/bin/env python3
"""Payment service. Approves a payment and can delay a response."""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("PAYMENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("PAYMENT_PORT", "9002"))

lock = threading.Lock()
delay_seconds = 0.0
success_count = 0
failure_count = 0
last_latency_ms = 0.0


def metrics():
    with lock:
        total = success_count + failure_count
        error_rate = (failure_count / total) if total else 0.0
        return {
            "service": "payment",
            "success_count": success_count,
            "failure_count": failure_count,
            "request_count": total,
            "error_rate": round(error_rate, 2),
            "last_latency_ms": round(last_latency_ms, 1),
            "delay_seconds": delay_seconds,
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
        f"[payment] trace_id={trace_id} status={status} latency_ms={latency_ms:.1f} "
        f"success_count={snapshot['success_count']} failure_count={snapshot['failure_count']} "
        f"error_rate={snapshot['error_rate']:.2f}",
        flush=True,
    )


class PaymentHandler(BaseHTTPRequestHandler):
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
        if self.path == "/health":
            self._send(200, {"status": "ok", "service": "payment"})
            return
        if self.path == "/metrics":
            self._send(200, metrics())
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if self.path == "/admin/delay":
            self._set_delay()
            return
        if self.path == "/payments":
            self._pay()
            return
        self._send(404, {"error": "not_found"})

    def _set_delay(self):
        global delay_seconds
        data, error = self._read_json()
        if error:
            self._send(400, {"error": "bad_request", "message": error})
            return
        seconds = data.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            self._send(400, {"error": "bad_request", "message": "seconds must be 0 or greater."})
            return
        with lock:
            delay_seconds = float(seconds)
        print(f"[payment] delay_seconds={delay_seconds}", flush=True)
        self._send(200, {"delay_seconds": delay_seconds})

    def _pay(self):
        started = time.perf_counter()
        trace_id = self.headers.get("X-Trace-Id", "missing")
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

        with lock:
            wait = delay_seconds
        print(
            f"[payment] trace_id={trace_id} event=request_start order_id={order_id} "
            f"amount={amount} delay_seconds={wait}",
            flush=True,
        )
        if wait:
            time.sleep(wait)
        latency_ms = (time.perf_counter() - started) * 1000
        record(trace_id, "success", latency_ms)
        self._send(
            200,
            {
                "status": "approved",
                "order_id": order_id,
                "amount": amount,
                "trace_id": trace_id,
                "latency_ms": round(latency_ms, 1),
            },
        )

    def log_message(self, fmt, *args):
        return


def main():
    server = ThreadingHTTPServer((HOST, PORT), PaymentHandler)
    print(f"[payment] listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
