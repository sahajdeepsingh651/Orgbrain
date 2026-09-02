import http.server
import json
import os
import socketserver
import secrets
from pathlib import Path
import threading

PORT = 8081
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "store" / "config"

account_map_path = CONFIG_DIR / "account_map.json"
api_tokens_path = CONFIG_DIR / "api_tokens.json"

class CaptureHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body)
            # Claude Code sends account_uuid embedded as a JSON string inside metadata.user_id
            user_id_str = data.get('metadata', {}).get('user_id', '{}')
            user_id_data = json.loads(user_id_str)
            account_uuid = user_id_data.get('account_uuid')
            
            if account_uuid:
                print(f"\n✅ Successfully captured your account_uuid: {account_uuid}")
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"id":"msg_mock","type":"message","role":"assistant","content":[{"type":"text","text":"Setup successful!"}]}')
                
                # Run the config setup in a new thread so the server can shut down cleanly
                threading.Thread(target=setup_configs, args=(account_uuid,)).start()
                
                # Tell the server to shut down
                threading.Thread(target=self.server.shutdown).start()
            else:
                print("\n❌ Failed to find account_uuid in request.")
                self.send_response(400)
                self.end_headers()
        except Exception as e:
            print(f"\n❌ Error parsing request: {e}")
            self.send_response(500)
            self.end_headers()

def setup_configs(account_uuid):
    print("\nLet's configure your Data Passport identity.")
    user_id = input("Enter your username (e.g. u-dev): ") or "u-dev"
    department = input("Enter your department (e.g. Engineering): ") or "Engineering"
    team = input("Enter your team (e.g. platform): ") or "platform"
    
    bus_token = f"token-{secrets.token_hex(8)}"
    
    # 1. Update account_map.json
    if not account_map_path.exists():
        if (CONFIG_DIR / "account_map.example.json").exists():
            account_map_path.write_text((CONFIG_DIR / "account_map.example.json").read_text())
        else:
            account_map_path.write_text("{}")
            
    try:
        acc_map = json.loads(account_map_path.read_text())
        # Remove the comment for clean insertion if it exists
        if "_comment" in acc_map:
            del acc_map["_comment"]
    except:
        acc_map = {}
        
    acc_map[account_uuid] = {
        "bus_token": bus_token,
        "user_id": user_id,
        "department": department,
        "team": team
    }
    account_map_path.write_text(json.dumps(acc_map, indent=2))
    
    # 2. Update api_tokens.json
    if not api_tokens_path.exists():
        if (CONFIG_DIR / "api_tokens.example.json").exists():
            api_tokens_path.write_text((CONFIG_DIR / "api_tokens.example.json").read_text())
        else:
            api_tokens_path.write_text("{}")
            
    try:
        api_tokens = json.loads(api_tokens_path.read_text())
    except:
        api_tokens = {}
        
    api_tokens[bus_token] = {
        "user_id": user_id,
        "department": department,
        "team": team
    }
    api_tokens_path.write_text(json.dumps(api_tokens, indent=2))
    
    print(f"\n🎉 Setup complete!")
    print(f"Generated bus_token: {bus_token}")
    print(f"Updated {account_map_path.name}")
    print(f"Updated {api_tokens_path.name}")
    print("\nYou can now start the gateway and bus normally.")

if __name__ == "__main__":
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    print(f"🎧 Listening on port {PORT} for Claude Code request...")
    print("👉 Please open a new terminal and run:")
    print(f"   ANTHROPIC_BASE_URL=http://localhost:{PORT} claude\n")
    
    with socketserver.TCPServer(("", PORT), CaptureHandler) as httpd:
        httpd.serve_forever()
