import socket
import json
import csv
import os
import time
import threading
import pickle
import numpy as np
import pandas as pd

MONITOR_PORT = 9999
METRICS_FILE = "data/metrics.csv"
MODEL_FILE = "model.pkl"
PREDICTION_INTERVAL = 5
FAILURE_THRESHOLD = 0.75
DASHBOARD_PORT = 9998

# Shared state
node_metrics = {}
node_status = {}
model = None

# ── CSV Setup ─────────────────────────────────────────────────────────────────
def setup_csv():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(METRICS_FILE):
        with open(METRICS_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["node_id", "battery", "packet_loss", "timestamp", "label"])
        print("[Monitor] metrics.csv created")

# ── Metrics Collector ─────────────────────────────────────────────────────────
def collect_metrics():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", MONITOR_PORT))
    sock.settimeout(2.0)
    print(f"[Monitor] Listening on port {MONITOR_PORT}")

    with open(METRICS_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        while True:
            try:
                data, _ = sock.recvfrom(4096)
                packet = json.loads(data.decode())

                if packet.get("type") != "HEARTBEAT":
                    continue

                nid = packet["node_id"]
                battery = packet["battery"]
                packet_loss = packet["packet_loss"]
                timestamp = packet["timestamp"]

                if battery < 20 or packet_loss > 60:
                    label = "pre_failure"
                else:
                    label = "healthy"

                node_metrics[nid] = {
                    "battery": battery,
                    "packet_loss": packet_loss,
                    "timestamp": timestamp,
                    "label": label
                }

                writer.writerow([nid, battery, packet_loss, timestamp, label])
                f.flush()

            except socket.timeout:
                pass
            except Exception as e:
                print(f"[Monitor] Collect error: {e}")

# ── Trigger Reroute ───────────────────────────────────────────────────────────
def trigger_reroute(node_id):
    import hmac as hmac_lib
    import hashlib

    SHARED_KEY = b"wsn_secret_key"
    NEIGHBOUR_PORTS = {
        1: [5002, 5003],
        2: [5001, 5004, 5005],
        3: [5001, 5005],
        4: [5002, 5006],
        5: [5002, 5003, 5007],
        6: [5004, 5008],
        7: [5005, 5008],
        8: []
    }

    payload = {
        "type": "RERR",
        "from": 0,
        "failed_node": node_id,
        "timestamp": time.time()
    }
    msg = json.dumps(payload, sort_keys=True).encode()
    payload["signature"] = hmac_lib.new(SHARED_KEY, msg, hashlib.sha256).hexdigest()
    signed_msg = json.dumps(payload).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for port in NEIGHBOUR_PORTS.get(node_id, []):
        sock.sendto(signed_msg, ("127.0.0.1", port))
    print(f"[Monitor] ⚡ Preemptive RERR sent for Node {node_id} — neighbours notified")

# ── Send to Dashboard ─────────────────────────────────────────────────────────
def send_to_dashboard(node_id, status, battery, packet_loss, prob):
    update = {
        "node_id": node_id,
        "status": status,
        "battery": battery,
        "packet_loss": packet_loss,
        "prob": round(prob, 2)
    }
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(json.dumps(update).encode(), ("127.0.0.1", DASHBOARD_PORT))
    except Exception as e:
        print(f"[Monitor] Dashboard send error: {e}")
    finally:
        sock.close()

# ── Live Prediction ───────────────────────────────────────────────────────────
def run_predictions():
    global model
    while True:
        time.sleep(PREDICTION_INTERVAL)

        if model is None:
            if os.path.exists(MODEL_FILE):
                with open(MODEL_FILE, "rb") as f:
                    model = pickle.load(f)
                print("[Monitor] Model loaded successfully")
            else:
                print("[Monitor] No model found — run train_model.py first")
                continue

        if not node_metrics:
            continue

        print("\n[Monitor] ── Prediction Report ──────────────────")
        for nid, metrics in node_metrics.items():
            features = pd.DataFrame(
                [[metrics["battery"], metrics["packet_loss"]]],
                columns=["battery", "packet_loss"]
            )
            prob = model.predict_proba(features)[0][1]

            if prob > FAILURE_THRESHOLD:
                status = "at_risk"
                print(f"  Node {nid} → ⚠ AT RISK  | Battery: {metrics['battery']}% | Loss: {metrics['packet_loss']}% | Prob: {prob:.2f}")
                trigger_reroute(nid)
            else:
                status = "healthy"
                print(f"  Node {nid} → ✔ Healthy  | Battery: {metrics['battery']}% | Loss: {metrics['packet_loss']}% | Prob: {prob:.2f}")

            node_status[nid] = status
            send_to_dashboard(nid, status, metrics["battery"], metrics["packet_loss"], prob)

        # ── Failed node detection — inside while True loop ────────────────
        for nid in list(node_metrics.keys()):
            last_seen = node_metrics[nid].get("timestamp", 0)
            if time.time() - last_seen > 10:
                if node_status.get(nid) != "failed":
                    node_status[nid] = "failed"
                    send_to_dashboard(
                        nid, "failed",
                        node_metrics[nid]["battery"],
                        node_metrics[nid]["packet_loss"],
                        1.0
                    )
                    print(f"[Monitor] Node {nid} marked FAILED — no heartbeat for 10s")

        print("[Monitor] ─────────────────────────────────────────\n")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    setup_csv()

    threads = [
        threading.Thread(target=collect_metrics, daemon=True),
        threading.Thread(target=run_predictions, daemon=True),
    ]

    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[Monitor] Shutting down")

if __name__ == "__main__":
    main()