import socket
import threading
import hmac
import hashlib
import time
import json
import argparse
import random
import copy

from router import get_next_hop, TOPOLOGY, remove_node

# ── Config ────────────────────────────────────────────────────────────────────
SHARED_KEY = b"wsn_secret_key"
MONITOR_PORT = 9999
HEARTBEAT_INTERVAL = 2
FAULT_CHECK_INTERVAL = 5
UNREACHABLE_TIMEOUT = 6

# Separate neighbour port map — only used for sending UDP packets
# Format: node_id → list of (neighbour_id, port)
NEIGHBOUR_PORTS = {
    1: [(2, 5002), (3, 5003)],
    2: [(1, 5001), (4, 5004), (5, 5005)],
    3: [(1, 5001), (5, 5005)],
    4: [(2, 5002), (6, 5006)],
    5: [(2, 5002), (3, 5003), (7, 5007)],
    6: [(4, 5004), (8, 5008)],
    7: [(5, 5005), (8, 5008)],
    8: []
}

# ── HMAC Signing ──────────────────────────────────────────────────────────────
def sign_packet(data: dict) -> str:
    msg = json.dumps(data, sort_keys=True).encode()
    return hmac.new(SHARED_KEY, msg, hashlib.sha256).hexdigest()

def verify_packet(data: dict, signature: str) -> bool:
    return hmac.compare_digest(sign_packet(data), signature)

# ── Broadcast RERR ────────────────────────────────────────────────────────────
def broadcast_rerr(sock, node_id, failed_node_id, neighbours):
    payload = {
        "type": "RERR",
        "from": node_id,
        "failed_node": failed_node_id,
        "timestamp": time.time()
    }
    payload["signature"] = sign_packet(payload)
    msg = json.dumps(payload).encode()
    for _, port in neighbours:
        try:
            sock.sendto(msg, ("127.0.0.1", port))
        except Exception as e:
            print(f"[Node {node_id}] RERR send error: {e}")

# ── Heartbeat Sender ──────────────────────────────────────────────────────────
def send_heartbeats(sock, node_id, neighbours, battery_ref, packet_loss_ref, seq_ref):
    while True:
        payload = {
            "type": "HEARTBEAT",
            "node_id": node_id,
            "seq": seq_ref[0],
            "battery": battery_ref[0],
            "packet_loss": packet_loss_ref[0],
            "timestamp": time.time()
        }
        payload["signature"] = sign_packet(payload)
        msg = json.dumps(payload).encode()

        for _, port in neighbours:
            try:
                sock.sendto(msg, ("127.0.0.1", port))
            except Exception as e:
                print(f"[Node {node_id}] Heartbeat send error: {e}")

        # Also forward metrics to monitor
        try:
            sock.sendto(msg, ("127.0.0.1", MONITOR_PORT))
        except:
            pass

        seq_ref[0] += 1
        time.sleep(HEARTBEAT_INTERVAL)

# ── Heartbeat Listener ────────────────────────────────────────────────────────
def listen(port, node_id, neighbour_table, seq_table, unreachable_set, failed_nodes):
    # Dedicated receiving socket — separate from sender
    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.bind(("127.0.0.1", port))
    recv_sock.settimeout(2.0)
    while True:
        try:
            data, _ = recv_sock.recvfrom(4096)
            packet = json.loads(data.decode())
            sig = packet.pop("signature")

            if not verify_packet(packet, sig):
                print(f"[Node {node_id}] REJECTED: Invalid signature from node {packet.get('node_id')}")
                continue

            ptype = packet.get("type")

            if ptype == "HEARTBEAT":
                nid = packet["node_id"]
                if packet["seq"] <= seq_table.get(nid, -1):
                    print(f"[Node {node_id}] REJECTED: Replay from node {nid}")
                    continue
                seq_table[nid] = packet["seq"]
                neighbour_table[nid] = {
                    "battery": packet["battery"],
                    "packet_loss": packet["packet_loss"],
                    "last_seen": time.time()
                }
                if nid in unreachable_set:
                    unreachable_set.discard(nid)
                    seq_table.pop(nid, None)  # reset seq so restarted node is accepted
                    failed_nodes.discard(nid)
                    # Recalculate route without the recovered node in failed list
                    new_topology = copy.deepcopy(TOPOLOGY)
                    new_hop = get_next_hop(new_topology, node_id, failed_nodes=failed_nodes)
                    print(f"[Node {node_id}] Node {nid} recovered — route restored: next hop Node {new_hop}")
                else:
                    unreachable_set.discard(nid)

            elif ptype == "RERR":
                failed = packet.get("failed_node")
                print(f"[Node {node_id}] RERR received — Node {failed} reported unreachable")
                unreachable_set.add(failed)
                failed_nodes.add(failed)

                # Recalculate route
                new_topology = copy.deepcopy(TOPOLOGY)
                new_hop = get_next_hop(new_topology, node_id, failed_nodes=failed_nodes)
                print(f"[Node {node_id}] Route updated — new next hop to sink: Node {new_hop}")

        except socket.timeout:
            pass  # Normal — just means no packet arrived in this window
        except OSError as e:
            if hasattr(e, 'winerror') and e.winerror == 10054:
                pass
            else:
                print(f"[Node {node_id}] Listen error: {e}")
        except Exception as e:
            print(f"[Node {node_id}] Listen error: {e}")

# ── Fault Detector ────────────────────────────────────────────────────────────
def detect_faults(sock, node_id, neighbours, neighbour_table, unreachable_set, failed_nodes):
    while True:
        time.sleep(FAULT_CHECK_INTERVAL)
        for nid, info in list(neighbour_table.items()):
            if time.time() - info["last_seen"] > UNREACHABLE_TIMEOUT:
                if nid not in unreachable_set:
                    print(f"[Node {node_id}] Node {nid} UNREACHABLE — broadcasting RERR")
                    unreachable_set.add(nid)
                    broadcast_rerr(sock, node_id, nid, neighbours)
                    failed_nodes.add(nid)
                    new_topology = copy.deepcopy(TOPOLOGY)
                    new_hop = get_next_hop(new_topology, node_id, failed_nodes=failed_nodes)
                    print(f"[Node {node_id}] Rerouted — new next hop to sink: Node {new_hop}")

# ── Simulate Degradation (for training data generation) ───────────────────────
def degrade(battery_ref, packet_loss_ref, node_id, degrade_flag):
    """Call this to simulate a node slowly dying — used to generate pre_failure data"""
    while degrade_flag[0]:
        battery_ref[0] = max(0, battery_ref[0] - 1)
        packet_loss_ref[0] = min(100, packet_loss_ref[0] + 2)
        print(f"[Node {node_id}] Degrading — Battery: {battery_ref[0]}% Loss: {packet_loss_ref[0]}%")
        time.sleep(1)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", type=int, required=True, help="Node ID (1-8)")
    parser.add_argument("--degrade", action="store_true", help="Simulate node degradation")
    args = parser.parse_args()

    node_id = args.id
    port = 5000 + node_id
    neighbours = NEIGHBOUR_PORTS.get(node_id, [])

    # Shared mutable state (using lists so threads can modify them)
    battery_ref = [100]
    packet_loss_ref = [random.randint(0, 5)]   # small random initial loss
    seq_ref = [0]
    neighbour_table = {}
    seq_table = {}
    unreachable_set = set()
    failed_nodes = set()

    current_topology = copy.deepcopy(TOPOLOGY)
    next_hop = get_next_hop(current_topology, node_id)
    print(f"[Node {node_id}] Initial next hop to sink: Node {next_hop}")

    # Single shared socket for sending and receiving
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)

    print(f"[Node {node_id}] Started on port {port} | Neighbours: {[n[0] for n in neighbours]}")

    # Start threads
    threads = [
        threading.Thread(target=send_heartbeats,
                         args=(sock, node_id, neighbours, battery_ref, packet_loss_ref, seq_ref),
                         daemon=True),
        threading.Thread(target=listen,
                         args=(port, node_id, neighbour_table, seq_table, unreachable_set, failed_nodes),
                         daemon=True),
        threading.Thread(target=detect_faults,
                         args=(sock, node_id, neighbours, neighbour_table, unreachable_set, failed_nodes),
                         daemon=True),
    ]

    if args.degrade:
        degrade_flag = [True]
        threads.append(threading.Thread(target=degrade,
                                        args=(battery_ref, packet_loss_ref, node_id, degrade_flag),
                                        daemon=True))

    for t in threads:
        t.start()

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"[Node {node_id}] Shutting down")

if __name__ == "__main__":
    main()