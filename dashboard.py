import matplotlib
matplotlib.use('TkAgg')

import matplotlib.pyplot as plt
import matplotlib.animation as animation
import matplotlib.patches as mpatches
import networkx as nx
import socket
import json
import threading
import time
import sys
sys.stdout.reconfigure(encoding='utf-8')

# ── Network topology for visualization ───────────────────────────────────────
EDGES = [
    (1, 2), (1, 3),
    (2, 4), (2, 5),
    (3, 5),
    (4, 6),
    (5, 7),
    (6, 8),
    (7, 8)
]

# Fixed positions for nodes so layout is stable
NODE_POSITIONS = {
    1: (0, 2),
    2: (1, 3),
    3: (1, 1),
    4: (2, 4),
    5: (2, 2),
    6: (3, 4),
    7: (3, 2),
    8: (4, 3)
}

NODE_LABELS = {
    1: "N1", 2: "N2", 3: "N3",
    4: "N4", 5: "N5", 6: "N6",
    7: "N7", 8: "SINK"
}

# ── Shared state updated by listener thread ───────────────────────────────────
node_status = {}      # node_id → "healthy" / "at_risk" / "failed"
node_metrics = {}     # node_id → {battery, packet_loss, prob}
active_edges = set(EDGES)  # edges currently active
failed_nodes = set()

# ── UDP Listener — receives status updates from monitor ───────────────────────
DASHBOARD_PORT = 9998

def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", DASHBOARD_PORT))
    sock.settimeout(1.0)
    while True:
        try:
            data, _ = sock.recvfrom(4096)
            update = json.loads(data.decode())
            nid = update["node_id"]
            node_status[nid] = update["status"]
            node_metrics[nid] = {
                "battery": update["battery"],
                "packet_loss": update["packet_loss"],
                "prob": update["prob"]
            }
            if update["status"] == "failed":
                failed_nodes.add(nid)
            elif nid in failed_nodes and update["status"] == "healthy":
                failed_nodes.discard(nid)
        except socket.timeout:
            pass
        except Exception as e:
            print(f"[Dashboard] Listener error: {e}")

# ── Build graph ───────────────────────────────────────────────────────────────
def build_graph():
    G = nx.Graph()
    G.add_nodes_from(range(1, 9))
    G.add_edges_from(EDGES)
    return G

# ── Draw function called every animation frame ────────────────────────────────
def draw(frame, G, ax):
    ax.clear()
    ax.set_facecolor("#1a1a2e")
    ax.set_title("SentinelMesh — WSN Live Health Monitor",
                 color="white", fontsize=14, fontweight="bold", pad=15)
    ax.axis("off")

    # Node colors based on status
    node_colors = []
    for node in G.nodes():
        status = node_status.get(node, "unknown")
        if status == "healthy":
            node_colors.append("#00c853")       # green
        elif status == "at_risk":
            node_colors.append("#ffab00")       # amber
        elif status == "failed":
            node_colors.append("#d50000")       # red
        else:
            node_colors.append("#455a64")       # grey — not yet seen

    # Edge colors — red if either endpoint is failed
    edge_colors = []
    for u, v in G.edges():
        if u in failed_nodes or v in failed_nodes:
            edge_colors.append("#d50000")
        else:
            edge_colors.append("#90caf9")

    # Draw edges
    nx.draw_networkx_edges(
        G, NODE_POSITIONS, ax=ax,
        edge_color=edge_colors,
        width=2.0, alpha=0.8
    )

    # Draw nodes
    nx.draw_networkx_nodes(
        G, NODE_POSITIONS, ax=ax,
        node_color=node_colors,
        node_size=1200, alpha=0.95
    )

    # Draw labels
    nx.draw_networkx_labels(
        G, NODE_POSITIONS, ax=ax,
        labels=NODE_LABELS,
        font_color="white",
        font_size=9,
        font_weight="bold"
    )

    # Draw metric annotations below each node
    for node, (x, y) in NODE_POSITIONS.items():
        metrics = node_metrics.get(node)
        if metrics:
            label = f"B:{metrics['battery']}% L:{metrics['packet_loss']}%"
            prob = metrics['prob']
            if prob > 0:
                label += f"\nP:{prob:.2f}"
            ax.text(x, y - 0.35, label,
                    ha='center', va='top',
                    fontsize=6.5, color='white',
                    bbox=dict(boxstyle='round,pad=0.2',
                              facecolor='#263238', alpha=0.7))

    # Legend
    legend_elements = [
        mpatches.Patch(color="#455a64", label="Unknown"),
        mpatches.Patch(color="#00c853", label="Healthy"),
        mpatches.Patch(color="#ffab00", label="At Risk"),
        mpatches.Patch(color="#d50000", label="Failed"),
    ]
    ax.legend(handles=legend_elements, loc="lower left",
              facecolor="#263238", labelcolor="white",
              fontsize=8, framealpha=0.8)

    # Timestamp
    ax.text(0.99, 0.01, f"Updated: {time.strftime('%H:%M:%S')}",
            transform=ax.transAxes,
            ha='right', va='bottom',
            fontsize=7, color='#90caf9')

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    G = build_graph()

    # Start listener thread
    t = threading.Thread(target=listen_for_updates, daemon=True)
    t.start()

    # Set up figure
    fig, ax = plt.subplots(figsize=(12, 7))
    fig.patch.set_facecolor("#1a1a2e")

    ani = animation.FuncAnimation(
        fig, draw,
        fargs=(G, ax),
        interval=2000,    # redraw every 2 seconds
        cache_frame_data=False
    )

    print("[Dashboard] Started — waiting for node data...")
    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    main()