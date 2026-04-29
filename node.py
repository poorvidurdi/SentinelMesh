import argparse
import hashlib
import hmac
import json
import random
import socket
import threading
import time

from router import get_edge_cost, get_neighbour_ports, get_sink, get_topology, get_next_hop

# ── Config ────────────────────────────────────────────────────────────────────
SHARED_KEY = b"wsn_secret_key"
MONITOR_PORT = 9999
DASHBOARD_PORT = 9998
HEARTBEAT_INTERVAL = 2
ROUTE_INTERVAL = 3
DATA_INTERVAL = 5
FAULT_CHECK_INTERVAL = 6
UNREACHABLE_TIMEOUT = 8
RISK_PENALTY_ALPHA = 10.0
MAX_ROUTE_DISTANCE = 9999

# ── HMAC Signing ──────────────────────────────────────────────────────────────
def sign_packet(data: dict) -> str:
    msg = json.dumps(data, sort_keys=True).encode()
    return hmac.new(SHARED_KEY, msg, hashlib.sha256).hexdigest()

def verify_packet(data: dict, signature: str) -> bool:
    return hmac.compare_digest(sign_packet(data), signature)


def make_signed_packet(payload: dict) -> bytes:
    packet = payload.copy()
    packet["timestamp"] = time.time()
    packet["signature"] = sign_packet(packet)
    return json.dumps(packet).encode()


def send_udp(sock, port, payload: dict):
    sock.sendto(make_signed_packet(payload), ("127.0.0.1", port))


def send_to_dashboard(payload: dict, node_id: int):
    payload = payload.copy()
    payload["from_node"] = node_id
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(json.dumps(payload).encode(), ("127.0.0.1", DASHBOARD_PORT))
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


def get_risk_penalty(prob):
    return prob * RISK_PENALTY_ALPHA if prob is not None else 0.0


def status_label(battery, packet_loss):
    if battery <= 0:
        return "failed"
    if battery < 20 or packet_loss > 40:
        return "at_risk"
    if battery < 40 or packet_loss > 25:
        return "degraded"
    return "healthy"


def compute_and_push_route(node_id, topology, neighbour_routes, failed_nodes, risk_table, state):
    if node_id == get_sink():
        state["next_hop"] = None
        state["route_cost"] = 0
        state["status"] = "sink"
        send_to_dashboard({"type": "ROUTE_UPDATE", "node_id": node_id, "next_hop": None, "cost": 0, "status": state["status"]}, node_id)
        return None

    best_hop = None
    best_cost = float("inf")
    for neighbour, neighbour_cost in neighbour_routes.items():
        if neighbour in failed_nodes or neighbour == node_id:
            continue
        link_cost = get_edge_cost(topology, node_id, neighbour)
        if link_cost == float("inf"):
            continue
        penalty = get_risk_penalty(risk_table.get(neighbour, 0.0))
        total = link_cost + neighbour_cost + penalty
        if total < best_cost:
            best_cost = total
            best_hop = neighbour

    state["next_hop"] = best_hop
    state["route_cost"] = best_cost if best_hop is not None else MAX_ROUTE_DISTANCE
    state["status"] = state.get("status", status_label(100, 0))
    send_to_dashboard({
        "type": "ROUTE_UPDATE",
        "node_id": node_id,
        "next_hop": best_hop,
        "cost": state["route_cost"],
        "status": state["status"]
    }, node_id)
    return best_hop


def forward_data_packet(sock, node_id, packet, state, edge_costs=None):
    next_hop = state.get("next_hop")
    if next_hop is None:
        print(f"[Node {node_id}] No route available, dropping DATA")
        send_to_dashboard({"type": "DATA_DROP", "origin": packet.get("origin"), "node": node_id, "reason": "no_route"}, node_id)
        return

    packet = packet.copy()
    packet["hops"] = packet.get("hops", []) + [node_id]
    packet["from"] = node_id
    packet["seq"] = int(time.time() * 1000)
    send_udp(sock, 5000 + next_hop, packet)
    print(f"[Node {node_id}] Forwarded DATA to Node {next_hop}")


def send_route_updates(sock, node_id, neighbours, seq_ref, topology, neighbour_routes, failed_nodes, risk_table, state):
    while True:
        compute_and_push_route(node_id, topology, neighbour_routes, failed_nodes, risk_table, state)
        payload = {
            "type": "ROUTE",
            "node_id": node_id,
            "distance": state.get("route_cost", MAX_ROUTE_DISTANCE),
            "status": state.get("status", "healthy"),
            "seq": seq_ref[0]
        }
        for _, port in neighbours:
            try:
                send_udp(sock, port, payload)
            except Exception as e:
                print(f"[Node {node_id}] Route send error: {e}")
        seq_ref[0] += 1
        time.sleep(ROUTE_INTERVAL)


def broadcast_rerr(sock, node_id, failed_node_id, neighbours):
    payload = {
        "type": "RERR",
        "from": node_id,
        "failed_node": failed_node_id,
        "seq": int(time.time() * 1000)
    }
    for _, port in neighbours:
        try:
            send_udp(sock, port, payload)
        except Exception as e:
            print(f"[Node {node_id}] RERR send error: {e}")

# ── Heartbeat Sender ──────────────────────────────────────────────────────────
def send_heartbeats(sock, node_id, neighbours, battery_ref, packet_loss_ref, seq_ref):
    while True:
        status = status_label(battery_ref[0], packet_loss_ref[0])
        if battery_ref[0] <= 0:
            time.sleep(HEARTBEAT_INTERVAL)
            continue
        payload = {
            "type": "HEARTBEAT",
            "node_id": node_id,
            "seq": seq_ref[0],
            "battery": battery_ref[0],
            "packet_loss": packet_loss_ref[0],
            "status": status
        }
        for _, port in neighbours:
            try:
                send_udp(sock, port, payload)
            except Exception as e:
                print(f"[Node {node_id}] Heartbeat send error: {e}")

        try:
            send_udp(sock, MONITOR_PORT, payload)
        except Exception:
            pass

        seq_ref[0] += 1
        time.sleep(HEARTBEAT_INTERVAL)

# ── Heartbeat Listener ────────────────────────────────────────────────────────
def listen(port, node_id, topology, neighbours, edge_costs, neighbour_routes, seq_table, neighbour_table, unreachable_set, failed_nodes, risk_table, state, battery_ref, packet_loss_ref):
    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.bind(("127.0.0.1", port))
    recv_sock.settimeout(2.0)
    while True:
        try:
            data, _ = recv_sock.recvfrom(4096)
            packet = json.loads(data.decode())
            signature = packet.pop("signature", None)
            ptype = packet.get("type")
            src_id = packet.get("node_id") or packet.get("from") or packet.get("from_node")

            if signature is None or not verify_packet(packet, signature):
                print(f"[Node {node_id}] REJECTED: Invalid signature from node {src_id}")
                send_to_dashboard({"type": "HMAC_REJECT", "detail": f"Invalid auth from node {src_id}"}, node_id)
                continue

            if packet.get("seq") is not None:
                key = (src_id, ptype)
                if packet["seq"] <= seq_table.get(key, -1):
                    print(f"[Node {node_id}] REJECTED: Replay from node {src_id} type {ptype}")
                    send_to_dashboard({"type": "REPLAY", "detail": f"Replay from node {src_id}"}, node_id)
                    continue
                seq_table[key] = packet["seq"]

            if ptype == "HEARTBEAT":
                nid = packet["node_id"]
                neighbour_table[nid] = {
                    "battery": packet["battery"],
                    "packet_loss": packet["packet_loss"],
                    "last_seen": time.time()
                }
                if packet.get("status") == "failed":
                    failed_nodes.add(nid)
                if nid in unreachable_set:
                    unreachable_set.discard(nid)
                    failed_nodes.discard(nid)
                    print(f"[Node {node_id}] Node {nid} recovered")

            elif ptype == "ROUTE":
                nid = packet["node_id"]
                neighbour_routes[nid] = packet.get("distance")
                if packet.get("status") == "failed":
                    failed_nodes.add(nid)
                compute_and_push_route(node_id, topology, neighbour_routes, failed_nodes, risk_table, state)

            elif ptype == "RERR":
                failed = packet.get("failed_node")
                print(f"[Node {node_id}] RERR received — Node {failed} unreachable")
                failed_nodes.add(failed)
                compute_and_push_route(node_id, topology, neighbour_routes, failed_nodes, risk_table, state)

            elif ptype == "RISK_UPDATE":
                target = packet.get("node_id")
                prob = packet.get("risk", 0.0)
                risk_table[target] = prob
                if target == node_id:
                    state["risk"] = prob
                    state["status"] = "at_risk" if prob >= 0.75 else status_label(battery_ref[0], packet_loss_ref[0])
                print(f"[Node {node_id}] Risk update for node {target}: {prob:.2f}")
                compute_and_push_route(node_id, topology, neighbour_routes, failed_nodes, risk_table, state)

            elif ptype == "DATA":
                origin = packet.get("origin")
                if node_id == get_sink():
                    print(f"[Node {node_id}] Delivered DATA from Node {origin} hops={packet.get('hops')}")
                    send_to_dashboard({"type": "DATA_DELIVERED", "node_id": origin, "hops": packet.get("hops", [])}, node_id)
                else:
                    forward_data_packet(recv_sock, node_id, packet, state, edge_costs)

            elif ptype == "SIM_FAULT":
                action = packet.get("action")
                battery_ref[0] = packet.get("battery", battery_ref[0])
                packet_loss_ref[0] = packet.get("packet_loss", packet_loss_ref[0])
                state["status"] = "failed" if action == "failed" else "at_risk" if action == "at_risk" else "healthy"
                print(f"[Node {node_id}] SIM_FAULT {action} battery={battery_ref[0]} loss={packet_loss_ref[0]}")
                if action == "failed":
                    failed_nodes.add(node_id)
                    broadcast_rerr(recv_sock, node_id, node_id, neighbours)
                compute_and_push_route(node_id, topology, neighbour_routes, failed_nodes, risk_table, state)

        except socket.timeout:
            pass
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
                    new_topology = get_topology()
                    new_hop = get_next_hop(new_topology, node_id, failed_nodes=failed_nodes, sink=get_sink())
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
    config_ports = get_neighbour_ports(node_id)
    neighbours = [(int(p) - 5000, p) for p in config_ports]

    # Shared mutable state (using lists so threads can modify them)
    battery_ref = [100]
    packet_loss_ref = [random.randint(0, 5)]   # small random initial loss
    seq_ref = [0]
    neighbour_table = {}
    seq_table = {}
    unreachable_set = set()
    failed_nodes = set()

    topology = get_topology()
    next_hop = get_next_hop(topology, node_id, sink=get_sink())
    print(f"[Node {node_id}] Initial next hop to sink: Node {next_hop}")

    risk_table = {}
    neighbour_routes = {}
    state = {"next_hop": next_hop, "route_cost": 0, "risk": 0.0, "status": "healthy"}

    # Single shared socket for sending and receiving
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)

    print(f"[Node {node_id}] Started on port {port} | Neighbours: {[n[0] for n in neighbours]}")

    # Start threads
    threads = [
        threading.Thread(target=send_heartbeats,
                         args=(sock, node_id, neighbours, battery_ref, packet_loss_ref, seq_ref),
                         daemon=True),
        threading.Thread(target=send_route_updates,
                         args=(sock, node_id, neighbours, seq_ref, topology, neighbour_routes, failed_nodes, risk_table, state),
                         daemon=True),
        threading.Thread(target=listen,
                         args=(port, node_id, topology, neighbours, None, neighbour_routes, seq_table, neighbour_table, unreachable_set, failed_nodes, risk_table, state, battery_ref, packet_loss_ref),
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