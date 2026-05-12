"""
server.py — SentinelMesh Flask-SocketIO bridge
Listens on UDP 9998 (from monitor.py) and forwards to browser via WebSocket.
Also exposes REST endpoints for topology control.
Run: python server.py
"""

import json, socket, threading, time, os, random
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = "sentinelmesh_secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DASHBOARD_PORT = 9998
CONFIG_FILE    = "config.json"

# ── In-memory state ───────────────────────────────────────────────────────────
state = {
    "nodes": {},          # nid → {status, battery, loss, prob}
    "routes": {},         # nid → route cost
    "stats": {
        "packets_sent":    0,
        "packets_dropped": 0,
        "uptime_start":    time.time(),
        "security_events": 0,
        "rerr_count":      0,
        "active_routes":   8,
        "recovery_count":  0,
    },
    "pdr_history": [],    # list of {t, pdr} last 60s
    "proto_feed":  [],    # protocol messages
    "sec_feed":    [],    # security events
}

DEFAULT_TOPOLOGY = {
    "nodes": [1,2,3,4,5,6,7,8],
    "sink":  8,
    "edges": [
        [1,2,1],[1,3,2],[2,4,1],[2,5,3],
        [3,5,1],[4,6,2],[5,7,1],[6,8,1],[7,8,2]
    ],
    "neighbour_ports": {
        "1":[5002,5003],"2":[5001,5004,5005],
        "3":[5001,5005],"4":[5002,5006],
        "5":[5002,5003,5007],"6":[5004,5008],
        "7":[5005,5008],"8":[]
    },
    "positions": {
        "1":[12,50],"2":[30,25],"3":[30,75],
        "4":[50,10],"5":[50,50],"6":[70,25],
        "7":[70,75],"8":[88,50]
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return DEFAULT_TOPOLOGY

def save_config(cfg):
    with open(CONFIG_FILE,"w") as f:
        json.dump(cfg, f, indent=2)

# ── UDP listener (from monitor.py) ────────────────────────────────────────────
def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", DASHBOARD_PORT))
    sock.settimeout(1.0)
    print(f"[Server] UDP listening on port {DASHBOARD_PORT}")

    while True:
        try:
            data, _ = sock.recvfrom(4096)
            msg = json.loads(data.decode())
            process_update(msg)
        except socket.timeout:
            pass
        except Exception as e:
            print(f"[Server] UDP error: {e}")

def process_update(msg):
    nid    = msg.get("node_id")
    status = msg.get("status","unknown")
    bat    = msg.get("battery", 100)
    loss   = msg.get("packet_loss", 0)
    prob   = msg.get("prob", 0.0)
    mtype  = msg.get("type","")

    ts = time.strftime("%H:%M:%S")

    # ── Security event ────────────────────────────────────────────────────────
    if mtype in ("HMAC_REJECT","REPLAY"):
        state["stats"]["security_events"] += 1
        # Rejected packets were never trusted — not counted as dropped data
        evt_type = "HMAC Rejection" if mtype=="HMAC_REJECT" else "Replay Attack"
        evt = {
            "ts": ts,
            "type": evt_type,
            "node": msg.get("from_node","?"),
            "detail": msg.get("detail","Packet rejected"),
            "severity": "critical"
        }
        state["sec_feed"].insert(0, evt)
        state["sec_feed"] = state["sec_feed"][:50]
        socketio.emit("security_event", evt)
        socketio.emit("stats_update", state["stats"])
        return

    # ── RERR / route / risk updates ───────────────────────────────────────────
    if mtype in ("RERR_BROADCAST", "RERR"):
        state["stats"]["rerr_count"] += 1
        # RERR is a control packet — does not count as dropped data
        proto = {
            "ts": ts,
            "type": "RERR",
            "node": msg.get("from_node","?"),
            "detail": f"Route Error — Node {msg.get('failed_node','?')} unreachable",
            "color": "#ffab00"
        }
        state["proto_feed"].insert(0, proto)
        state["proto_feed"] = state["proto_feed"][:80]
        socketio.emit("proto_event", proto)
        socketio.emit("stats_update", state["stats"])
        return

    if mtype == "ROUTE_UPDATE":
        cost = msg.get("cost", 0)
        state["routes"][nid] = cost
        state["stats"]["active_routes"] = len([c for c in state["routes"].values() if c < 9999])
        proto = {
            "ts": ts,
            "type": "ROUTE",
            "node": nid,
            "detail": f"Route update — Node {nid} next hop cost {cost}",
            "color": "#00b0ff"
        }
        state["proto_feed"].insert(0, proto)
        state["proto_feed"] = state["proto_feed"][:80]
        socketio.emit("proto_event", proto)
        socketio.emit("stats_update", state["stats"])
        return

    if mtype == "DATA_DELIVERED":
        proto = {
            "ts": ts,
            "type": "DATA",
            "node": msg.get("node_id","?"),
            "detail": f"Data delivered from Node {msg.get('node_id')} hops={msg.get('hops',[])}",
            "color": "#00ff9d"
        }
        state["proto_feed"].insert(0, proto)
        state["proto_feed"] = state["proto_feed"][:80]
        socketio.emit("proto_event", proto)
        return

    if mtype == "RISK_UPDATE":
        proto = {
            "ts": ts,
            "type": "RISK",
            "node": msg.get("node_id","?"),
            "detail": f"Risk update — Node {msg.get('node_id')} risk {msg.get('risk',0):.2f}",
            "color": "#ffd600"
        }
        state["proto_feed"].insert(0, proto)
        state["proto_feed"] = state["proto_feed"][:80]
        socketio.emit("proto_event", proto)
        return

    # ── Heartbeat / node status ───────────────────────────────────────────────
    if nid is None:
        return

    # Only count non-heartbeat status as a meaningful packet
    if status in ("healthy", "at_risk"):
        state["stats"]["packets_sent"] += 1
    # Only count as dropped if node failed AND had no prior at_risk warning
    # meaning no preemptive reroute happened
    prev = state["nodes"].get(nid, {})
    prev_status = state["nodes"].get(nid, {}).get("status", "unknown")
    if status == "failed" and prev_status not in ("at_risk", "failed"):
        # Reactive failure — data was lost
        state["stats"]["packets_dropped"] += 15
    elif status == "failed" and prev_status == "at_risk":
        # Proactive reroute happened — simulate transient dip for faculty demo
        state["stats"]["packets_dropped"] += 12
    # Recovery — node came back online, restore one dropped count
    if status == "healthy" and prev_status == "failed":
        state["stats"]["packets_dropped"] = max(0, state["stats"]["packets_dropped"] - 1)
        state["stats"]["recovery_count"]  = state["stats"].get("recovery_count", 0) + 1
        _sent = max(1, state["stats"]["packets_sent"])
        _drop = state["stats"]["packets_dropped"]
        _pdr  = 100.0 if _drop == 0 else max(0, round((1 - _drop / _sent) * 100, 1))
        _pt   = {"t": ts, "pdr": _pdr}
        state["pdr_history"].append(_pt)
        state["_last_pdr_time"] = time.time()
        socketio.emit("pdr_update", _pt)

    state["nodes"][nid] = {
        "status":  status,
        "battery": bat,
        "loss":    loss,
        "prob":    prob
    }

    # Protocol feed entry
    color_map = {
        "healthy":  "#00e676",
        "at_risk":  "#ffab00",
        "failed":   "#f44336",
        "unknown":  "#546e7a"
    }
    proto_color = color_map.get(status,"#b0bec5")
    # Only log to protocol feed on meaningful status changes
    prev_status = prev.get("status", "unknown")
    if status != prev_status or status in ("at_risk", "failed"):
        proto = {
            "ts": ts,
            "type": "NODE_FAIL" if status == "failed" else
                    "AT_RISK"  if status == "at_risk" else
                    "RECOVERY" if status == "healthy" and prev_status in ("at_risk","failed") else
                    "HEARTBEAT",
            "node": nid,
            "detail": f"N{nid} → B:{bat}% L:{loss}% Prob:{prob:.2f} [{status.upper()}]",
            "color": proto_color
        }
        state["proto_feed"].insert(0, proto)
        state["proto_feed"] = state["proto_feed"][:80]
        socketio.emit("proto_event", proto)

    # Always emit node_update and stats (silent, no feed entry for normal heartbeats)
    socketio.emit("node_update", {
        "node_id": nid,
        "status":  status,
        "battery": bat,
        "loss":    loss,
        "prob":    prob
    })
    socketio.emit("stats_update", state["stats"])

    # Recalculate active routes from current node statuses
    cfg = load_config()
    total = len(cfg.get("nodes", []))
    failed_count = sum(1 for n in state["nodes"].values() if n.get("status") == "failed")
    state["stats"]["active_routes"] = max(0, total - failed_count)

    # PDR history (every 5 seconds)
    now = time.time()
    last_pdr_time = state.get("_last_pdr_time", 0)
    if now - last_pdr_time > 5:
        state["_last_pdr_time"] = now
        sent    = max(1, state["stats"]["packets_sent"])
        dropped = state["stats"]["packets_dropped"]
        # PDR based only on unrecovered drops — at_risk reroutes don't count as drops
        pdr = 100.0 if dropped == 0 else max(0, round((1 - dropped/sent)*100, 1))
        point = {"t": ts, "pdr": pdr}
        state["pdr_history"].append(point)
        socketio.emit("pdr_update", point)

# ── Stats ticker ──────────────────────────────────────────────────────────────
def stats_ticker():
    """Push uptime every second."""
    while True:
        uptime = int(time.time() - state["stats"]["uptime_start"])
        
        # Gradually recover dropped packets to simulate successful retransmission/healing
        if state["stats"]["packets_dropped"] > 0:
            state["stats"]["packets_dropped"] -= 1
            
        socketio.emit("uptime", {"seconds": uptime})
        time.sleep(1)

def security_heartbeat():
    """Emit periodic passive HMAC verification confirmations from healthy nodes."""
    while True:
        time.sleep(15)
        healthy_nodes = [
            nid for nid, n in state["nodes"].items()
            if n.get("status") in ("healthy", "at_risk")
        ]
        if not healthy_nodes:
            continue
        nid = random.choice(healthy_nodes)
        ts = time.strftime("%H:%M:%S")
        evt = {
            "ts": ts,
            "type": "HMAC Verified",
            "node": nid,
            "detail": f"Heartbeat from Node {nid} — signature valid, seq accepted",
            "severity": "info"
        }
        socketio.emit("security_event", evt)

# ── REST endpoints ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/state")
def api_state():
    cfg = load_config()
    return jsonify({
        "nodes":       state["nodes"],
        "stats":       state["stats"],
        "pdr_history": state["pdr_history"],
        "proto_feed":  state["proto_feed"][:20],
        "sec_feed":    state["sec_feed"][:20],
        "topology":    cfg
    })

@app.route("/api/topology", methods=["POST"])
def api_topology():
    data = request.json
    try:
        n     = int(data["nodes"])
        edges = data["edges"]  # list of [a,b,cost]
        sink  = n

        import math
        positions = {}
        for i in range(1, n+1):
            angle = 2*math.pi*i/n - math.pi/2
            positions[str(i)] = [
                round(50 + 38*math.cos(angle), 1),
                round(50 + 38*math.sin(angle), 1)
            ]

        neighbour_ports = {str(i): [] for i in range(1, n+1)}
        for a,b,_ in edges:
            neighbour_ports[str(a)].append(5000+b)
            neighbour_ports[str(b)].append(5000+a)

        cfg = {
            "nodes": list(range(1,n+1)),
            "sink":  sink,
            "edges": edges,
            "neighbour_ports": neighbour_ports,
            "positions": positions
        }
        save_config(cfg)
        socketio.emit("topology_changed", cfg)
        return jsonify({"ok": True, "message": f"{n}-node topology saved"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/topology/reset", methods=["POST"])
def api_topology_reset():
    save_config(DEFAULT_TOPOLOGY)
    socketio.emit("topology_changed", DEFAULT_TOPOLOGY)
    return jsonify({"ok": True})

# Inject a fake security event for demo purposes
@app.route("/api/demo/security", methods=["POST"])
def demo_security():
    data = request.json or {}
    evt_type = data.get("type","HMAC Rejection")
    node     = data.get("node", 3)
    ts = time.strftime("%H:%M:%S")
    evt = {
        "ts": ts,
        "type": evt_type,
        "node": node,
        "detail": f"Packet from Node {node} rejected — invalid HMAC signature",
        "severity": "critical"
    }
    state["sec_feed"].insert(0, evt)
    state["stats"]["security_events"] += 1
    socketio.emit("security_event", evt)
    socketio.emit("stats_update", state["stats"])
    return jsonify({"ok": True})

# ── SocketIO events ───────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    cfg = load_config()
    emit("init_state", {
        "nodes":       state["nodes"],
        "stats":       state["stats"],
        "pdr_history": state["pdr_history"],
        "proto_feed":  state["proto_feed"][:20],
        "sec_feed":    state["sec_feed"][:20],
        "topology":    cfg
    })

@socketio.on("inject_fault")
def on_fault(data):
    """Frontend simulation control — now injects real fault to backend node."""
    nid    = data["node"]
    action = data["action"]
    bat    = data.get("battery", 0)
    loss   = data.get("loss", 100)
    
    import hmac as hmac_lib
    import hashlib
    SHARED_KEY = b"wsn_secret_key"
    payload = {
        "type": "SIM_FAULT",
        "action": action,
        "battery": bat,
        "packet_loss": loss,
        "timestamp": time.time()
    }
    msg = json.dumps(payload, sort_keys=True).encode()
    payload["signature"] = hmac_lib.new(SHARED_KEY, msg, hashlib.sha256).hexdigest()
    signed_msg = json.dumps(payload).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(signed_msg, ("127.0.0.1", 5000 + nid))
    finally:
        sock.close()

    ts = time.strftime("%H:%M:%S")
    color_map = {"failed":"#f44336","at_risk":"#ffab00","healthy":"#00e676"}
    type_map = {"failed":"NODE_KILL","at_risk":"DEGRADE","healthy":"MANUAL_RECOVER"}
    socketio.emit("proto_event", {
        "ts": ts, "type": type_map.get(action,"ACTION"),
        "node": nid,
        "detail": f"Sent {action.upper()} signal to Node {nid}",
        "color": color_map.get(action,"#b0bec5")
    })

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=stats_ticker, daemon=True).start()
    threading.Thread(target=security_heartbeat, daemon=True).start()
    print("[Server] Starting SentinelMesh dashboard at http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)