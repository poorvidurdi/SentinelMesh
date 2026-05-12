import subprocess, sys, time, os

procs = []

def launch(cmd, label):
    p = subprocess.Popen(
        [sys.executable] + cmd
    )
    procs.append(p)
    print(f"[Launcher] Started {label} (PID {p.pid})")
    time.sleep(0.5)

print("[Launcher] Starting SentinelMesh...")

launch(["server.py"],    "Flask Server")
time.sleep(2)
launch(["monitor.py"],   "Monitor + ML")
time.sleep(1)

for i in range(1, 9):
    launch(["node.py", "--id", str(i)], f"Node {i}")

print("\n[Launcher] All processes started")
print("[Launcher] Dashboard: http://localhost:5000")
print("[Launcher] Press Ctrl+C to stop all\n")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n[Launcher] Shutting down all processes...")
    for p in procs:
        p.terminate()
    print("[Launcher] Done")
