import socket
import json
import time
import hmac
import hashlib

SHARED_KEY = b"wsn_secret_key"

def sign_packet(data: dict) -> str:
    msg = json.dumps(data, sort_keys=True).encode()
    return hmac.new(SHARED_KEY, msg, hashlib.sha256).hexdigest()

def make_signed_packet(payload: dict) -> bytes:
    packet = payload.copy()
    packet["timestamp"] = time.time()
    packet["signature"] = sign_packet(packet)
    return json.dumps(packet).encode()

if __name__ == "__main__":
    print("[Demo] Triggering True Network Degradation on Node 4 (Port 5004) over 10 seconds...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    start_batt = 100
    end_batt = 30
    start_loss = 0
    end_loss = 70
    steps = 10
    
    for i in range(1, steps + 1):
        curr_batt = int(start_batt - (start_batt - end_batt) * (i / steps))
        curr_loss = int(start_loss + (end_loss - start_loss) * (i / steps))
        
        payload = {
            "type": "SIM_FAULT",
            "action": "healthy", # Let the monitor's ML model upgrade this to 'at_risk' proactively!
            "battery": curr_batt,
            "packet_loss": curr_loss
        }
        
        try:
            sock.sendto(make_signed_packet(payload), ("127.0.0.1", 5004))
            print(f"[Demo] Degrading ({i}/{steps})... Battery: {curr_batt}% | Loss: {curr_loss}%")
        except Exception as e:
            print(f"[Demo] Failed to send fault packet: {e}")
            
        time.sleep(1)
        
    print("[Demo] Degradation complete. Watch the dashboard to see proactive rerouting engage!")
