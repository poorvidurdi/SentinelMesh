# router.py

# Network topology: node_id -> {neighbour_id: link_cost}
TOPOLOGY = {
    1: {2: 1, 3: 2},
    2: {1: 1, 4: 1, 5: 3},
    3: {1: 2, 5: 1},
    4: {2: 1, 6: 2},
    5: {2: 3, 3: 1, 7: 1},
    6: {4: 2, 8: 1},
    7: {5: 1, 8: 2},
    8: {}  # sink
}

SINK = 8

def bellman_ford(topology, source):
    """
    Run Bellman-Ford from source to all nodes.
    Returns next_hop dict: {destination: next_hop_node_id}
    """
    dist = {node: float('inf') for node in topology}
    prev = {node: None for node in topology}
    dist[source] = 0

    for _ in range(len(topology) - 1):
        for u in topology:
            for v, weight in topology[u].items():
                if dist[u] + weight < dist[v]:
                    dist[v] = dist[u] + weight
                    prev[v] = u

    # Build next_hop table toward sink
    next_hop = {}
    for dest in topology:
        if dest == source:
            continue
        node = dest
        while prev[node] != source and prev[node] is not None:
            node = prev[node]
        if prev[node] == source:
            next_hop[dest] = node
        else:
            next_hop[dest] = None  # unreachable

    return next_hop


def remove_node(topology, failed_node):
    """
    Return a new topology with failed_node removed.
    Does not modify the original.
    """
    new_topology = {}
    for node, neighbours in topology.items():
        if node == failed_node:
            continue
        new_topology[node] = {
            n: cost for n, cost in neighbours.items()
            if n != failed_node
        }
    return new_topology


def get_next_hop(topology, source, failed_nodes=None):
    """
    Get next hop toward sink from source,
    excluding any failed nodes.
    """
    if failed_nodes:
        for node in failed_nodes:
            topology = remove_node(topology, node)

    next_hop = bellman_ford(topology, source)
    hop = next_hop.get(SINK)

    if hop:
        print(f"[Router] Node {source} → next hop to sink: Node {hop}")
    else:
        print(f"[Router] Node {source} → no path to sink (all routes blocked)")

    return hop