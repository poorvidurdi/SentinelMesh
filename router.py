import json
import os

CONFIG_FILE = "config.json"

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def build_topology(config):
    """Build topology dict {node: {neighbour: cost}} from config edges."""
    nodes = config["nodes"]
    topology = {n: {} for n in nodes}
    for a, b, cost in config["edges"]:
        topology[a][b] = cost
        topology[b][a] = cost  # undirected
    return topology

def bellman_ford(topology, source):
    dist = {node: float('inf') for node in topology}
    prev = {node: None for node in topology}
    dist[source] = 0

    for _ in range(len(topology) - 1):
        for u in topology:
            for v, weight in topology[u].items():
                if dist[u] + weight < dist[v]:
                    dist[v] = dist[u] + weight
                    prev[v] = u

    next_hop = {}
    for dest in topology:
        if dest == source:
            continue
        node = dest
        while prev[node] != source and prev[node] is not None:
            node = prev[node]
        next_hop[dest] = node if prev[node] == source else None

    return next_hop

def remove_node(topology, failed_node):
    return {
        node: {n: c for n, c in neighbours.items() if n != failed_node}
        for node, neighbours in topology.items()
        if node != failed_node
    }

def get_next_hop(topology, source, failed_nodes=None, sink=None):
    config = load_config()
    if sink is None:
        sink = config["sink"]

    if failed_nodes:
        for node in failed_nodes:
            topology = remove_node(topology, node)

    next_hop = bellman_ford(topology, source)
    hop = next_hop.get(sink)

    if hop:
        print(f"[Router] Node {source} → next hop to sink: Node {hop}")
    else:
        print(f"[Router] Node {source} → no path to sink (all routes blocked)")

    return hop

# Exported for use in node.py
def get_topology():
    config = load_config()
    return build_topology(config)

def get_sink():
    return load_config()["sink"]

def get_neighbour_ports(node_id):
    config = load_config()
    return config["neighbour_ports"].get(str(node_id), [])


def get_node_port(node_id):
    return 5000 + int(node_id)


def get_all_node_ports():
    return [get_node_port(int(n)) for n in load_config()["nodes"]]


def get_edge_cost(topology, src, dst):
    return topology.get(src, {}).get(dst, float('inf'))