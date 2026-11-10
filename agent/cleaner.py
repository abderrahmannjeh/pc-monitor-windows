"""
Fonctions de nettoyage / optimisation (style « CleanMyMac » pour Windows) :
  - analyse et suppression des fichiers inutiles (temp, caches navigateurs/applis, corbeille, dumps…)
  - libération de la RAM (réduction des working sets)
  - recherche des gros fichiers (envoyés à la corbeille, jamais supprimés définitivement)
  - gestion des programmes au démarrage (activation / désactivation réversible)
"""
import ctypes
import ctypes.wintypes as wt
import glob
import os
import shutil
import struct
import subprocess
import time
import winreg

import psutil

LOCAL = os.environ.get("LOCALAPPDATA", "")
ROAMING = os.environ.get("APPDATA", "")
WINDIR = os.environ.get("WINDIR", r"C:\Windows")
PROGDATA = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
HOME = os.path.expanduser("~")
TEMP = os.environ.get("TEMP", os.path.join(LOCAL, "Temp"))
MIN_AGE = 3600  # on ne touche pas aux fichiers temporaires de moins d'1 h


def _chromium(base):
    """Dossiers de cache d'un navigateur Chromium (tous profils)."""
    out = []
    for prof in glob.glob(os.path.join(base, "*")):
        for sub in ("Cache", "Code Cache", "GPUCache", "Service Worker\\CacheStorage", "DawnCache", "GrShaderCache"):
            out.append(os.path.join(prof, sub))
    for sub in ("ShaderCache", "GrShaderCache", "GraphiteDawnCache"):
        out.append(os.path.join(base, sub))
    return out


def _electron(base):
    return [os.path.join(base, s) for s in ("Cache", "Code Cache", "GPUCache", "CachedData", "Service Worker\\CacheStorage", "logs")]


CATEGORIES = [
    {"id": "user_temp", "group": "Système", "name": "Fichiers temporaires utilisateur",
     "desc": "Fichiers laissés par les installations et les applications", "paths": [TEMP], "min_age": True},
    {"id": "win_temp", "group": "Système", "name": "Fichiers temporaires Windows",
     "desc": "Nécessite souvent les droits administrateur", "paths": [os.path.join(WINDIR, "Temp")], "min_age": True},
    {"id": "recycle", "group": "Système", "name": "Corbeille", "desc": "Fichiers supprimés en attente", "special": "recycle"},
    {"id": "thumbs", "group": "Système", "name": "Cache des miniatures",
     "desc": "Aperçus d'images régénérés automatiquement",
     "files": [os.path.join(LOCAL, "Microsoft\\Windows\\Explorer\\thumbcache_*.db"),
               os.path.join(LOCAL, "Microsoft\\Windows\\Explorer\\iconcache_*.db")]},
    {"id": "crash", "group": "Système", "name": "Rapports d'erreurs et dumps",
     "desc": "Rapports de plantage (WER) et fichiers .dmp",
     "paths": [os.path.join(LOCAL, "CrashDumps"), os.path.join(LOCAL, "Microsoft\\Windows\\WER"),
               os.path.join(PROGDATA, "Microsoft\\Windows\\WER"), os.path.join(WINDIR, "Minidump")]},
    {"id": "wu", "group": "Système", "name": "Cache Windows Update",
     "desc": "Mises à jour déjà installées (admin requis)",
     "paths": [os.path.join(WINDIR, "SoftwareDistribution\\Download")]},
    {"id": "logs", "group": "Système", "name": "Journaux Windows",
     "desc": "Anciens fichiers .log (admin requis)",
     "files": [os.path.join(WINDIR, "Logs\\CBS\\*.log"), os.path.join(WINDIR, "Panther\\*.log")], "min_age": True},
    {"id": "shader", "group": "Système", "name": "Cache de shaders (DirectX / GPU)",
     "desc": "Recréé automatiquement par les jeux et le GPU",
     "paths": [os.path.join(LOCAL, "D3DSCache"), os.path.join(LOCAL, "NVIDIA\\DXCache"),
               os.path.join(LOCAL, "NVIDIA\\GLCache"), os.path.join(LOCAL, "AMD\\DxCache"),
               os.path.join(LOCAL, "Intel\\ShaderCache")]},
    {"id": "edge", "group": "Navigateurs", "name": "Microsoft Edge", "desc": "Cache web (historique et mots de passe conservés)",
     "paths": _chromium(os.path.join(LOCAL, "Microsoft\\Edge\\User Data")), "app": "msedge.exe"},
    {"id": "chrome", "group": "Navigateurs", "name": "Google Chrome", "desc": "Cache web (historique et mots de passe conservés)",
     "paths": _chromium(os.path.join(LOCAL, "Google\\Chrome\\User Data")), "app": "chrome.exe"},
    {"id": "brave", "group": "Navigateurs", "name": "Brave", "desc": "Cache web",
     "paths": _chromium(os.path.join(LOCAL, "BraveSoftware\\Brave-Browser\\User Data")), "app": "brave.exe"},
    {"id": "firefox", "group": "Navigateurs", "name": "Firefox", "desc": "Cache web",
     "paths": glob.glob(os.path.join(LOCAL, "Mozilla\\Firefox\\Profiles\\*\\cache2")), "app": "firefox.exe"},
    {"id": "vscode", "group": "Applications", "name": "Visual Studio Code", "desc": "Caches et journaux",
     "paths": _electron(os.path.join(ROAMING, "Code")), "app": "Code.exe"},
    {"id": "slack", "group": "Applications", "name": "Slack", "desc": "Caches et journaux",
     "paths": _electron(os.path.join(ROAMING, "Slack")), "app": "slack.exe"},
    {"id": "teams", "group": "Applications", "name": "Microsoft Teams", "desc": "Caches",
     "paths": _electron(os.path.join(ROAMING, "Microsoft\\Teams")) +
     [os.path.join(LOCAL, "Packages\\MSTeams_8wekyb3d8bbwe\\LocalCache\\Microsoft\\MSTeams\\EBWebView\\Default\\Cache")],
     "app": "ms-teams.exe"},
    {"id": "discord", "group": "Applications", "name": "Discord", "desc": "Caches",
     "paths": _electron(os.path.join(ROAMING, "discord")), "app": "Discord.exe"},
    {"id": "spotify", "group": "Applications", "name": "Spotify", "desc": "Musique en cache",
     "paths": [os.path.join(LOCAL, "Spotify\\Storage"), os.path.join(LOCAL, "Spotify\\Data")], "app": "Spotify.exe"},
    {"id": "dev", "group": "Développeur", "name": "Caches pip / npm / yarn",
     "desc": "Paquets téléchargés, réinstallés au besoin",
     "paths": [os.path.join(LOCAL, "pip\\Cache"), os.path.join(LOCAL, "npm-cache"), os.path.join(ROAMING, "npm-cache"),
               os.path.join(LOCAL, "Yarn\\Cache"), os.path.join(LOCAL, "pnpm-cache")]},
]
CAT_BY_ID = {c["id"]: c for c in CATEGORIES}


# --------------------------------------------------------------------------
# Corbeille (API shell32)
# --------------------------------------------------------------------------
class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def recycle_info():
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
        return info.i64Size, info.i64NumItems
    return 0, 0


def empty_recycle():
    # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", wt.LPCWSTR), ("pTo", wt.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wt.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wt.LPCWSTR)]


def send_to_recycle(path):
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = path + "\0\0"
    op.fFlags = 0x40 | 0x10 | 0x400 | 0x4  # ALLOWUNDO | NOCONFIRMATION | NOERRORUI | SILENT
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0


# --------------------------------------------------------------------------
# Analyse / nettoyage des fichiers inutiles
# --------------------------------------------------------------------------
def _walk(root):
    """Parcours rapide : sous Windows, scandir fournit taille et date sans appel stat supplémentaire."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            yield e.path, st.st_size, st.st_mtime
                    except OSError:
                        pass
        except OSError:
            pass


def _iter_files(cat):
    """Renvoie (chemin, taille) des fichiers d'une catégorie."""
    now = time.time()
    min_age = cat.get("min_age")
    for pattern in cat.get("files", []):
        for f in glob.glob(pattern):
            try:
                st = os.stat(f)
            except OSError:
                continue
            if min_age and now - st.st_mtime < MIN_AGE:
                continue
            yield f, st.st_size
    for root in cat.get("paths", []):
        if not os.path.isdir(root):
            continue
        for fp, size, mtime in _walk(root):
            if min_age and now - mtime < MIN_AGE:
                continue
            yield fp, size


def scan_junk():
    out = []
    running = {(p.info["name"] or "").lower() for p in psutil.process_iter(["name"])}
    for cat in CATEGORIES:
        size = count = 0
        if cat.get("special") == "recycle":
            size, count = recycle_info()
        else:
            for _f, sz in _iter_files(cat):
                size += sz
                count += 1
        if size == 0 and cat.get("app") and not any(os.path.isdir(p) for p in cat.get("paths", [])):
            continue  # application non installée : on n'affiche pas
        out.append({"id": cat["id"], "group": cat["group"], "name": cat["name"], "desc": cat["desc"],
                    "size": size, "count": count,
                    "running": bool(cat.get("app")) and cat["app"].lower() in running})
    return out


def clean_junk(ids):
    results = []
    for cid in ids:
        cat = CAT_BY_ID.get(cid)
        if not cat:
            continue
        freed = deleted = failed = 0
        if cat.get("special") == "recycle":
            freed, deleted = recycle_info()
            empty_recycle()
        else:
            for f, sz in list(_iter_files(cat)):
                try:
                    os.remove(f)
                    freed += sz
                    deleted += 1
                except OSError:
                    failed += 1  # fichier verrouillé ou accès refusé : ignoré
            # supprime les sous-dossiers devenus vides (sans toucher au dossier racine)
            for root in cat.get("paths", []):
                if not os.path.isdir(root):
                    continue
                for dirpath, dirs, files in os.walk(root, topdown=False):
                    if dirpath != root and not os.listdir(dirpath):
                        try:
                            os.rmdir(dirpath)
                        except OSError:
                            pass
        results.append({"id": cid, "freed": freed, "deleted": deleted, "failed": failed})
    return results


# --------------------------------------------------------------------------
# RAM
# --------------------------------------------------------------------------
def free_ram():
    """Réduit le working set de chaque processus accessible (équivalent « purge »).
    Windows recharge à la demande les pages réellement utilisées."""
    before = psutil.virtual_memory().available
    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    k32.OpenProcess.restype = wt.HANDLE
    ok = 0
    for pid in psutil.pids():
        if pid in (0, 4):
            continue
        h = k32.OpenProcess(0x0100 | 0x0400, False, pid)  # SET_QUOTA | QUERY_INFORMATION
        if h:
            if psapi.EmptyWorkingSet(h):
                ok += 1
            k32.CloseHandle(h)
    time.sleep(1.0)
    after = psutil.virtual_memory().available
    return {"before": before, "after": after, "freed": max(0, after - before), "processes": ok}


# --------------------------------------------------------------------------
# Gros fichiers
# --------------------------------------------------------------------------
_last_large = set()
SKIP_DIRS = {"appdata", "$recycle.bin", "node_modules", ".git", "windows", "program files", "program files (x86)"}


def scan_large(min_mb=200, limit=60, budget=25):
    global _last_large
    found = []
    t0 = time.time()
    threshold = min_mb * 1048576
    stack = [HOME]
    while stack and time.time() - t0 <= budget:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name.lower() not in SKIP_DIRS and not e.name.startswith("."):
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            if st.st_size >= threshold:
                                found.append({"path": e.path, "name": e.name, "size": st.st_size, "mtime": st.st_mtime,
                                              "ext": os.path.splitext(e.name)[1].lower().lstrip(".")})
                    except OSError:
                        pass
        except OSError:
            pass
    found.sort(key=lambda f: -f["size"])
    found = found[:limit]
    _last_large = {f["path"] for f in found}
    return {"files": found, "partial": time.time() - t0 > budget}


def recycle_large(paths):
    res = []
    for p in paths:
        if p not in _last_large:
            res.append({"path": p, "ok": False, "error": "Fichier non issu de l'analyse"})
            continue
        ok = send_to_recycle(p)
        if ok:
            _last_large.discard(p)
        res.append({"path": p, "ok": ok and not os.path.exists(p)})
    return res


def reveal(path):
    if path in _last_large and os.path.exists(path):
        subprocess.Popen(["explorer", "/select,", path])
        return True
    return False


# --------------------------------------------------------------------------
# Programmes au démarrage
# --------------------------------------------------------------------------
RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_RUN = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
APPROVED_FOLDER = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"
STARTUP_DIRS = [(winreg.HKEY_CURRENT_USER, os.path.join(ROAMING, r"Microsoft\Windows\Start Menu\Programs\Startup")),
                (winreg.HKEY_LOCAL_MACHINE, os.path.join(PROGDATA, r"Microsoft\Windows\Start Menu\Programs\StartUp"))]


def _approved(hive, key, name):
    try:
        with winreg.OpenKey(hive, key) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return not (val and val[0] & 1)  # octet impair = désactivé
    except OSError:
        return True


def list_startup():
    items = []
    for hive, label in ((winreg.HKEY_CURRENT_USER, "Utilisateur"), (winreg.HKEY_LOCAL_MACHINE, "Tous les utilisateurs")):
        try:
            with winreg.OpenKey(hive, RUN) as k:
                i = 0
                while True:
                    try:
                        name, cmd, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    items.append({"id": f"reg|{int(hive == winreg.HKEY_LOCAL_MACHINE)}|{name}", "name": name,
                                  "command": str(cmd), "scope": label, "source": "Registre",
                                  "enabled": _approved(hive, APPROVED_RUN, name)})
        except OSError:
            pass
    for hive, folder in STARTUP_DIRS:
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                if f.lower() == "desktop.ini":
                    continue
                items.append({"id": f"dir|{int(hive == winreg.HKEY_LOCAL_MACHINE)}|{f}",
                              "name": os.path.splitext(f)[0], "command": os.path.join(folder, f),
                              "scope": "Utilisateur" if hive == winreg.HKEY_CURRENT_USER else "Tous les utilisateurs",
                              "source": "Dossier Démarrage", "enabled": _approved(hive, APPROVED_FOLDER, f)})
    return items


def set_startup(item_id, enabled):
    kind, machine, name = item_id.split("|", 2)
    hive = winreg.HKEY_LOCAL_MACHINE if machine == "1" else winreg.HKEY_CURRENT_USER
    key = APPROVED_RUN if kind == "reg" else APPROVED_FOLDER
    # même format que le Gestionnaire des tâches : 02 = activé, 03 = désactivé, + FILETIME
    ft = int((time.time() + 11644473600) * 10_000_000)
    data = struct.pack("<I", 2 if enabled else 3) + struct.pack("<Q", 0 if enabled else ft)
    try:
        with winreg.CreateKeyEx(hive, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, data)
        return {"ok": True}
    except PermissionError:
        return {"ok": False, "error": "Droits administrateur requis"}
    except OSError as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------
# Divers
# --------------------------------------------------------------------------
def disk_summary():
    total, used, free = shutil.disk_usage(os.environ.get("SYSTEMDRIVE", "C:") + "\\")
    return {"total": total, "used": used, "free": free}


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
