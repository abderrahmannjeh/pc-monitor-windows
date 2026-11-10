"""
Choix du GPU par application (même réglage que Paramètres > Affichage > Graphiques).
Windows stocke la préférence dans HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences :
    nom de la valeur = chemin complet de l'exe, données = "GpuPreference=N;"
    N = 0 : Windows décide · 1 : économie d'énergie (iGPU Intel) · 2 : hautes performances (NVIDIA)
La préférence s'applique au prochain lancement : une application en cours ne peut pas changer de GPU,
d'où l'option de redémarrage.
"""
import os
import re
import subprocess
import time
import winreg

import psutil

KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
LABELS = {0: "Windows décide", 1: "Économie d'énergie", 2: "Hautes performances"}


def get_pref(exe):
    if not exe:
        return 0
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            val, _ = winreg.QueryValueEx(k, exe)
        m = re.search(r"GpuPreference=(\d)", str(val))
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def all_prefs():
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                m = re.search(r"GpuPreference=(\d)", str(val))
                out[name.lower()] = int(m.group(1)) if m else 0
    except OSError:
        pass
    return out


def set_pref(exe, pref):
    if pref not in (0, 1, 2) or not exe or not os.path.isfile(exe):
        return {"ok": False, "error": "Programme introuvable"}
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            if pref == 0:
                try:
                    winreg.DeleteValue(k, exe)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(k, exe, 0, winreg.REG_SZ, f"GpuPreference={pref};")
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def _main_process(pids):
    """Processus « racine » du groupe : celui dont le parent n'est pas dans le groupe (le plus ancien)."""
    procs = []
    for pid in pids:
        try:
            procs.append(psutil.Process(pid))
        except psutil.Error:
            pass
    roots = []
    for p in procs:
        try:
            if p.ppid() not in pids:
                roots.append(p)
        except psutil.Error:
            pass
    cand = roots or procs
    cand.sort(key=lambda p: p.create_time())
    return cand[0] if cand else None


def restart(pids, is_allowed):
    """Ferme toutes les instances de l'application puis la relance avec la même ligne de commande."""
    pids = [p for p in pids if is_allowed(p)]
    main = _main_process(set(pids))
    if not main:
        return {"ok": False, "error": "Application introuvable"}
    try:
        exe, cmd, cwd = main.exe(), main.cmdline(), main.cwd()
    except psutil.Error as e:
        return {"ok": False, "error": f"Accès refusé ({e.__class__.__name__})"}
    if "\\windowsapps\\" in exe.lower():
        return {"ok": False, "error": "Application du Microsoft Store : redémarrez-la vous-même"}
    procs = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            p.terminate()
            procs.append(p)
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=3)
    time.sleep(0.5)
    try:
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd or [exe], cwd=cwd if os.path.isdir(cwd) else None, close_fds=True, creationflags=flags)
    except OSError as e:
        return {"ok": False, "error": f"Relance impossible : {e}"}
    return {"ok": True}
