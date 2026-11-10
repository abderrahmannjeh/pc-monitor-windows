"""
PC Monitor - serveur local de collecte de métriques.
Lance un petit serveur HTTP sur 127.0.0.1 et sert le tableau de bord (dashboard.html).
Dépendance : psutil  (pip install psutil)
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

import cleaner
import gpu_pref

HOST, PORT = "127.0.0.1", 8765
VERSION = "1.0.0"
# fichiers embarqués (PyInstaller les décompresse dans sys._MEIPASS)
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# URL publique du site, écrite par build_agent.bat dans agent_config.py
try:
    from agent_config import SITE_URL
except ImportError:
    SITE_URL = os.environ.get("PCMON_SITE_URL", "")
SITE_URL = SITE_URL.rstrip("/")
ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
if SITE_URL:
    ALLOWED_ORIGINS.add(re.match(r"^https?://[^/]+", SITE_URL).group(0))
# origines supplémentaires pour tester en local (ex. PCMON_EXTRA_ORIGINS=http://localhost:8080)
ALLOWED_ORIGINS |= {o.strip().rstrip("/") for o in os.environ.get("PCMON_EXTRA_ORIGINS", "").split(",") if o.strip()}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CPU_COUNT = psutil.cpu_count() or 1
ME = os.getpid()

# --------------------------------------------------------------------------
# Filtrage : on n'affiche que les applications utilisateur, pas le système
# --------------------------------------------------------------------------
SYSTEM_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe", "fontdrvhost.exe",
    "dwm.exe", "explorer.exe", "sihost.exe", "taskhostw.exe", "ctfmon.exe",
    "runtimebroker.exe", "searchhost.exe", "searchindexer.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "textinputhost.exe", "applicationframehost.exe",
    "conhost.exe", "dllhost.exe", "smartscreen.exe", "securityhealthsystray.exe",
    "securityhealthservice.exe", "msmpeng.exe", "nissrv.exe", "audiodg.exe",
    "lockapp.exe", "useroobebroker.exe", "widgets.exe", "widgetservice.exe",
    "crossdeviceresume.exe", "phoneexperiencehost.exe", "systemsettings.exe",
    "backgroundtaskhost.exe", "wudfhost.exe", "spoolsv.exe", "memory compression",
    "secure system", "lsaiso.exe", "wmiprvse.exe", "searchprotocolhost.exe",
    "searchfilterhost.exe", "sgrmbroker.exe", "unsecapp.exe", "taskmgr.exe",
    "igfxem.exe", "igfxcuiservice.exe", "nvcontainer.exe", "nvdisplay.container.exe",
    "rtkauduservice64.exe", "mpdefendercoreservice.exe", "smss", "midisrv.exe",
}

_user = None


def current_user():
    global _user
    if _user is None:
        try:
            _user = psutil.Process(ME).username()
        except Exception:
            _user = ""
    return _user


def is_user_app(p_info):
    name = (p_info.get("name") or "").lower()
    exe = (p_info.get("exe") or "").lower()
    if not name or name in SYSTEM_NAMES:
        return False
    if p_info.get("pid") in (0, 4, ME):
        return False
    if (p_info.get("username") or "") != current_user():
        return False
    windir = os.environ.get("WINDIR", r"C:\Windows").lower()
    if exe.startswith(windir):
        return False
    return True


# --------------------------------------------------------------------------
# PIDs possédant une fenêtre visible (= "Applications" comme le Gestionnaire)
# --------------------------------------------------------------------------
def pids_with_windows():
    if os.name != "nt":
        return set()
    user32 = ctypes.windll.user32
    pids = set()
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            if user32.GetWindow(hwnd, 4) == 0:  # GW_OWNER : fenêtre principale
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pids.add(pid.value)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return pids


# --------------------------------------------------------------------------
# GPU : nvidia-smi + compteurs de performance Windows (par processus)
# --------------------------------------------------------------------------
class GpuCollector(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__()
        self.nvidia = []
        self.engines = {}      # luid -> % (moteur 3D)
        self.per_pid = {}      # pid -> %
        self.adapters = []
        self.has_nvsmi = self._which("nvidia-smi")
        self.nvidia_pids = set()   # PIDs qui utilisent la carte NVIDIA
        self._load_adapters()

    @staticmethod
    def _which(cmd):
        from shutil import which
        return which(cmd) is not None

    def _load_adapters(self):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterRAM | ConvertTo-Json"],
                capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout
            data = json.loads(out) if out.strip() else []
            if isinstance(data, dict):
                data = [data]
            self.adapters = [{"name": d.get("Name"), "driver": d.get("DriverVersion")} for d in data]
        except Exception:
            self.adapters = []

    def _poll_nvidia(self):
        if not self.has_nvsmi:
            return
        fields = "name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,clocks.gr,clocks.mem,fan.speed"
        try:
            out = subprocess.run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout
        except Exception:
            return
        gpus = []
        for line in out.strip().splitlines():
            v = [x.strip() for x in line.split(",")]

            def num(x):
                try:
                    return float(x)
                except ValueError:
                    return None
            if len(v) >= 10:
                gpus.append({
                    "name": v[0], "util": num(v[1]), "mem_util": num(v[2]),
                    "mem_used": num(v[3]), "mem_total": num(v[4]), "temp": num(v[5]),
                    "power": num(v[6]), "clock": num(v[7]), "mem_clock": num(v[8]), "fan": num(v[9]),
                })
        self.nvidia = gpus
        # tableau des processus de nvidia-smi : « |    0   N/A  N/A   4260   C+G   ...Code.exe   N/A | »
        try:
            table = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5,
                                   creationflags=NO_WINDOW).stdout
            self.nvidia_pids = {int(m.group(1)) for m in
                                re.finditer(r"^\|\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(?:C\+G|C|G)\s", table, re.M)}
        except Exception:
            pass

    def _poll_counters(self):
        if os.name != "nt":
            return
        ps = ("$s=(Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -ErrorAction SilentlyContinue).CounterSamples;"
              "$s | ForEach-Object { $_.InstanceName + '|' + [string]::Format([cultureinfo]::InvariantCulture,'{0}',$_.CookedValue) }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
        except Exception:
            return
        rx = re.compile(r"pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+_eng_\d+_engtype_(\w*)", re.I)
        per_engine = defaultdict(float)   # (luid, type) -> somme
        per_pid = defaultdict(float)
        for line in out.splitlines():
            if "|" not in line:
                continue
            inst, val = line.rsplit("|", 1)
            m = rx.search(inst)
            if not m:
                continue
            try:
                val = float(val)
            except ValueError:
                continue
            pid, luid, etype = int(m.group(1)), m.group(2), m.group(3).lower()
            per_engine[(luid, etype)] += val
            per_pid[pid] = max(per_pid[pid], 0) + val
        luids = defaultdict(float)
        for (luid, _etype), v in per_engine.items():
            luids[luid] = max(luids[luid], v)
        self.engines = {k: min(100.0, v) for k, v in luids.items()}
        self.per_pid = {k: min(100.0, v) for k, v in per_pid.items()}

    def run(self):
        while True:
            self._poll_nvidia()
            self._poll_counters()
            time.sleep(2)


# --------------------------------------------------------------------------
# Collecteur principal
# --------------------------------------------------------------------------
class Monitor(threading.Thread):
    daemon = True

    def __init__(self, gpu):
        super().__init__()
        self.gpu = gpu
        self.lock = threading.Lock()
        self.snapshot = {}
        self.procs = {}           # pid -> psutil.Process (cache pour cpu_percent)
        self.prev_net = psutil.net_io_counters()
        self.prev_disk = psutil.disk_io_counters()
        self.prev_t = time.time()
        self.prev_pio = {}
        self.allowed_pids = set()
        psutil.cpu_percent(percpu=True)

    def _static_info(self):
        freq = psutil.cpu_freq()
        cpu_name = platform.processor()
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_Processor).Name"],
                                 capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout.strip()
            if out:
                cpu_name = out.splitlines()[0].strip()
        except Exception:
            pass
        return {
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "cpu_name": cpu_name,
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": CPU_COUNT,
            "cpu_max_freq": freq.max if freq else None,
            "boot_time": psutil.boot_time(),
            "user": current_user(),
        }

    def collect_processes(self, dt):
        windowed = pids_with_windows()
        prefs = gpu_pref.all_prefs()
        nv_pids = self.gpu.nvidia_pids
        groups = {}
        seen = set()
        for p in psutil.process_iter(["pid", "name", "exe", "username", "memory_info", "create_time", "num_threads"]):
            info = p.info
            if not is_user_app(info):
                continue
            pid = info["pid"]
            seen.add(pid)
            proc = self.procs.get(pid)
            if proc is None:
                proc = p
                self.procs[pid] = proc
                try:
                    proc.cpu_percent(None)
                except Exception:
                    pass
                cpu = 0.0
            else:
                try:
                    cpu = proc.cpu_percent(None) / CPU_COUNT
                except Exception:
                    cpu = 0.0
            # I/O disque par processus
            io_rate = 0.0
            try:
                io = proc.io_counters()
                tot = io.read_bytes + io.write_bytes
                prev = self.prev_pio.get(pid)
                if prev is not None and dt > 0:
                    io_rate = max(0.0, (tot - prev) / dt)
                self.prev_pio[pid] = tot
            except Exception:
                pass
            mem = info["memory_info"].rss if info["memory_info"] else 0
            name = info["name"]
            key = name.lower()
            g = groups.setdefault(key, {
                "name": re.sub(r"\.exe$", "", name, flags=re.I), "exe": info.get("exe") or "",
                "cpu": 0.0, "mem": 0, "gpu": 0.0, "io": 0.0, "threads": 0,
                "count": 0, "window": False, "pids": [], "children": [], "nvidia": False,
                "gpu_pref": prefs.get((info.get("exe") or "").lower(), 0),
                "started": info.get("create_time") or 0,
            })
            gpu = self.gpu.per_pid.get(pid, 0.0)
            g["cpu"] += cpu
            g["mem"] += mem
            g["gpu"] += gpu
            g["io"] += io_rate
            g["threads"] += info.get("num_threads") or 0
            g["count"] += 1
            g["window"] = g["window"] or pid in windowed
            g["nvidia"] = g["nvidia"] or pid in nv_pids
            g["pids"].append(pid)
            g["started"] = min(g["started"], info.get("create_time") or g["started"])
            g["children"].append({"pid": pid, "cpu": round(cpu, 1), "mem": mem,
                                  "gpu": round(gpu, 1), "io": io_rate, "window": pid in windowed})
        # nettoyage du cache
        for pid in list(self.procs):
            if pid not in seen:
                self.procs.pop(pid, None)
                self.prev_pio.pop(pid, None)
        self.allowed_pids = seen
        self.allowed_exes = {g["exe"].lower() for g in groups.values() if g["exe"]}
        result = []
        for g in groups.values():
            g["cpu"] = round(min(g["cpu"], 100), 1)
            g["gpu"] = round(min(g["gpu"], 100), 1)
            g["children"].sort(key=lambda c: -c["mem"])
            result.append(g)
        result.sort(key=lambda g: (-g["window"], -g["cpu"], -g["mem"]))
        return result

    def collect(self):
        now = time.time()
        dt = now - self.prev_t
        self.prev_t = now

        per_core = psutil.cpu_percent(percpu=True)
        cpu_total = sum(per_core) / len(per_core) if per_core else 0
        freq = psutil.cpu_freq()
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()

        net = psutil.net_io_counters()
        net_up = (net.bytes_sent - self.prev_net.bytes_sent) / dt if dt > 0 else 0
        net_down = (net.bytes_recv - self.prev_net.bytes_recv) / dt if dt > 0 else 0
        self.prev_net = net

        dio = psutil.disk_io_counters()
        d_read = d_write = 0
        if dio and self.prev_disk:
            d_read = (dio.read_bytes - self.prev_disk.read_bytes) / dt if dt > 0 else 0
            d_write = (dio.write_bytes - self.prev_disk.write_bytes) / dt if dt > 0 else 0
        self.prev_disk = dio

        disks = []
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts or not part.fstype:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            disks.append({"mount": part.mountpoint, "fs": part.fstype, "total": u.total,
                          "used": u.used, "free": u.free, "percent": u.percent})

        bat = None
        try:
            b = psutil.sensors_battery()
            if b:
                bat = {"percent": b.percent, "plugged": b.power_plugged,
                       "secsleft": b.secsleft if b.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else None}
        except Exception:
            pass

        nics = []
        try:
            stats = psutil.net_if_stats()
            addrs = psutil.net_if_addrs()
            for name, st in stats.items():
                if not st.isup or "loopback" in name.lower():
                    continue
                ipv4 = next((a.address for a in addrs.get(name, []) if a.family == socket.AF_INET), None)
                if ipv4 and not ipv4.startswith("169.254"):
                    nics.append({"name": name, "ip": ipv4, "speed": st.speed})
            virt = ("vethernet", "virtual", "vmware", "vbox", "hyper-v", "wsl", "docker", "tap", "openvpn", "tun")
            nics.sort(key=lambda n: any(v in n["name"].lower() for v in virt))
        except Exception:
            pass

        try:
            conns = len(psutil.net_connections(kind="inet"))
        except Exception:
            conns = None

        processes = self.collect_processes(dt)

        gpus = []
        for g in self.gpu.nvidia:
            gpus.append({**g, "vendor": "nvidia"})
        engines = [{"luid": k, "util": round(v, 1)} for k, v in sorted(self.gpu.engines.items())]

        snap = {
            "time": now,
            "uptime": now - psutil.boot_time(),
            "cpu": {"total": round(cpu_total, 1), "per_core": per_core,
                    "freq": freq.current if freq else None, "freq_max": freq.max if freq else None,
                    "ctx_switches": psutil.cpu_stats().ctx_switches},
            "memory": {"total": vm.total, "used": vm.used, "available": vm.available, "percent": vm.percent,
                       "swap_total": sw.total, "swap_used": sw.used, "swap_percent": sw.percent},
            "disks": disks,
            "disk_io": {"read": d_read, "write": d_write},
            "network": {"up": net_up, "down": net_down, "sent": net.bytes_sent, "recv": net.bytes_recv,
                        "interfaces": nics, "connections": conns},
            "gpu": {"nvidia": gpus, "engines": engines, "adapters": self.gpu.adapters},
            "battery": bat,
            "process_total": len(psutil.pids()),
            "processes": processes,
        }
        with self.lock:
            self.snapshot = snap

    def run(self):
        self.static = self._static_info()
        while True:
            try:
                self.collect()
            except Exception as e:  # on ne laisse jamais mourir le thread
                print("collect error:", e, file=sys.stderr)
            time.sleep(1.5)


def kill_pids(pids, allowed):
    results = []
    procs = []
    for pid in pids:
        ok = pid in allowed
        if not ok:
            try:
                ok = is_user_app(psutil.Process(pid).as_dict(["pid", "name", "exe", "username"]))
            except Exception:
                ok = False
        if not ok:
            results.append({"pid": pid, "ok": False, "error": "Processus protégé ou système"})
            continue
        try:
            p = psutil.Process(pid)
            p.terminate()
            procs.append(p)
        except psutil.NoSuchProcess:
            results.append({"pid": pid, "ok": True})
        except psutil.AccessDenied:
            results.append({"pid": pid, "ok": False, "error": "Accès refusé"})
        except Exception as e:
            results.append({"pid": pid, "ok": False, "error": str(e)})
    gone, alive = psutil.wait_procs(procs, timeout=3)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    for p in procs:
        results.append({"pid": p.pid, "ok": True})
    return results


gpu = GpuCollector()
monitor = Monitor(gpu)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---- sécurité -------------------------------------------------------
    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin in ALLOWED_ORIGINS

    def _host_ok(self):
        # bloque le « DNS rebinding » : un domaine pirate qui pointerait vers 127.0.0.1
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        if not (self._host_ok() and self._origin_ok()):
            return self._send(403, {"error": "forbidden"})
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PCMon")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- routes ---------------------------------------------------------
    def do_GET(self):
        if not (self._host_ok() and self._origin_ok()):
            return self._send(403, {"error": "forbidden"})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            if SITE_URL and "--local-ui" not in sys.argv:
                # l'interface officielle est le site : on y redirige
                self.send_response(302)
                self.send_header("Location", SITE_URL)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            with open(os.path.join(RES_DIR, "dashboard.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/ping":
            self._send(200, {"agent": "pc-monitor", "version": VERSION})
        elif path == "/api/stats":
            with monitor.lock:
                snap = monitor.snapshot
            self._send(200, snap)
        elif path == "/api/info":
            self._send(200, {**getattr(monitor, "static", {}), "admin": cleaner.is_admin(), "version": VERSION})
        elif path == "/api/clean/scan":
            self._send(200, {"items": cleaner.scan_junk(), "disk": cleaner.disk_summary()})
        elif path == "/api/large":
            m = re.search(r"min=(\d+)", self.path)
            self._send(200, cleaner.scan_large(int(m.group(1)) if m else 200))
        elif path == "/api/startup":
            self._send(200, {"items": cleaner.list_startup()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not (self._host_ok() and self._origin_ok()):
            return self._send(403, {"error": "forbidden"})
        # en-tête personnalisé : force une pré-vérification CORS, donc seuls les sites autorisés passent
        if self.headers.get("X-PCMon") != "1":
            return self._send(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad request"})
        if self.path == "/api/kill":
            pids = [int(x) for x in body.get("pids", [])]
            self._send(200, {"results": kill_pids(pids, set(monitor.allowed_pids))})
        elif self.path == "/api/clean":
            self._send(200, {"results": cleaner.clean_junk(body.get("ids", []))})
        elif self.path == "/api/ram":
            self._send(200, cleaner.free_ram())
        elif self.path == "/api/large/recycle":
            self._send(200, {"results": cleaner.recycle_large(body.get("paths", []))})
        elif self.path == "/api/reveal":
            self._send(200, {"ok": cleaner.reveal(body.get("path", ""))})
        elif self.path == "/api/startup":
            self._send(200, cleaner.set_startup(str(body.get("id", "")), bool(body.get("enabled"))))
        elif self.path == "/api/gpu/pref":
            exe = str(body.get("exe", ""))
            if exe.lower() not in getattr(monitor, "allowed_exes", set()):
                return self._send(200, {"ok": False, "error": "Application non autorisée"})
            self._send(200, gpu_pref.set_pref(exe, int(body.get("pref", 0))))
        elif self.path == "/api/gpu/restart":
            allowed = set(monitor.allowed_pids)
            pids = [int(x) for x in body.get("pids", [])]
            self._send(200, gpu_pref.restart(pids, lambda pid: pid in allowed))
        elif self.path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Timer(0.3, lambda: os._exit(0)).start()
        else:
            self._send(404, {"error": "not found"})


def already_running():
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


def main():
    url = SITE_URL or f"http://{HOST}:{PORT}/"
    if "--local-ui" in sys.argv:
        url = f"http://{HOST}:{PORT}/"
    if already_running():  # une seule instance : on ouvre simplement l'interface
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    gpu.start()
    monitor.start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Agent PC Monitor {VERSION} en écoute sur http://{HOST}:{PORT}  ->  interface : {url}")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
