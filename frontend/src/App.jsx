import { useEffect, useMemo, useState } from 'react';
import { io } from 'socket.io-client';

const SOCKET_URL = 'http://localhost:5000';

const formatStatus = (status) => {
  const map = {
    healthy: 'Healthy',
    at_risk: 'At Risk',
    failed: 'Failed',
    sink: 'Sink',
    unknown: 'Unknown',
  };
  return map[status] || status || 'Unknown';
};

function App() {
  const [connected, setConnected] = useState(false);
  const [nodes, setNodes] = useState({});
  const [stats, setStats] = useState({ packets_sent: 0, packets_dropped: 0, security_events: 0, rerr_count: 0, active_routes: 0 });
  const [protoFeed, setProtoFeed] = useState([]);
  const [secFeed, setSecFeed] = useState([]);
  const [pdrData, setPdrData] = useState([]);
  const [uptime, setUptime] = useState(0);
  const [topology, setTopology] = useState(null);

  useEffect(() => {
    const socket = io(SOCKET_URL, { transports: ['websocket'], autoConnect: true });

    socket.on('connect', () => {
      setConnected(true);
    });

    socket.on('disconnect', () => {
      setConnected(false);
    });

    socket.on('init_state', (payload) => {
      setNodes(payload.nodes || {});
      setStats(payload.stats || {});
      setProtoFeed((payload.proto_feed || []).slice(0, 50).reverse());
      setSecFeed((payload.sec_feed || []).slice(0, 50).reverse());
      setPdrData(payload.pdr_history || []);
      setTopology(payload.topology || null);
    });

    socket.on('node_update', (payload) => {
      setNodes((prev) => ({ ...prev, [payload.node_id]: payload }));
    });

    socket.on('stats_update', (payload) => {
      setStats(payload);
    });

    socket.on('proto_event', (payload) => {
      setProtoFeed((prev) => [payload, ...prev].slice(0, 50));
    });

    socket.on('security_event', (payload) => {
      setSecFeed((prev) => [payload, ...prev].slice(0, 30));
    });

    socket.on('pdr_update', (point) => {
      setPdrData((prev) => [...prev, point].slice(-60));
    });

    socket.on('topology_changed', (payload) => {
      setTopology(payload);
    });

    socket.on('uptime', (payload) => {
      setUptime(payload.seconds || 0);
    });

    return () => {
      socket.disconnect();
    };
  }, []);

  const nodesList = useMemo(() => Object.entries(nodes).sort((a, b) => a[0] - b[0]), [nodes]);

  const pdrHistory = useMemo(() => pdrData.slice(-20), [pdrData]);

  const pdrValue = stats.packets_sent
    ? Math.max(0, ((1 - stats.packets_dropped / Math.max(stats.packets_sent, 1)) * 100)).toFixed(1)
    : '100';

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <h1>SentinelMesh Live</h1>
          <p>Real-time WSN routing, ML risk and security monitoring</p>
        </div>
        <div className={`status-badge ${connected ? 'connected' : 'disconnected'}`}>
          {connected ? 'Connected' : 'Disconnected'}
        </div>
      </header>

      <section className="summary-grid">
        <Card label="Packets Sent" value={stats.packets_sent || 0} />
        <Card label="Dropped" value={stats.packets_dropped || 0} color="danger" />
        <Card label="PDR" value={`${pdrValue}%`} />
        <Card label="Uptime" value={formatTime(uptime)} />
        <Card label="Security Events" value={stats.security_events || 0} color="danger" />
        <Card label="RERR" value={stats.rerr_count || 0} color="warn" />
        <Card label="Active Routes" value={stats.active_routes || 0} color="accent" />
      </section>

      <div className="main-grid">
        <div className="panel">
          <h2>Node Status</h2>
          <div className="node-grid">
            {nodesList.map(([id, node]) => (
              <div key={id} className={`node-card ${node.status || 'unknown'}`}>
                <div className="node-title">Node {id}</div>
                <div>Status: {formatStatus(node.status)}</div>
                <div>Battery: {node.battery ?? '—'}%</div>
                <div>Loss: {node.loss ?? '—'}%</div>
                <div>Risk: {(node.prob ?? 0).toFixed(2)}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          <h2>Route + Delivery Feed</h2>
          <div className="feed-list">
            {(protoFeed || []).map((item, index) => (
              <div key={index} className={`feed-item ${item.type.toLowerCase()}`}>
                <div className="feed-header">
                  <span>{item.ts}</span>
                  <span>{item.type}</span>
                </div>
                <div>{item.detail}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="bottom-grid">
        <div className="panel chart-panel">
          <h2>PDR Trend</h2>
          <div className="chart-strip">
            {pdrHistory.map((point, index) => (
              <div key={index} className="chart-bar" style={{ height: `${Math.max(6, point.pdr)}%` }} title={`${point.t}: ${point.pdr}%`} />
            ))}
          </div>
          <div className="chart-legend">Latest PDR points ({pdrHistory.length})</div>
        </div>

        <div className="panel">
          <h2>Security Feed</h2>
          <div className="feed-list">
            {secFeed.map((item, index) => (
              <div key={index} className="feed-item security">
                <div className="feed-header">
                  <span>{item.ts}</span>
                  <span>{item.type}</span>
                </div>
                <div>{item.detail}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Card({ label, value, color }) {
  return (
    <div className={`stat-card ${color || ''}`}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function formatTime(seconds) {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

export default App;
