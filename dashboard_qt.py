import sys, json, socket, threading, time, math, random
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QLabel, QComboBox, QTextEdit, QSlider, QLineEdit,
    QFrame, QMenu
)
from PyQt5.QtCore import Qt, QTimer, QPointF, pyqtSignal, QObject, QRectF
from PyQt5.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont,
    QRadialGradient, QLinearGradient, QFontMetrics
)

# ── Constants ──────────────────────────────────────────────────────────────────
DASHBOARD_PORT = 9998
SINK_NODE      = 8
NODE_R         = 40  # Increased base node radius

DEFAULT_EDGES = [
    (1,2),(1,3),(2,4),(2,5),(3,5),
    (4,6),(5,7),(6,8),(7,8)
]

# Base positions in 1000×700 space — scaled at paint time
BASE_POS = {
    1:(100,350), 2:(270,195), 3:(270,505),
    4:(460, 90), 5:(460,350), 6:(650,195),
    7:(650,505), 8:(840,350),
}

STATUS_COLORS = {
    "unknown": QColor("#37474f"),
    "healthy":  QColor("#00e676"),
    "at_risk":  QColor("#ffab00"),
    "failed":   QColor("#f44336"),
}

BG_COLOR    = QColor("#06131f")
PANEL_COLOR = QColor("#0d1b2b")
CARD_COLOR  = QColor("#122540")
ACCENT_COLOR = QColor("#6ce6ff")


# ── Signal bridge ──────────────────────────────────────────────────────────────
class SignalBridge(QObject):
    node_update = pyqtSignal(dict)


# ── Packet ─────────────────────────────────────────────────────────────────────
class Packet:
    def __init__(self, src, dst):
        self.src = src
        self.dst = dst
        self.progress = 0.0
        self.speed = 0.008 + random.random()*0.006


# ── Canvas ─────────────────────────────────────────────────────────────────────
class NetworkCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(800, 600)  # Increased minimum size
        self._base   = dict(BASE_POS)
        self.nodes   = {}
        self.edges   = list(DEFAULT_EDGES)
        self.packets = []
        self.active_path  = []
        self.path_timer   = 0
        self.failed_nodes = set()
        self.at_risk_nodes= set()
        self.fading       = {}
        self._build_nodes()
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx)

    # ── helpers ────────────────────────────────────────────────────────────────
    def _scale(self, bx, by):
        W = max(self.width(),  1000)
        H = max(self.height(), 700)
        # leave proportional space for header and legend
        header_h = max(80, int(H * 0.1))
        legend_h = max(90, int(H * 0.12))
        usable_y0, usable_y1 = header_h, H - legend_h
        usable_h = usable_y1 - usable_y0
        x = int(bx * W / 1000)
        y = int(usable_y0 + by * usable_h / 700)
        return QPointF(x, y)

    def _pos(self, nid):
        n = self.nodes[nid]
        return self._scale(n["bx"], n["by"])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update()

    def _build_nodes(self):
        self.nodes = {}
        for nid,(bx,by) in self._base.items():
            self.nodes[nid] = {
                "bx":bx,"by":by,
                "status":"unknown",
                "battery":100,"loss":0,
                "alpha":255,"pulse":0,
            }

    # ── public API ─────────────────────────────────────────────────────────────
    def reset_linear(self):
        self._base = dict(BASE_POS)
        self.edges = list(DEFAULT_EDGES)
        self._build_nodes()
        self.packets.clear(); self.failed_nodes.clear()
        self.at_risk_nodes.clear(); self.fading.clear()
        self.active_path.clear()
        self.update()

    def rebuild_custom(self, n_nodes, edges):
        cx,cy,r = 500,350,260
        self._base = {}
        for i in range(1,n_nodes+1):
            angle = 2*math.pi*i/n_nodes - math.pi/2
            self._base[i] = (int(cx+r*math.cos(angle)),
                             int(cy+r*math.sin(angle)))
        self.edges = edges
        self._build_nodes()
        self.packets.clear(); self.failed_nodes.clear()
        self.at_risk_nodes.clear(); self.fading.clear()
        self.active_path.clear()
        self.update()

    def update_node(self, data):
        nid = data["node_id"]
        if nid not in self.nodes: return
        n = self.nodes[nid]
        old = n["status"]
        n["battery"] = data["battery"]
        n["loss"]    = data["packet_loss"]
        new_s = data["status"]
        if data["battery"] <= 0: new_s = "failed"
        n["status"] = new_s
        if new_s == "at_risk":
            self.at_risk_nodes.add(nid)
        elif nid in self.at_risk_nodes and new_s not in ("at_risk","failed"):
            self.at_risk_nodes.discard(nid)
        if new_s == "failed" and old != "failed":
            self._begin_fade(nid)
        self.update()

    def _begin_fade(self, nid):
        self.failed_nodes.add(nid)
        self.at_risk_nodes.discard(nid)
        self.fading[nid] = 255
        self.nodes[nid]["pulse"] = 7.0

    def kill_node(self, nid):
        if nid not in self.nodes: return
        self.nodes[nid]["status"] = "failed"
        self._begin_fade(nid)
        self.update()

    def recover_node(self, nid):
        if nid not in self.nodes: return
        self.nodes[nid].update({
            "status":"healthy","alpha":255,
            "battery":100,"loss":0,"pulse":0
        })
        self.failed_nodes.discard(nid)
        self.at_risk_nodes.discard(nid)
        self.fading.pop(nid,None)
        self.update()

    def highlight_path(self, path_edges):
        self.active_path = list(path_edges)
        self.path_timer  = 100
        self.update()

    # ── tick ───────────────────────────────────────────────────────────────────
    def tick(self):
        # advance / remove packets
        self.packets = [p for p in self.packets if p.progress < 1.0]
        for p in self.packets:
            p.progress += p.speed

        # nodes to avoid
        avoided = self.failed_nodes | self.at_risk_nodes

        # edges safe to use
        safe = [(u,v) for u,v in self.edges
                if u not in avoided and v not in avoided]

        # prefer reroute path edges when active
        if self.active_path:
            preferred = [(u,v) for u,v in self.active_path
                         if u not in avoided and v not in avoided]
            if preferred:
                safe = preferred

        if len(self.packets) < 55 and safe and random.random() < 0.38:
            self.packets.append(Packet(*random.choice(safe)))

        # pulse decay
        for n in self.nodes.values():
            if n["pulse"] > 0:
                n["pulse"] = max(0, n["pulse"]-0.11)

        # fade
        for nid in list(self.fading):
            self.fading[nid] -= 5
            self.nodes[nid]["alpha"] = max(0, self.fading[nid])
            if self.fading[nid] <= 0:
                del self.fading[nid]

        # path timer
        if self.path_timer > 0:
            self.path_timer -= 1
            if self.path_timer == 0:
                self.active_path.clear()

        self.update()

    # ── paint ──────────────────────────────────────────────────────────────────
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W,H = self.width(), self.height()

        # Calculate scaling factor for fonts and elements
        scale_factor = max(0.8, min(1.5, W / 1380))
        font_scale = lambda base: max(8, int(base * scale_factor))

        # background
        bg = QLinearGradient(0,0,W,H)
        bg.setColorAt(0, QColor("#06131f"))
        bg.setColorAt(0.45, QColor("#081b2b"))
        bg.setColorAt(1, QColor("#071926"))
        p.fillRect(self.rect(), bg)

        # subtle grid
        p.setPen(QPen(QColor(108,230,255,20),1))
        grid_spacing = max(50, int(70 * scale_factor))
        for x in range(0,W,grid_spacing): p.drawLine(x,0,x,H)
        for y in range(0,H,grid_spacing): p.drawLine(0,y,W,y)

        # industry glow accent band
        accent = QColor("#6ce6ff")
        accent.setAlpha(30)
        p.setBrush(QBrush(accent))
        p.setPen(Qt.NoPen)
        p.drawRect(0,0,W,14)

        # ── HEADER ────────────────────────────────────────────────────────────
        header_font_size = font_scale(18)
        p.setFont(QFont("Arial", header_font_size, QFont.Bold))
        p.setPen(QColor("#90caf9"))
        header_height = max(36, int(36 * scale_factor))
        p.drawText(QRectF(20,10,W-40,header_height), Qt.AlignLeft|Qt.AlignVCenter,
                   "SentinelMesh — IIoT Pipeline Network")
        subtitle_font_size = font_scale(11)
        p.setFont(QFont("Arial", subtitle_font_size))
        p.setPen(QColor("#546e7a"))
        subtitle_height = max(24, int(24 * scale_factor))
        p.drawText(QRectF(20,10 + header_height,W-40,subtitle_height), Qt.AlignLeft|Qt.AlignVCenter,
                   "AI-Based Failure Prediction & Self-Healing WSN")

        # ── EDGES ─────────────────────────────────────────────────────────────
        avoided = self.failed_nodes | self.at_risk_nodes
        for u,v in self.edges:
            if u not in self.nodes or v not in self.nodes: continue
            pu,pv = self._pos(u), self._pos(v)
            failed = u in self.failed_nodes or v in self.failed_nodes
            atrisk = (u in self.at_risk_nodes or v in self.at_risk_nodes) and not failed
            hi     = (u,v) in self.active_path or (v,u) in self.active_path

            if hi:
                p.setPen(QPen(QColor(255,214,0,50),12)); p.drawLine(pu,pv)
                p.setPen(QPen(QColor("#ffd600"),3));      p.drawLine(pu,pv)
            elif failed:
                p.setPen(QPen(QColor("#c62828"),2,Qt.DashLine)); p.drawLine(pu,pv)
            elif atrisk:
                # amber dashed — warning, do not route here
                p.setPen(QPen(QColor("#ffab00"),2,Qt.DashLine)); p.drawLine(pu,pv)
            else:
                p.setPen(QPen(QColor(30,136,229,45),9)); p.drawLine(pu,pv)
                p.setPen(QPen(QColor("#1e88e5"),2));     p.drawLine(pu,pv)

        # ── PACKETS ───────────────────────────────────────────────────────────
        for pkt in self.packets:
            if pkt.src not in self.nodes or pkt.dst not in self.nodes: continue
            if pkt.src in avoided or pkt.dst in avoided: continue
            ps = self._pos(pkt.src); pd2 = self._pos(pkt.dst)
            x  = ps.x()+(pd2.x()-ps.x())*pkt.progress
            y  = ps.y()+(pd2.y()-ps.y())*pkt.progress
            gl = QRadialGradient(x,y,11)
            gl.setColorAt(0,QColor(128,222,234,190))
            gl.setColorAt(1,QColor(128,222,234,0))
            p.setBrush(QBrush(gl)); p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(x,y),11,11)
            p.setBrush(QBrush(QColor("#80deea")))
            p.setPen(QPen(QColor("#ffffff"),1))
            p.drawEllipse(QPointF(x,y),5,5)

        # ── NODES ─────────────────────────────────────────────────────────────
        for nid,node in self.nodes.items():
            pos  = self._pos(nid)
            st   = node["status"]
            alp  = node["alpha"]
            puls = node["pulse"]
            col  = QColor(STATUS_COLORS.get(st,STATUS_COLORS["unknown"]))
            col.setAlpha(alp)

            # outer glow
            if st in ("healthy","at_risk","failed") and alp>30:
                gc = QColor(col); gc.setAlpha(min(65,alp//3))
                p.setBrush(QBrush(gc)); p.setPen(Qt.NoPen)
                p.drawEllipse(pos,NODE_R+16,NODE_R+16)

            # pulse ring
            if puls>0:
                ra = int(puls/7*210)
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(244,67,54,ra),2.5))
                rx = int((1-puls/7)*28)
                p.drawEllipse(pos,NODE_R+12+rx,NODE_R+12+rx)

            # fill
            gr = QRadialGradient(pos.x()-NODE_R//3,pos.y()-NODE_R//3,NODE_R*2.2)
            lt = col.lighter(160); lt.setAlpha(alp)
            gr.setColorAt(0,lt); gr.setColorAt(1,col)
            p.setBrush(QBrush(gr))
            bc2 = col.lighter(200); bc2.setAlpha(alp)
            p.setPen(QPen(bc2,2))
            p.drawEllipse(pos,NODE_R,NODE_R)

            # node label — centred exactly in circle
            lbl = "SINK" if nid==SINK_NODE else f"N{nid}"
            lc  = QColor("#ffffff"); lc.setAlpha(alp)
            p.setPen(lc)
            node_font_size = font_scale(13)
            p.setFont(QFont("Arial", node_font_size, QFont.Bold))
            r = QRectF(pos.x()-NODE_R, pos.y()-NODE_R, NODE_R*2, NODE_R*2)
            p.drawText(r, Qt.AlignCenter, lbl)

            # metrics — in a small pill ABOVE the node to avoid overlap
            if st != "unknown" and alp > 80:
                met = f"B:{node['battery']}%  L:{node['loss']}%"
                metrics_font_size = font_scale(8)
                fm  = QFontMetrics(QFont("Arial", metrics_font_size))
                tw  = fm.horizontalAdvance(met)+12
                th  = max(18, int(18 * scale_factor))
                mx  = int(pos.x()-tw/2)
                my  = int(pos.y()-NODE_R-th-6)
                mc  = QColor("#1e2a3a"); mc.setAlpha(200)
                p.setBrush(QBrush(mc)); p.setPen(Qt.NoPen)
                p.drawRoundedRect(mx,my,tw,th,4,4)
                tc = QColor("#cfd8dc"); tc.setAlpha(alp)
                p.setPen(tc)
                p.setFont(QFont("Arial", metrics_font_size))
                p.drawText(QRectF(mx,my,tw,th), Qt.AlignCenter, met)

            # status badge — BELOW the node
            badges = {"at_risk":"⚠ AT RISK","failed":"✖ FAILED"}
            if st in badges and alp>110:
                badge_txt = badges[st]
                badge_font_size = font_scale(9)
                fm2 = QFontMetrics(QFont("Arial", badge_font_size, QFont.Bold))
                bw  = fm2.horizontalAdvance(badge_txt)+16
                bh  = max(22, int(22 * scale_factor))
                bx2 = int(pos.x()-bw/2)
                by2 = int(pos.y()+NODE_R+6)
                bc3 = QColor(col); bc3.setAlpha(210)
                p.setBrush(QBrush(bc3)); p.setPen(Qt.NoPen)
                p.drawRoundedRect(bx2,by2,bw,bh,5,5)
                p.setPen(QColor("#ffffff"))
                p.setFont(QFont("Arial", badge_font_size, QFont.Bold))
                p.drawText(QRectF(bx2,by2,bw,bh), Qt.AlignCenter, badge_txt)

        # ── LEGEND — left-side vertical panel ──────────────────────────────
        items = [
            ("#37474f","Unknown"),("#00e676","Healthy"),
            ("#ffab00","At Risk"),("#f44336","Failed"),
        ]
        dot_r = max(8, int(8 * scale_factor))
        legend_font_size = font_scale(10)
        p.setFont(QFont("Arial", legend_font_size, QFont.Bold))
        legend_title = "LEGEND"
        title_h = max(18, int(18 * scale_factor))
        item_h = max(22, int(22 * scale_factor))
        item_gap = max(8, int(8 * scale_factor))
        leg_padding = max(14, int(14 * scale_factor))
        legend_w = max(170, int(170 * scale_factor))
        legend_h = leg_padding * 2 + title_h + item_gap + len(items) * item_h
        legend_x = 20
        legend_y = H - legend_h - 20

        # floating legend background
        lb = QColor("#0a1628"); lb.setAlpha(220)
        p.setBrush(QBrush(lb)); p.setPen(Qt.NoPen)
        p.drawRoundedRect(legend_x, legend_y, legend_w, legend_h, 14, 14)

        p.setPen(QColor("#546e7a"))
        p.setFont(QFont("Arial", legend_font_size, QFont.Bold))
        p.drawText(QRectF(legend_x + leg_padding, legend_y + leg_padding,
                   legend_w - leg_padding*2, title_h),
                   Qt.AlignLeft|Qt.AlignVCenter, legend_title)

        for i,(c,lbl2) in enumerate(items):
            item_y = legend_y + leg_padding + title_h + item_gap + i * item_h
            p.setBrush(QBrush(QColor(c))); p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(legend_x + leg_padding + dot_r, item_y + item_h/2), dot_r, dot_r)
            p.setPen(QColor("#cfd8dc"))
            p.setFont(QFont("Arial", legend_font_size))
            p.drawText(QRectF(legend_x + leg_padding + dot_r*2 + 10,
                       item_y, legend_w - leg_padding*2 - dot_r*2 - 10, item_h),
                       Qt.AlignLeft|Qt.AlignVCenter, lbl2)

        p.end()

    # ── context menu ───────────────────────────────────────────────────────────
    def _ctx(self, pos):
        nid = self._node_at(pos)
        if nid is None: return
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu{background:#131f2e;color:white;border:1px solid #1e88e5;
                  border-radius:6px;font-size:13px;padding:4px;}
            QMenu::item{padding:9px 22px;}
            QMenu::item:selected{background:#1e3a5f;border-radius:4px;}
        """)
        menu.addAction(f"💀  Kill Node {nid}").triggered.connect(
            lambda: self.parent().parent().kill_node(nid))
        menu.addAction(f"⚠  Degrade Node {nid}").triggered.connect(
            lambda: self.parent().parent().degrade_node(nid))
        menu.addAction(f"✔  Recover Node {nid}").triggered.connect(
            lambda: self.parent().parent().recover_node(nid))
        menu.exec_(self.mapToGlobal(pos))

    def _node_at(self, pos):
        for nid in self.nodes:
            np2 = self._pos(nid)
            dx,dy = pos.x()-np2.x(), pos.y()-np2.y()
            if math.sqrt(dx*dx+dy*dy) < NODE_R+8: return nid
        return None


# ── Main window ────────────────────────────────────────────────────────────────
class SentinelMesh(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            "SentinelMesh — IIoT WSN Failure Prediction & Self-Healing")
        self.resize(1600,900)  # Increased default size for better readability
        self.setStyleSheet(
            f"background:{BG_COLOR.name()};color:#e1f5fe;")
        self.bridge = SignalBridge()
        self.bridge.node_update.connect(self._on_update)

        # widget registries for responsive scaling
        self._sec_labels   = []
        self._lbl_labels   = []
        self._btn_widgets  = []   # (widget, color_hex)
        self._combo_widgets= []
        self._inp_widgets  = []
        self._sidebar_meta = {}   # role → widget

        self._build_ui()
        self._start_udp()
        self.timer = QTimer()
        self.timer.timeout.connect(self.canvas.tick)
        self.timer.start(50)

    # ── responsive ─────────────────────────────────────────────────────────────
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_scale()

    def _fs(self, base):
        # Use geometric mean of width and height scaling for better proportion
        w_scale = self.width() / 1600
        h_scale = self.height() / 900
        scale = (w_scale * h_scale) ** 0.5
        scale = max(0.8, min(2.0, scale))  # Allow larger scaling for maximized windows
        return max(8, int(base * scale))

    def _apply_scale(self):
        fs = self._fs
        for role,w in self._sidebar_meta.items():
            if role=="title":
                w.setStyleSheet(
                    f"color:#90caf9;font-size:{fs(18)}px;"
                    f"font-weight:bold;background:transparent;")
            elif role=="subtitle":
                w.setStyleSheet(
                    f"color:#546e7a;font-size:{fs(11)}px;background:transparent;")
            elif role=="log":
                w.setStyleSheet(
                    f"background:{CARD_COLOR.name()};color:#b0bec5;"
                    f"font-size:{fs(12)}px;border:1px solid #1e2a3a;"
                    f"border-radius:6px;padding:6px;")
        for w in self._sec_labels:
            w.setStyleSheet(
                f"color:#1e88e5;font-size:{fs(12)}px;font-weight:bold;"
                f"background:transparent;letter-spacing:1px;")
        for w in self._lbl_labels:
            w.setStyleSheet(
                f"color:#90a4ae;font-size:{fs(12)}px;background:transparent;")
        for w,c in self._btn_widgets:
            w.setFixedHeight(max(32, fs(42)))  # Increased base height
            w.setStyleSheet(
                f"QPushButton{{background:{c};color:white;border:none;"
                f"border-radius:10px;font-size:{fs(14)}px;"
                f"font-weight:bold;padding:8px 14px;"
                f"background-color:{c};}}"
                f"QPushButton:hover{{background:{c}dd;}}"
                f"QPushButton:pressed{{background:{c}aa;}}")
        for w in self._combo_widgets:
            w.setFixedHeight(max(30, fs(40)))  # Increased height
            w.setStyleSheet(
                f"QComboBox{{background:{CARD_COLOR.name()};color:white;"
                f"border:1px solid #1e2a3a;border-radius:8px;"  # Larger radius
                f"padding:6px 14px;font-size:{fs(14)}px;}}"  # Increased padding and font
                f"QComboBox::drop-down{{border:none;width:28px;}}"  # Larger dropdown
                f"QComboBox QAbstractItemView{{background:{CARD_COLOR.name()};"
                f"color:white;selection-background-color:#1e3a5f;"
                f"border:1px solid #1e2a3a;font-size:{fs(14)}px;}}")
        for w in self._inp_widgets:
            w.setFixedHeight(max(28, fs(38)))  # Increased height
            w.setStyleSheet(
                f"QLineEdit{{background:{CARD_COLOR.name()};color:white;"
                f"border:1px solid #1e2a3a;border-radius:8px;"  # Larger radius
                f"padding:6px 12px;font-size:{fs(14)}px;}}"  # Increased padding and font
                f"QLineEdit:focus{{border:1px solid #1e88e5;}}")

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(15,15,15,15)  # Increased margins
        layout.setSpacing(15)  # Increased spacing

        self.canvas = NetworkCanvas(self)
        layout.addWidget(self.canvas, stretch=5)  # Increased stretch for canvas

        sidebar = QWidget()
        sidebar.setMinimumWidth(280)  # Increased minimum width
        sidebar.setMaximumWidth(450)  # Increased maximum width
        sidebar.setStyleSheet(
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            f"stop:0 {PANEL_COLOR.name()}, stop:1 #142b44);"
            f"border:1px solid rgba(108, 230, 255, 0.14);"
            f"border-radius:16px;")  # Larger border radius
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(18,18,18,18)  # Increased margins
        sb.setSpacing(12)  # Increased spacing

        # header
        h = QLabel("⬡  SentinelMesh")
        h.setStyleSheet("color:#90caf9;font-size:18px;font-weight:bold;background:transparent;")
        self._sidebar_meta["title"] = h
        sb.addWidget(h)

        s = QLabel("IIoT Wireless Sensor Network Monitor")
        s.setStyleSheet("color:#546e7a;font-size:11px;background:transparent;")
        self._sidebar_meta["subtitle"] = s
        sb.addWidget(s)
        sb.addWidget(self._div())

        # Network builder
        sb.addWidget(self._sec("🔧  NETWORK BUILDER"))
        self.topo_combo = self._combo(["Linear (Pipeline)","Custom"])
        self.topo_combo.currentTextChanged.connect(
            lambda t: self.custom_w.setVisible(t=="Custom"))
        sb.addWidget(self.topo_combo)

        self.custom_w = QWidget()
        self.custom_w.setStyleSheet("background:transparent;")
        cw = QVBoxLayout(self.custom_w)
        cw.setContentsMargins(0,0,0,0); cw.setSpacing(4)
        cw.addWidget(self._lbl("Number of nodes:"))
        self.n_input = self._inp("8")
        cw.addWidget(self.n_input)
        cw.addWidget(self._lbl("Edges (e.g. 1-2,2-3):"))
        self.e_input = self._inp("1-2,2-3,3-4,4-5,5-6,6-7,7-8")
        cw.addWidget(self.e_input)
        self.custom_w.setVisible(False)
        sb.addWidget(self.custom_w)
        sb.addWidget(self._btn("⚙  Build Network","#3949ab",self._build_net))

        sb.addWidget(self._div())
        sb.addWidget(self._sec("▶  SIMULATION CONTROLS"))
        row = QHBoxLayout()
        row.addWidget(self._btn("▶ Start","#2e7d32",self._start))
        row.addWidget(self._btn("⏸ Pause","#37474f",self._pause))
        sb.addLayout(row)
        sb.addWidget(self._btn("↺  Reset Network","#37474f",self._reset))

        sb.addWidget(self._div())
        sb.addWidget(self._sec("⚡  FAULT INJECTION"))
        sb.addWidget(self._lbl("Select Node:"))
        self.fault_combo = self._combo([str(i) for i in range(1,9)])
        sb.addWidget(self.fault_combo)

        row2 = QHBoxLayout()
        row2.addWidget(self._btn("💀 Kill","#b71c1c",
            lambda: self.kill_node(int(self.fault_combo.currentText()))))
        row2.addWidget(self._btn("⚠ Degrade","#e65100",
            lambda: self.degrade_node(int(self.fault_combo.currentText()))))
        row2.addWidget(self._btn("✔ Recover","#1b5e20",
            lambda: self.recover_node(int(self.fault_combo.currentText()))))
        sb.addLayout(row2)

        sb.addWidget(self._lbl("Degradation Speed:"))
        self.spd = QSlider(Qt.Horizontal)
        self.spd.setRange(1,5); self.spd.setValue(2)
        self.spd.setStyleSheet("""
            QSlider::groove:horizontal{height:10px;background:rgba(108,230,255,0.16);border-radius:5px;}
            QSlider::handle:horizontal{background:#6ce6ff;width:22px;height:22px;
                margin:-6px 0;border-radius:11px;}
            QSlider::sub-page:horizontal{background:#6ce6ff;border-radius:5px;}
        """)
        sb.addWidget(self.spd)

        sb.addWidget(self._div())
        sb.addWidget(self._sec("📋  EVENT LOG"))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setStyleSheet(
            f"background:rgba(18,37,64,0.88);color:#d0e6f5;"
            f"font-size:14px;border:1px solid rgba(108,230,255,0.16);"
            f"border-radius:12px;padding:10px;")  # Increased font size and padding
        self._sidebar_meta["log"] = self.log_box
        sb.addWidget(self.log_box, stretch=1)

        layout.addWidget(sidebar, stretch=1)

    # ── widget helpers ──────────────────────────────────────────────────────────
    def _sec(self, t):
        l = QLabel(t)
        l.setStyleSheet("color:#1e88e5;font-size:12px;font-weight:bold;"
                        "background:transparent;letter-spacing:1px;")
        l.setWordWrap(True)
        self._sec_labels.append(l)
        return l

    def _lbl(self, t):
        l = QLabel(t)
        l.setStyleSheet("color:#90a4ae;font-size:12px;background:transparent;")
        l.setWordWrap(True)
        self._lbl_labels.append(l)
        return l

    def _div(self):
        d = QFrame(); d.setFrameShape(QFrame.HLine)
        d.setStyleSheet("background:#1e2a3a;max-height:1px;")
        return d

    def _btn(self, t, c, fn=None):
        b = QPushButton(t)
        b.setFixedHeight(42)  # Increased default height
        b.setStyleSheet(
            f"QPushButton{{background:{c};color:white;border:none;"
            f"border-radius:8px;font-size:14px;font-weight:bold;padding:6px 10px;}}"
            f"QPushButton:hover{{background:{c}cc;}}"
            f"QPushButton:pressed{{background:{c}88;}}")
        if fn: b.clicked.connect(fn)
        self._btn_widgets.append((b,c))
        return b

    def _combo(self, items):
        c = QComboBox(); c.addItems(items)
        c.setFixedHeight(40)  # Increased height
        c.setStyleSheet(
            f"QComboBox{{background:{CARD_COLOR.name()};color:white;"
            f"border:1px solid #1e2a3a;border-radius:8px;"
            f"padding:6px 14px;font-size:14px;}}"
            f"QComboBox::drop-down{{border:none;width:28px;}}"
            f"QComboBox QAbstractItemView{{background:{CARD_COLOR.name()};"
            f"color:white;selection-background-color:#1e3a5f;"
            f"border:1px solid #1e2a3a;font-size:14px;}}")
        self._combo_widgets.append(c)
        return c

    def _inp(self, ph):
        i = QLineEdit(ph); i.setFixedHeight(38)  # Increased height
        i.setStyleSheet(
            f"QLineEdit{{background:{CARD_COLOR.name()};color:white;"
            f"border:1px solid #1e2a3a;border-radius:8px;"
            f"padding:6px 12px;font-size:14px;}}"
            f"QLineEdit:focus{{border:1px solid #1e88e5;}}")
        self._inp_widgets.append(i)
        return i

    # ── log ─────────────────────────────────────────────────────────────────────
    def _log(self, msg, col="#b0bec5"):
        t = time.strftime("%H:%M:%S")
        self.log_box.append(
            f'<span style="color:{col};font-size:14px">[{t}]  {msg}</span>')  # Increased font size
        self.log_box.verticalScrollBar().setValue(
            self.log_box.verticalScrollBar().maximum())

    # ── simulation ──────────────────────────────────────────────────────────────
    def _start(self):
        self.timer.start(50)
        self._log("Simulation started — monitoring active","#00e676")

    def _pause(self):
        self.timer.stop()
        self._log("Simulation paused","#ffab00")

    def _reset(self):
        self.canvas.reset_linear()
        self._update_fault_combo(8)
        self._log("Network reset to default","#90caf9")

    def _build_net(self):
        topo = self.topo_combo.currentText()
        if topo=="Linear (Pipeline)":
            self.canvas.reset_linear()
            self._update_fault_combo(8)
            self._log("Linear pipeline topology loaded","#90caf9")
        else:
            try:
                n = int(self.n_input.text())
                edges=[]
                for pair in self.e_input.text().split(","):
                    a,b = pair.strip().split("-")
                    edges.append((int(a),int(b)))
                self.canvas.rebuild_custom(n,edges)
                self._update_fault_combo(n)
                self._log(f"Custom: {n} nodes, {len(edges)} edges","#90caf9")
            except Exception as e:
                self._log(f"Topology error: {e}","#f44336")

    def _update_fault_combo(self,n):
        self.fault_combo.clear()
        self.fault_combo.addItems([str(i) for i in range(1,n+1)])

    # ── fault ───────────────────────────────────────────────────────────────────
    def kill_node(self, nid):
        self.canvas.kill_node(nid)
        self._log(f"💀  Node {nid} killed","#f44336")
        self._show_reroute(nid)

    def degrade_node(self, nid):
        spd = self.spd.value()
        self._log(f"⚠  Node {nid} degrading at speed {spd}x","#ffab00")
        threading.Thread(target=self._dw,args=(nid,spd),daemon=True).start()

    def _dw(self, nid, spd):
        bat,loss = 100,0
        while bat>0 and nid not in self.canvas.failed_nodes:
            bat  = max(0,  bat-spd)
            loss = min(100,loss+spd*2)
            prob = 1.0 if (bat<20 or loss>60) else 0.0
            st   = "at_risk" if prob>0.75 else "healthy"
            if bat<=0: st="failed"
            self.bridge.node_update.emit({
                "node_id":nid,"status":st,
                "battery":bat,"packet_loss":loss,"prob":prob})
            time.sleep(1.0/spd)
        self.bridge.node_update.emit({
            "node_id":nid,"status":"failed",
            "battery":0,"packet_loss":100,"prob":1.0})

    def recover_node(self, nid):
        self.canvas.recover_node(nid)
        self._log(f"✔  Node {nid} recovered","#00e676")

    def _show_reroute(self, nid):
        paths = {
            1:[(2,5),(5,7),(7,8)],
            2:[(1,3),(3,5),(5,7),(7,8)],
            3:[(1,2),(2,5),(5,7),(7,8)],
            4:[(2,5),(5,7),(7,8)],
            5:[(2,4),(4,6),(6,8)],
            6:[(4,2),(2,5),(5,7),(7,8)],
            7:[(5,3),(3,1),(1,2)],
        }
        path = paths.get(nid,[])
        if path:
            self.canvas.highlight_path(path)
            self._log(f"↺  Rerouted around Node {nid} — yellow path active","#ffd600")

    # ── UDP ─────────────────────────────────────────────────────────────────────
    def _start_udp(self):
        threading.Thread(target=self._udp,daemon=True).start()

    def _udp(self):
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1",DASHBOARD_PORT))
        sock.settimeout(1.0)
        while True:
            try:
                data,_=sock.recvfrom(4096)
                self.bridge.node_update.emit(json.loads(data.decode()))
            except socket.timeout: pass
            except Exception as e: print(f"[Dashboard] {e}")

    def _on_update(self, data):
        nid=data["node_id"]; st=data["status"]
        self.canvas.update_node(data)
        if st=="at_risk":
            self._log(
                f"⚠  Node {nid} AT RISK — B:{data['battery']}%  "
                f"L:{data['packet_loss']}% — rerouting preemptively","#ffab00")
            self._show_reroute(nid)
        elif st=="failed":
            self._log(f"💀  Node {nid} FAILED — removed","#f44336")
            self._show_reroute(nid)


# ── entry ──────────────────────────────────────────────────────────────────────
if __name__=="__main__":
    app=QApplication(sys.argv)
    app.setStyle("Fusion")
    w=SentinelMesh()
    w.show()
    sys.exit(app.exec_())