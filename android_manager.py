#!/usr/bin/env python3
"""
Android Device Manager
----------------------
Manages Android devices via ADB: USB ↔ TCP/IP switching,
charging control, scrcpy mirroring, and Wi-Fi connection management.

Features:
  • Auto-detect all connected ADB devices
  • Interactive device selector
  • Switch to TCP/IP (wireless) mode — disables charging while cable remains plugged in
  • Switch back to USB mode at any time
  • Launch scrcpy for any / all connected devices
  • Connect device to Wi-Fi: WPA/WPA2-Personal, WPA2/WPA3-Enterprise (EAP/PEAP/TTLS)
  • Saved Wi-Fi profiles (~/.android_wifi_profiles.json) with obfuscated passwords
  • Scan nearby SSIDs from the device
  • Persistent state tracking across sessions (~/.android_manager_state.json)
"""

import os
import sys
import json
import time
import shutil
import subprocess
import argparse
import getpass
import base64
import signal
import platform
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────
# ANSI colour helpers
# ─────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"

USE_COLOR = (sys.stdout.isatty() and
             (os.name != "nt" or
              os.environ.get("ANSICON") or
              os.environ.get("WT_SESSION") or
              os.environ.get("TERM") not in (None, "dumb")))


def c(text, *codes):
    if not USE_COLOR:
        return str(text)
    return "".join(codes) + str(text) + RESET


def ok(msg): print(c("  ✔  ", GREEN, BOLD) + msg)
def err(msg): print(c("  ✘  ", RED, BOLD) + msg)
def warn(msg): print(c("  ⚠  ", YELLOW, BOLD) + msg)
def info(msg): print(c("  ℹ  ", CYAN, BOLD) + msg)
def step(msg): print(c("  ➜  ", BLUE, BOLD) + msg)
def hdr(msg): print(c(f"\n  ── {msg} ──", MAGENTA, BOLD))


# ─────────────────────────────────────────────
# Persistent files
# ─────────────────────────────────────────────
STATE_FILE = Path.home() / ".android_manager_state.json"
PROFILE_FILE = Path.home() / ".android_wifi_profiles.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ─────────────────────────────────────────────
# Wi-Fi Profile Store
# ─────────────────────────────────────────────


def _b64(s: str) -> str:
    """Trivially obfuscate passwords stored in JSON (not cryptographic)."""
    return base64.b64encode(s.encode()).decode()


def _unb64(s: str) -> str:
    try:
        return base64.b64decode(s.encode()).decode()
    except Exception:
        return s


def load_profiles() -> list:
    if PROFILE_FILE.exists():
        try:
            raw = json.loads(PROFILE_FILE.read_text())
            for p in raw:
                if p.get("_b64_password"):
                    p["password"] = _unb64(p["_b64_password"])
                if p.get("_b64_eap_password"):
                    p["eap_password"] = _unb64(p["_b64_eap_password"])
            return raw
        except Exception:
            pass
    return []


def save_profiles(profiles: list):
    out = []
    for p in profiles:
        entry = {k: v for k, v in p.items()
                 if k not in ("password", "eap_password")}
        if p.get("password"):
            entry["_b64_password"] = _b64(p["password"])
        if p.get("eap_password"):
            entry["_b64_eap_password"] = _b64(p["eap_password"])
        out.append(entry)
    PROFILE_FILE.write_text(json.dumps(out, indent=2))
    if os.name == "posix":
        PROFILE_FILE.chmod(0o600)


def find_profile(name: str) -> Optional[dict]:
    for p in load_profiles():
        if (p.get("name", "").lower() == name.lower() or
                p.get("ssid", "").lower() == name.lower()):
            return p
    return None

# ─────────────────────────────────────────────
# ADB helpers
# ─────────────────────────────────────────────


def adb(*args, serial: str = None, capture: bool = True,
        timeout: int = 20) -> subprocess.CompletedProcess:
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)


def adb_shell(serial: str, *cmd_args, capture: bool = True) -> str:
    result = adb("shell", *cmd_args, serial=serial, capture=capture)
    return result.stdout.strip() if capture else ""


def require_adb():
    if not shutil.which("adb"):
        err("adb not found in PATH.")
        print("  Install Android Platform Tools:")
        print(c("    https://developer.android.com/tools/releases/platform-tools", DIM))
        sys.exit(1)


def _run_install_command(cmd: list[str], description: str) -> bool:
    step(f"Installing scrcpy via {description}…")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode == 0:
        ok(f"scrcpy installed successfully via {description}.")
        return True
    warn(f"{description} install failed: {result.stderr.strip() or result.stdout.strip()}")
    return False


def _verify_scrcpy_available() -> bool:
    if shutil.which("scrcpy"):
        return True
    result = subprocess.run(["where", "scrcpy"],
                            capture_output=True, text=True, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def _get_latest_scrcpy_release() -> Optional[dict]:
    url = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "android-device-manager"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        warn(f"Could not fetch scrcpy release metadata: {exc}")
        return None


def _download_file(url: str, dest: Path) -> bool:
    try:
        step(f"Downloading {url} …")
        req = urllib.request.Request(
            url, headers={"User-Agent": "android-device-manager"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        print(
                            f"\r  {pct:3d}%  {downloaded // 1024} KB / {total // 1024} KB", end="", flush=True)
        if total:
            print()
        return True
    except Exception as exc:
        warn(f"Download failed: {exc}")
        return False


def _install_scrcpy_windows_manual(release: dict) -> bool:
    assets = release.get("assets", [])
    asset = next((a for a in assets if re.search(
        r"scrcpy-win64.*\.zip", a["name"], re.I)), None)
    if not asset:
        warn("No Windows ZIP asset available for scrcpy.")
        return False

    install_dir = Path(os.environ.get(
        "ProgramFiles", "C:\\Program Files")) / "scrcpy"
    install_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset["name"]
        if not _download_file(asset["browser_download_url"], archive):
            return False
        import zipfile
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        extracted_dir = next(Path(tmp).glob("scrcpy*"), None)
        if not extracted_dir or not extracted_dir.is_dir():
            warn("Could not find extracted scrcpy directory.")
            return False
        for item in extracted_dir.iterdir():
            target = install_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)

    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
        current, _ = winreg.QueryValueEx(key, "PATH")
        if str(install_dir).lower() not in current.lower():
            new_path = current + ";" + str(install_dir)
            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)
            os.environ["PATH"] = new_path
            ok("Added scrcpy install directory to user PATH; restart the terminal to use it permanently.")
        winreg.CloseKey(key)
    except Exception as exc:
        warn(f"Could not update PATH automatically: {exc}")

    return True


def _install_scrcpy_unix_manual(release: dict) -> bool:
    assets = release.get("assets", [])
    asset = next((a for a in assets if re.search(
        r"(linux|macos).*\.tar\.gz", a["name"], re.I)), None)
    if not asset:
        warn("No tarball asset available for scrcpy.")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset["name"]
        if not _download_file(asset["browser_download_url"], archive):
            return False
        run = subprocess.run(
            ["tar", "xzf", str(archive), "-C", tmp], capture_output=True, text=True)
        if run.returncode != 0:
            warn(f"Could not extract archive: {run.stderr.strip()}")
            return False
        binary = next(Path(tmp).rglob("scrcpy"), None)
        if not binary:
            warn("Could not find scrcpy binary in extracted archive.")
            return False
        dest = Path("/usr/local/bin/scrcpy")
        run = subprocess.run(["sudo", "cp", str(binary), str(
            dest)], capture_output=True, text=True)
        if run.returncode != 0:
            warn(f"Could not install binary: {run.stderr.strip()}")
            return False
        run = subprocess.run(
            ["sudo", "chmod", "+x", str(dest)], capture_output=True, text=True)
        if run.returncode != 0:
            warn(f"Could not set executable permissions: {run.stderr.strip()}")
            return False
    return True


def _install_scrcpy() -> bool:
    """Attempt to install scrcpy via platform-specific methods."""
    system = platform.system()

    try:
        if system == "Windows":
            if shutil.which("winget"):
                if _run_install_command([
                    "winget", "install",
                    "--id", "Genymobile.scrcpy",
                    "--exact",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ], "winget"):
                    if _verify_scrcpy_available():
                        return True
                    warn(
                        "winget installed scrcpy, but scrcpy.exe was not found in the current PATH.")
                warn(
                    "winget installation failed; falling back to direct GitHub ZIP install.")
            else:
                warn(
                    "winget is not available on this system; using direct GitHub ZIP install.")

        elif system == "Darwin":
            if shutil.which("brew"):
                if _run_install_command(["brew", "install", "scrcpy"], "Homebrew"):
                    return True

        elif system == "Linux":
            if shutil.which("apt-get"):
                step("Updating package lists…")
                subprocess.run(["sudo", "apt-get", "update"],
                               capture_output=True, text=True, timeout=300)
                if _run_install_command(["sudo", "apt-get", "install", "-y", "scrcpy"], "apt"):
                    return True
            if shutil.which("snap"):
                if _run_install_command(["sudo", "snap", "install", "scrcpy"], "snap"):
                    return True
            if shutil.which("flatpak"):
                if _run_install_command(["flatpak", "install", "-y", "flathub", "com.genymobile.scrcpy"], "flatpak"):
                    return True

    except subprocess.TimeoutExpired as e:
        warn(f"Installation attempt timed out: {e}")
    except Exception as e:
        warn(f"Installation attempt failed: {e}")

    release = _get_latest_scrcpy_release()
    if release:
        if system == "Windows":
            return _install_scrcpy_windows_manual(release)
        return _install_scrcpy_unix_manual(release)
    return False


def require_scrcpy() -> bool:
    if shutil.which("scrcpy"):
        return True

    warn("scrcpy not found in PATH.")
    print("  Attempting automatic installation…")
    if _install_scrcpy():
        if shutil.which("scrcpy"):
            ok("scrcpy is now available.")
            return True

    warn("Automatic scrcpy installation failed.")
    print("  Install scrcpy manually from:")
    print(c("    https://github.com/Genymobile/scrcpy", DIM))
    return False

# ─────────────────────────────────────────────
# Device discovery
# ─────────────────────────────────────────────


def get_devices() -> list:
    # First, try to reconnect any previously connected TCP/IP devices
    state = load_state()
    for usb_serial, info in list(state.items()):
        tcp_serial = info.get("tcp_serial")
        if tcp_serial:
            # Try to reconnect silently without blocking
            try:
                result = adb("connect", tcp_serial, timeout=5)
            except Exception:
                pass  # Fail silently, will be detected below

    result = adb("devices", "-l")
    devices = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or "offline" in line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        serial = parts[0]
        props = {}
        for token in parts[2:]:
            if ":" in token:
                k, _, v = token.partition(":")
                props[k] = v
        conn_type = "tcpip" if ":" in serial else "usb"
        model = props.get("model", "") or props.get("product", "") or serial
        devices.append({
            "serial":    serial,
            "status":    parts[1],
            "model":     model,
            "product":   props.get("product", ""),
            "transport": props.get("transport_id", ""),
            "conn_type": conn_type,
            "props":     props,
        })
    return devices


def enrich_device(d: dict) -> dict:
    serial = d["serial"]

    # IP via ip route
    ip_out = adb_shell(serial, "ip", "route")
    ip = ""
    for ln in ip_out.splitlines():
        if "src" in ln:
            parts = ln.split()
            idx = parts.index("src") + 1 if "src" in parts else -1
            if 0 < idx < len(parts):
                ip = parts[idx]
                break
    d["ip"] = ip

    # Battery / charging
    battery = adb_shell(serial, "dumpsys", "battery")
    charging = "unknown"
    level = "?"
    for ln in battery.splitlines():
        ln = ln.strip()
        if ln.startswith("AC powered:"):
            charging = "AC" if "true" in ln else charging
        elif ln.startswith("USB powered:") and "true" in ln:
            charging = "USB" if charging == "unknown" else charging + "+USB"
        elif ln.startswith("Wireless powered:") and "true" in ln:
            charging = "Wireless" if charging == "unknown" else charging + "+Wireless"
        elif ln.startswith("level:"):
            level = ln.split(":")[1].strip()
    d["charging"] = charging
    d["battery"] = level

    # Current Wi-Fi SSID
    d["ssid"] = _current_ssid(serial)
    return d

# ─────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────


def print_banner():
    print(f"""
{c('╔════════════════════════════════════════════════════════════╗', CYAN, BOLD)}
{c('║', CYAN, BOLD)}  {c('Android Device Manager', WHITE, BOLD)}  {c('USB↔TCP/IP · Wi-Fi · scrcpy', DIM)}  {c('║', CYAN, BOLD)}
{c('╚════════════════════════════════════════════════════════════╝', CYAN, BOLD)}""")


def print_device_table(devices: list, show_index: bool = True):
    if not devices:
        warn("No devices found. Check USB connections or run `adb start-server`.")
        return
    col_w = [4, 20, 14, 14, 18, 9, 10, 10]
    headers = ["#", "Model", "Serial", "IP",
               "Wi-Fi SSID", "Connect", "Bat%", "Charging"]
    sep = "─" * (sum(col_w) + len(col_w) * 3 + 1)
    print(c(sep, DIM))
    print("".join(c(f" {h:<{w}}", BOLD, WHITE)
          for h, w in zip(headers, col_w)))
    print(c(sep, DIM))
    for i, d in enumerate(devices):
        ct = d.get("conn_type", "usb")
        conn = (c(f" {'WIFI':<{col_w[5]}}", GREEN, BOLD) if ct == "tcpip"
                else c(f" {'USB':<{col_w[5]}}", YELLOW, BOLD))
        ch = d.get("charging", "?")
        ch_c = (RED if ch not in ("unknown", "none", "?", "disabled")
                else (DIM if ch == "disabled" else RESET))
        idx = c(f" {i+1:<{col_w[0]}}", CYAN, BOLD) if show_index else "  "
        print(
            idx
            + c(f" {d['model'][:col_w[1]]:<{col_w[1]}}", WHITE)
            + c(f" {d['serial'][:col_w[2]]:<{col_w[2]}}", DIM)
            + c(f" {d.get('ip', '')[:col_w[3]]:<{col_w[3]}}", CYAN)
            + c(f" {d.get('ssid', '')[:col_w[4]]:<{col_w[4]}}", MAGENTA)
            + conn
            + c(f" {d.get('battery', '?')+'%':<{col_w[6]}}", GREEN)
            + c(f" {ch:<{col_w[7]}}", ch_c)
        )
    print(c(sep, DIM))


def print_profile_table(profiles: list):
    if not profiles:
        info("No saved Wi-Fi profiles.  Use option [2] to add one.")
        return
    col_w = [4, 22, 22, 20, 18, 8]
    headers = ["#", "Profile Name", "SSID",
               "Security", "EAP / Identity", "Hidden"]
    sep = "─" * (sum(col_w) + len(col_w) * 3 + 1)
    print(c(sep, DIM))
    print("".join(c(f" {h:<{w}}", BOLD, WHITE)
          for h, w in zip(headers, col_w)))
    print(c(sep, DIM))
    for i, p in enumerate(profiles):
        sec = p.get("security", "WPA/WPA2-Personal")
        eap_id = p.get("eap_identity", "") or p.get("eap_method", "")
        hidden = "Yes" if p.get("hidden") else "No"
        print(
            c(f" {i+1:<{col_w[0]}}", CYAN, BOLD)
            + c(f" {p.get('name', p.get('ssid', ''))[:col_w[1]]:<{col_w[1]}}", WHITE)
            + c(f" {p.get('ssid', '')[:col_w[2]]:<{col_w[2]}}", YELLOW)
            + c(f" {sec[:col_w[3]]:<{col_w[3]}}", GREEN)
            + c(f" {eap_id[:col_w[4]]:<{col_w[4]}}", MAGENTA)
            + c(f" {hidden:<{col_w[5]}}", DIM)
        )
    print(c(sep, DIM))


# ─────────────────────────────────────────────
# Wi-Fi engine
# ─────────────────────────────────────────────
_API_CACHE: dict = {}


def _api_level(serial: str) -> int:
    if serial not in _API_CACHE:
        out = adb_shell(serial, "getprop", "ro.build.version.sdk")
        try:
            _API_CACHE[serial] = int(out.strip())
        except ValueError:
            _API_CACHE[serial] = 29
    return _API_CACHE[serial]

# ── Personal (WPA/WPA2/WPA3-SAE) ─────────────


def _wifi_connect_wpa(serial: str, ssid: str, password: str,
                      hidden: bool = False) -> bool:
    api = _api_level(serial)

    # Android 13+ — cmd wifi connect-network
    if api >= 33:
        hidden_flag = "-h " if hidden else ""
        cmd = f'cmd wifi connect-network "{ssid}" wpa2 {hidden_flag}"{password}"'
        step(f"[Android 13+] cmd wifi …")
        adb_shell(serial, cmd)
        if _wait_for_wifi(serial, ssid):
            return True

    # Android 10-12 — cmd wifi (slightly different syntax)
    if 29 <= api <= 32:
        step(f"[Android 10-12] cmd wifi …")
        out = adb_shell(serial,
                        f'cmd wifi connect-network "{ssid}" wpa2 "{password}"')
        if _wait_for_wifi(serial, ssid):
            return True

    # Fallback: wpa_supplicant block (needs root or open conf)
    step("Fallback: wpa_supplicant network block …")
    block = (
        'network={\n'
        f'    ssid="{ssid}"\n'
        f'    psk="{password}"\n'
        '    key_mgmt=WPA-PSK\n'
        f'    scan_ssid={1 if hidden else 0}\n'
        '}\n'
    )
    if _push_wpa_conf(serial, block, connect=True, ssid=ssid):
        return _wait_for_wifi(serial, ssid)

    # Last resort: open Wi-Fi settings UI
    step("Opening Wi-Fi settings on device …")
    adb_shell(serial,
              "am start -a android.settings.WIFI_SETTINGS")
    info("Wi-Fi settings opened. Connect manually if automatic methods failed.")
    return False

# ── Enterprise (WPA2/WPA3-EAP) ───────────────


def _wifi_connect_enterprise(serial: str, ssid: str,
                             eap_method: str,
                             phase2: str,
                             identity: str,
                             password: str,
                             anonymous_identity: str = "",
                             ca_cert: str = "",
                             client_cert: str = "",
                             hidden: bool = False) -> bool:
    api = _api_level(serial)
    step(
        f"Connecting Enterprise Wi-Fi ({eap_method}/{phase2}) → {c(ssid, CYAN)}")

    # Android 13+ has full enterprise support in cmd wifi
    if api >= 33:
        parts = [
            "cmd", "wifi", "connect-network",
            f'"{ssid}"', "wpa2",
            "-x", eap_method,
            "-u", f'"{identity}"',
            "-p", f'"{password}"',
        ]
        if phase2:
            parts += ["-P", phase2]
        if anonymous_identity:
            parts += ["-a", f'"{anonymous_identity}"']
        if hidden:
            parts += ["-h"]
        out = adb_shell(serial, " ".join(parts))
        step(f"cmd wifi → {out or '(no output)'}")
        if _wait_for_wifi(serial, ssid):
            return True
        warn("cmd wifi enterprise: may need manual CA cert trust on device.")

    # Fallback: wpa_supplicant block (root path)
    step("Attempting wpa_supplicant enterprise block (needs root) …")
    lines = [
        'network={',
        f'    ssid="{ssid}"',
        '    key_mgmt=WPA-EAP',
        f'    eap={eap_method.upper()}',
        f'    identity="{identity}"',
        f'    password="{password}"',
    ]
    if phase2:
        lines.append(f'    phase2="auth={phase2.upper()}"')
    if anonymous_identity:
        lines.append(f'    anonymous_identity="{anonymous_identity}"')
    lines.append(f'    scan_ssid={1 if hidden else 0}')
    lines.append('}')
    block = "\n".join(lines) + "\n"
    _push_wpa_conf(serial, block, connect=True, ssid=ssid)
    return _wait_for_wifi(serial, ssid)

# ── Helpers ───────────────────────────────────


def _push_wpa_conf(serial: str, block: str,
                   connect: bool = False, ssid: str = "") -> bool:
    tmp = "/data/local/tmp/_adb_wpa_net.conf"
    escaped = block.replace("'", "'\\''")
    adb_shell(serial, f"printf '%s' '{escaped}' > {tmp}")
    out = adb_shell(serial,
                    f"cat {tmp} >> /data/misc/wifi/wpa_supplicant.conf 2>/dev/null && "
                    f"wpa_cli reconfigure 2>/dev/null && echo ok || echo fail")
    if "ok" in out:
        ok("wpa_supplicant block added.")
        if connect and ssid:
            adb_shell(serial,
                      f'wpa_cli select_network '
                      f'$(wpa_cli list_networks | grep "{ssid}" | cut -f1) 2>/dev/null')
        return True
    warn(f"wpa_supplicant push: {out} (root may be required)")
    return False


def _wait_for_wifi(serial: str, expected_ssid: str,
                   timeout: int = 20, interval: float = 2.0) -> bool:
    step(
        f"Waiting for connection to {c(expected_ssid, CYAN)} (up to {timeout}s)…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = _current_ssid(serial)
        if current and expected_ssid in current:
            ok(f"Connected to {c(expected_ssid, GREEN, BOLD)}")
            return True
        # also check wpa_supplicant state
        state = adb_shell(serial, "getprop", "wifi.supplicant.state")
        if state.strip().upper() == "COMPLETED":
            current = _current_ssid(serial)
            if current and expected_ssid in current:
                ok(f"Connected to {c(expected_ssid, GREEN, BOLD)}")
                return True
        time.sleep(interval)
    warn(f"Timed out waiting for '{expected_ssid}'.")
    return False


def _current_ssid(serial: str) -> str:
    out = adb_shell(serial, "dumpsys", "wifi")
    for ln in out.splitlines():
        ln = ln.strip()
        if "mWifiInfo" in ln and "SSID:" in ln:
            for part in ln.split(","):
                if "SSID:" in part:
                    return part.split("SSID:")[1].strip().strip('"')
        if ln.startswith("SSID:"):
            return ln.split(":", 1)[1].strip().strip('"')
    return ""


def _scan_wifi(serial: str) -> list:
    step("Triggering Wi-Fi scan on device…")
    # Trigger a fresh scan
    adb_shell(serial, "cmd wifi start-scan 2>/dev/null || true")
    time.sleep(2)

    ssids = []

    # Method 1: cmd wifi list-scan-results (Android 9+)
    out = adb_shell(serial, "cmd wifi list-scan-results")
    for ln in out.splitlines():
        parts = ln.split()
        # Format: BSSID  Freq  Level  Flags  SSID...
        if len(parts) >= 5 and not ln.startswith("BSSID"):
            ssid = " ".join(parts[4:]).strip()
            if ssid and ssid not in ssids and ssid != "<hidden>":
                ssids.append(ssid)

    # Method 2: wpa_cli scan_results (fallback / root)
    if not ssids:
        out2 = adb_shell(serial, "wpa_cli scan_results 2>/dev/null")
        for ln in out2.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 5:
                ssid = parts[4].strip()
                if ssid and ssid not in ssids:
                    ssids.append(ssid)

    return ssids


def _disconnect_wifi(serial: str) -> bool:
    api = _api_level(serial)
    if api >= 33:
        adb_shell(serial, "cmd wifi disconnect")
        ok("Disconnect command sent.")
        return True
    adb_shell(serial, "svc wifi disable")
    time.sleep(1)
    adb_shell(serial, "svc wifi enable")
    ok("Wi-Fi toggled (legacy disconnect).")
    return True

# ─────────────────────────────────────────────
# Wi-Fi Profile CRUD helpers
# ─────────────────────────────────────────────


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    prompt_str = c(f"  {label}", BOLD)
    if default and not secret:
        prompt_str += c(f" [{default}]", DIM)
    prompt_str += ": "
    if secret:
        val = getpass.getpass(prompt_str).strip()
    else:
        val = input(prompt_str).strip()
    return val or default


def _choose(label: str, options: list, default: int = 0) -> str:
    print(c(f"\n  {label}:", BOLD))
    for i, o in enumerate(options):
        marker = c("►", CYAN, BOLD) if i == default else " "
        print(f"    {marker} [{i + 1}] {o}")
    while True:
        raw = input(c(f"  Choice [1-{len(options)}]: ", DIM)).strip()
        if not raw:
            return options[default]
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass


def _pick_index(label: str, count: int) -> Optional[int]:
    raw = input(c(f"  {label} [1-{count}]: ", BOLD)).strip()
    try:
        idx = int(raw) - 1
        if 0 <= idx < count:
            return idx
    except ValueError:
        pass
    warn("Invalid selection.")
    return None


def wifi_add_profile_interactive(existing: Optional[dict] = None) -> Optional[dict]:
    existing = existing or {}
    hdr("Edit Wi-Fi Profile" if existing else "New Wi-Fi Profile")
    name = _prompt("Profile name (e.g. 'Home', 'Office-5GHz')",
                   default=existing.get("name", ""))
    if not name:
        warn("Profile name cannot be empty.")
        return None
    ssid = _prompt("Wi-Fi SSID (network name)",
                   default=existing.get("ssid", name))
    hidden = _choose("Hidden SSID?", ["No", "Yes"],
                     default=1 if existing.get("hidden") else 0) == "Yes"
    security_options = [
        "WPA/WPA2-Personal",
        "WPA3-Personal (SAE)",
        "WPA2-Enterprise (EAP/PEAP)",
        "WPA3-Enterprise (EAP)",
        "Open (no password)",
    ]
    try:
        default_sec = security_options.index(existing.get("security"))
    except ValueError:
        default_sec = 0
    sec = _choose("Security type", security_options, default=default_sec)

    profile: dict = {"name": name, "ssid": ssid,
                     "hidden": hidden, "security": sec}

    if "Enterprise" in sec:
        eap_options = ["PEAP", "TTLS", "TLS", "PWD", "SIM", "AKA"]
        try:
            default_eap = eap_options.index(existing.get("eap_method", "PEAP"))
        except ValueError:
            default_eap = 0
        eap = _choose("EAP method", eap_options, default=default_eap)
        phase2 = ""
        if eap in ("PEAP", "TTLS"):
            phase2_options = ["MSCHAPV2", "GTC", "PAP", "CHAP", "MSCHAP"]
            try:
                default_phase2 = phase2_options.index(
                    existing.get("phase2", "MSCHAPV2"))
            except ValueError:
                default_phase2 = 0
            phase2 = _choose("Phase-2 / inner auth", phase2_options,
                             default=default_phase2)
        profile["eap_method"] = eap
        profile["phase2"] = phase2
        profile["eap_identity"] = _prompt("Identity (username / user@domain)",
                                          default=existing.get("eap_identity", ""))
        profile["eap_password"] = _prompt(
            "EAP password", default=existing.get("eap_password", ""), secret=True)
        profile["anonymous_identity"] = _prompt("Anonymous identity (optional)",
                                                default=existing.get("anonymous_identity", ""))
        profile["ca_cert_path"] = _prompt("CA certificate path on host (optional)",
                                          default=existing.get("ca_cert_path", ""))
        profile["client_cert_path"] = _prompt("Client cert .p12/.pfx path (optional)",
                                              default=existing.get("client_cert_path", ""))
    elif "Open" not in sec:
        profile["password"] = _prompt("Wi-Fi password",
                                      default=existing.get("password", ""),
                                      secret=True)

    ok(f"Profile '{c(name, CYAN)}' ready.")
    return profile

# ─────────────────────────────────────────────
# Connect via profile
# ─────────────────────────────────────────────


def _connect_profile(serial: str, profile: dict):
    ssid = profile.get("ssid", "")
    sec = profile.get("security", "WPA/WPA2-Personal")
    hidden = profile.get("hidden", False)

    hdr(f"Connecting to '{ssid}'  [{profile.get('name', ssid)}]")
    info(f"Security: {c(sec, YELLOW)}   Hidden: {c(str(hidden), DIM)}")

    if "Enterprise" in sec:
        success = _wifi_connect_enterprise(
            serial, ssid,
            eap_method=profile.get("eap_method", "PEAP"),
            phase2=profile.get("phase2", "MSCHAPV2"),
            identity=profile.get("eap_identity", ""),
            password=profile.get("eap_password", ""),
            anonymous_identity=profile.get("anonymous_identity", ""),
            ca_cert=profile.get("ca_cert_path", ""),
            client_cert=profile.get("client_cert_path", ""),
            hidden=hidden,
        )
    elif "Open" in sec:
        step(f"Connecting to open network {c(ssid, CYAN)}…")
        api = _api_level(serial)
        if api >= 33:
            adb_shell(serial, f'cmd wifi connect-network "{ssid}" open')
        else:
            adb_shell(serial, "svc wifi enable")
        success = _wait_for_wifi(serial, ssid)
    else:
        success = _wifi_connect_wpa(serial, ssid,
                                    profile.get("password", ""), hidden=hidden)

    if success:
        ok(f"Device connected to {c(ssid, GREEN, BOLD)}")
    else:
        err(f"Could not confirm connection to '{ssid}'.")
        info("Check credentials or complete the flow manually on the device.")

# ─────────────────────────────────────────────
# Wi-Fi Manager sub-menu
# ─────────────────────────────────────────────


def wifi_manage_profiles_menu():
    while True:
        profiles = load_profiles()
        hdr("Wi-Fi Manager")
        print_profile_table(profiles)
        print()
        print(c("    [1]", CYAN) + " Connect device using a saved profile")
        print(c("    [2]", CYAN) + " Add / save a new Wi-Fi profile")
        print(c("    [3]", CYAN) + " Edit a saved profile")
        print(c("    [4]", RED) + " Delete a saved profile")
        print(c("    [5]", YELLOW) + " Connect manually (one-time, not saved)")
        print(c("    [6]", MAGENTA) + " Scan nearby Wi-Fi networks on device")
        print(c("    [7]", DIM) + " Disconnect device from current Wi-Fi")
        print(c("    [b]", DIM) + " Back to main menu")
        print()

        try:
            choice = input(c("  Choose: ", BOLD)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice == "b":
            break

        # ── [1] Connect via saved profile ──────────────────
        elif choice == "1":
            if not profiles:
                warn("No profiles saved. Add one with option [2].")
            else:
                devices = get_devices()
                if not devices:
                    warn("No devices connected.")
                else:
                    print_device_table(devices)
                    dev = select_device(devices)
                    if dev:
                        print_profile_table(profiles)
                        idx = _pick_index("Select profile", len(profiles))
                        if idx is not None:
                            _connect_profile(dev["serial"], profiles[idx])

        # ── [2] Add profile ────────────────────────────────
        elif choice == "2":
            p = wifi_add_profile_interactive()
            if p:
                # Check for duplicate name
                existing_names = [x.get("name", "").lower() for x in profiles]
                if p["name"].lower() in existing_names:
                    ow = input(c(f"  Profile '{p['name']}' exists. Overwrite? [y/N]: ",
                               YELLOW)).strip().lower()
                    if ow == "y":
                        profiles = [x for x in profiles
                                    if x.get("name", "").lower() != p["name"].lower()]
                    else:
                        info("Profile not saved.")
                        try:
                            input(c("\n  Press Enter to continue…", DIM))
                        except (EOFError, KeyboardInterrupt):
                            pass
                        continue
                profiles.append(p)
                save_profiles(profiles)
                ok(f"Profile saved → {PROFILE_FILE}")

        # ── [3] Edit profile ───────────────────────────────
        elif choice == "3":
            if not profiles:
                warn("No profiles to edit.")
            else:
                print_profile_table(profiles)
                idx = _pick_index("Select profile to edit", len(profiles))
                if idx is not None:
                    info(
                        "Re-enter fields (press Enter to keep current value shown in brackets).")
                    old = profiles[idx]
                    info(
                        f"Editing: {c(old.get('name', old.get('ssid', '')), CYAN)}")
                    new = wifi_add_profile_interactive(existing=old)
                    if new:
                        profiles[idx] = new
                        save_profiles(profiles)
                        ok("Profile updated.")

        # ── [4] Delete profile ─────────────────────────────
        elif choice == "4":
            if not profiles:
                warn("No profiles to delete.")
            else:
                print_profile_table(profiles)
                idx = _pick_index("Select profile to delete", len(profiles))
                if idx is not None:
                    name = profiles[idx].get(
                        "name", profiles[idx].get("ssid", ""))
                    confirm = input(
                        c(f"  Delete '{name}'? [y/N]: ", RED)).strip().lower()
                    if confirm == "y":
                        profiles.pop(idx)
                        save_profiles(profiles)
                        ok(f"Profile '{name}' deleted.")

        # ── [5] Manual one-time connect ────────────────────
        elif choice == "5":
            devices = get_devices()
            if not devices:
                warn("No devices connected.")
            else:
                print_device_table(devices)
                dev = select_device(devices)
                if dev:
                    _wifi_connect_manual(dev["serial"])

        # ── [6] Scan ───────────────────────────────────────
        elif choice == "6":
            devices = get_devices()
            if not devices:
                warn("No devices connected.")
            else:
                print_device_table(devices)
                dev = select_device(devices)
                if dev:
                    ssids = _scan_wifi(dev["serial"])
                    if ssids:
                        hdr(f"Nearby Networks ({len(ssids)} found)")
                        for i, s in enumerate(ssids, 1):
                            print(f"    {c(str(i), CYAN, BOLD)}. {s}")
                        print()
                        ans = input(c("  Connect to one? Enter number or [n]: ",
                                      DIM)).strip()
                        if ans.isdigit():
                            i2 = int(ans) - 1
                            if 0 <= i2 < len(ssids):
                                _wifi_connect_manual(dev["serial"],
                                                     prefill_ssid=ssids[i2])
                    else:
                        warn("No SSIDs found (ensure Wi-Fi is enabled on device).")

        # ── [7] Disconnect ─────────────────────────────────
        elif choice == "7":
            devices = get_devices()
            if not devices:
                warn("No devices connected.")
            else:
                print_device_table(devices)
                dev = select_device(devices)
                if dev:
                    _disconnect_wifi(dev["serial"])

        else:
            warn("Unknown option.")

        try:
            input(c("\n  Press Enter to continue…", DIM))
        except (EOFError, KeyboardInterrupt):
            print()
            break


def _wifi_connect_manual(serial: str, prefill_ssid: str = ""):
    """Interactive one-time connect without saving a profile."""
    hdr("Manual Wi-Fi Connect")
    ssid = _prompt("SSID", default=prefill_ssid)
    if not ssid:
        return
    sec = _choose("Security type", [
        "WPA/WPA2-Personal",
        "WPA3-Personal (SAE)",
        "WPA2-Enterprise (EAP/PEAP)",
        "Open (no password)",
    ])
    hidden = _choose("Hidden SSID?", ["No", "Yes"]) == "Yes"

    profile: dict = {"name": ssid, "ssid": ssid,
                     "security": sec, "hidden": hidden}

    if "Enterprise" in sec:
        eap = _choose("EAP method", ["PEAP", "TTLS", "TLS", "PWD"])
        phase2 = ""
        if eap in ("PEAP", "TTLS"):
            phase2 = _choose("Phase-2", ["MSCHAPV2", "GTC", "PAP", "CHAP"])
        profile.update({
            "eap_method":         eap,
            "phase2":             phase2,
            "eap_identity":       _prompt("Identity (username)"),
            "eap_password":       _prompt("EAP password", secret=True),
            "anonymous_identity": _prompt("Anonymous identity (optional)"),
        })
    elif "Open" not in sec:
        profile["password"] = _prompt("Password", secret=True)

    save_q = input(
        c("  Save as a profile for later? [y/N]: ", DIM)).strip().lower()
    if save_q == "y":
        profs = load_profiles()
        profs.append(profile)
        save_profiles(profs)
        ok(f"Profile '{ssid}' saved.")

    _connect_profile(serial, profile)


# ─────────────────────────────────────────────
# Core USB ↔ TCP/IP operations  (unchanged)
# ─────────────────────────────────────────────
DEFAULT_TCPIP_PORT = 5555


def _verify_tcpip_connection(tcp_serial: str) -> bool:
    """Verify if a TCP/IP device is still reachable."""
    try:
        result = adb("shell", "echo ok", serial=tcp_serial, timeout=5)
        return result.returncode == 0 and "ok" in result.stdout
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def _reconnect_tcpip_devices():
    """Attempt to reconnect to previously connected TCP/IP devices after cable disconnect."""
    state = load_state()
    reconnected = []
    failed = []

    for usb_serial, info in list(state.items()):
        tcp_serial = info.get("tcp_serial")
        port = info.get("port", DEFAULT_TCPIP_PORT)

        if not tcp_serial:
            continue

        # Check if already connected
        devices = get_devices()
        if any(d["serial"] == tcp_serial for d in devices):
            ok(f"TCP/IP device already connected: {c(tcp_serial, GREEN)}")
            reconnected.append(tcp_serial)
            continue

        # Try to reconnect
        step(f"Reconnecting to {c(tcp_serial, CYAN)}…")
        try:
            result = adb("connect", tcp_serial, timeout=10)
            if "connected" in result.stdout.lower():
                ok(f"Reconnected: {c(tcp_serial, GREEN, BOLD)}")
                reconnected.append(tcp_serial)
            else:
                warn(
                    f"Failed to reconnect {tcp_serial}: {result.stdout.strip()}")
                failed.append(tcp_serial)
        except Exception as e:
            warn(f"Error reconnecting {tcp_serial}: {e}")
            failed.append(tcp_serial)

    return reconnected, failed


def enable_tcpip(serial: str, port: int = DEFAULT_TCPIP_PORT) -> Optional[str]:
    step(f"Enabling TCP/IP mode on {c(serial, CYAN)} (port {port})…")
    r = adb("tcpip", str(port), serial=serial)
    if r.returncode != 0:
        err(f"Failed to enable TCP/IP: {r.stderr.strip()}")
        return None
    time.sleep(1.5)
    ip = _get_device_ip(serial)
    if not ip:
        err("Could not determine device IP. Is it on Wi-Fi?")
        return None
    ok(f"Device IP: {c(ip, GREEN)}")
    tcp_serial = f"{ip}:{port}"
    step(f"Connecting to {c(tcp_serial, CYAN)} over Wi-Fi…")
    for attempt in range(1, 4):
        r = adb("connect", tcp_serial)
        if "connected" in r.stdout.lower():
            ok(f"Connected wirelessly: {c(tcp_serial, GREEN, BOLD)}")
            break
        warn(f"Attempt {attempt}/3 failed — retrying in 2s…")
        time.sleep(2)
    else:
        err(f"Could not connect to {tcp_serial}")
        return None
    _set_charging(tcp_serial, enable=False)
    return tcp_serial


def disable_tcpip(usb_serial: str, tcp_serial: str):
    step("Re-enabling charging…")
    target = tcp_serial if tcp_serial else usb_serial
    _set_charging(target, enable=True)
    if tcp_serial:
        step(f"Disconnecting TCP/IP session {c(tcp_serial, CYAN)}…")
        adb("disconnect", tcp_serial)
        ok("TCP/IP session closed.")
    if usb_serial:
        step(f"Restarting ADB in USB mode on {c(usb_serial, CYAN)}…")
        r = adb("usb", serial=usb_serial)
        if r.returncode == 0:
            ok("Device is back in USB mode.")
        else:
            warn(f"adb usb: {r.stderr.strip()}")


def _get_device_ip(serial: str) -> Optional[str]:
    out = adb_shell(serial, "ip", "-f", "inet", "addr", "show", "wlan0")
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("inet "):
            return ln.split()[1].split("/")[0]
    out = adb_shell(serial, "ip", "route")
    for ln in out.splitlines():
        if "wlan0" in ln and "src" in ln:
            parts = ln.split()
            try:
                return parts[parts.index("src") + 1]
            except (ValueError, IndexError):
                pass
    out = adb_shell(serial, "getprop", "dhcp.wlan0.ipaddress")
    return out.strip() if out else None


def _set_charging(serial: str, enable: bool):
    action = "Enabling" if enable else "Disabling"
    step(f"{action} charging on {c(serial, CYAN)}…")
    sysfs_paths = [
        "/sys/class/power_supply/battery/charging_enabled",
        "/sys/class/power_supply/battery/battery_charging_enabled",
        "/sys/class/power_supply/usb/charge_enable",
        "/sys/kernel/debug/usb/charge_enable",
    ]
    value = "1" if enable else "0"
    for path in sysfs_paths:
        check = adb_shell(
            serial, f"[ -f {path} ] && echo exists || echo missing")
        if "exists" in check:
            adb_shell(serial,
                      f"echo {value} > {path} 2>&1 || su -c 'echo {value} > {path}'")
            verify = adb_shell(serial, f"cat {path}")
            if verify.strip() == value:
                ok("Charging " + ("re-enabled." if enable
                   else "disabled — device won't charge while cable is connected."))
                return
    cmd = "reset" if enable else "set usb 0"
    adb_shell(serial, f"dumpsys battery {cmd}")
    if enable:
        ok("Charging state reset via dumpsys.")
    else:
        warn("Could not disable charging via sysfs (may need root).")
        warn("TCP/IP session is still active.")

# ─────────────────────────────────────────────
# scrcpy helpers
# ─────────────────────────────────────────────


def launch_scrcpy(serial: str, title: str = ""):
    if not require_scrcpy():
        return None
    cmd = ["scrcpy", "-s", serial, "--window-title", title or serial]
    step(f"Launching scrcpy for {c(serial, CYAN)}…")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    ok(f"scrcpy started (PID {proc.pid}).")
    return proc


def launch_scrcpy_all(devices: list):
    if not devices:
        warn("No devices to mirror.")
        return
    if not require_scrcpy():
        return
    for d in devices:
        launch_scrcpy(d["serial"],
                      title=f"{d['model']} ({d['conn_type'].upper()})")

# ─────────────────────────────────────────────
# Shared UI
# ─────────────────────────────────────────────


def select_device(devices: list) -> Optional[dict]:
    if not devices:
        return None
    if len(devices) == 1:
        info(f"Auto-selecting only device: {c(devices[0]['model'], CYAN)}")
        return devices[0]
    while True:
        try:
            raw = input(
                c(f"\n  Select device [1-{len(devices)}] (or q): ", BOLD))
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.strip().lower() == "q":
            return None
        try:
            idx = int(raw.strip()) - 1
            if 0 <= idx < len(devices):
                return devices[idx]
        except ValueError:
            pass
        warn("Invalid selection.")

# ─────────────────────────────────────────────
# Main interactive menu
# ─────────────────────────────────────────────


def interactive_menu():
    print_banner()
    state = load_state()

    while True:
        step("Scanning for devices…")
        devices = get_devices()
        for i, d in enumerate(devices):
            devices[i] = enrich_device(d)

        print()
        print_device_table(devices)
        print()
        print(c("  ── ADB / Mirroring ──────────────────────────────────", DIM))
        print(c("    [1]", CYAN) +
              " Select device → TCP/IP mode (disable charging)")
        print(c("    [2]", CYAN) + " Select device → Back to USB mode")
        print(c("    [3]", CYAN) + " Launch scrcpy on selected device")
        print(c("    [4]", CYAN) + " Launch scrcpy on ALL devices")
        print(c("    [6]", CYAN) +
              " Reconnect saved TCP/IP devices (after cable disconnect)")
        print(c("  ── Wi-Fi ────────────────────────────────────────────", DIM))
        print(c("    [w]", MAGENTA) +
              " Wi-Fi manager  (profiles · connect · scan · disconnect)")
        print(c("  ── Other ────────────────────────────────────────────", DIM))
        print(c("    [5]", CYAN) + " Refresh device list")
        print(c("    [q]", RED) + " Quit")
        print()

        try:
            choice = input(c("  Choose action: ", BOLD)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice == "q":
            break

        elif choice == "w":
            wifi_manage_profiles_menu()
            continue

        elif choice == "1":
            usb_devices = [d for d in devices if d["conn_type"] == "usb"]
            if not usb_devices:
                warn("No USB-connected devices available for TCP/IP switch.")
            else:
                print()
                print_device_table(usb_devices)
                dev = select_device(usb_devices)
                if dev:
                    port = DEFAULT_TCPIP_PORT
                    raw_port = input(
                        c(f"  TCP/IP port [{port}]: ", DIM)).strip()
                    if raw_port.isdigit():
                        port = int(raw_port)
                    tcp_serial = enable_tcpip(dev["serial"], port)
                    if tcp_serial:
                        state[dev["serial"]] = {
                            "tcp_serial": tcp_serial, "port": port}
                        save_state(state)
                        offer = input(c("  Launch scrcpy for this device? [Y/n]: ",
                                        DIM)).strip().lower()
                        if offer != "n":
                            time.sleep(1)
                            launch_scrcpy(tcp_serial,
                                          title=f"{dev['model']} (Wi-Fi)")

        elif choice == "2":
            print()
            print_device_table(devices)
            dev = select_device(devices)
            if dev:
                usb_serial = dev["serial"] if dev["conn_type"] == "usb" else None
                tcp_serial = None
                if dev["conn_type"] == "tcpip":
                    tcp_serial = dev["serial"]
                    for usb_s, info_d in state.items():
                        if info_d.get("tcp_serial") == tcp_serial:
                            usb_serial = usb_s
                            break
                else:
                    tcp_serial = state.get(dev["serial"], {}).get("tcp_serial")
                disable_tcpip(usb_serial, tcp_serial)
                if dev["serial"] in state:
                    del state[dev["serial"]]
                    save_state(state)

        elif choice == "3":
            print()
            print_device_table(devices)
            dev = select_device(devices)
            if dev:
                launch_scrcpy(dev["serial"],
                              title=f"{dev['model']} ({dev['conn_type'].upper()})")

        elif choice == "4":
            launch_scrcpy_all(devices)

        elif choice == "5":
            info("Refreshing…")
            continue

        elif choice == "6":
            hdr("Reconnecting TCP/IP Devices")
            reconnected, failed = _reconnect_tcpip_devices()
            if reconnected:
                ok(f"Successfully reconnected: {len(reconnected)} device(s)")
                for tcp_ser in reconnected:
                    print(f"  • {c(tcp_ser, GREEN)}")
            if failed:
                warn(f"Failed to reconnect: {len(failed)} device(s)")
                for tcp_ser in failed:
                    print(f"  • {c(tcp_ser, RED)}")
            if not reconnected and not failed:
                info("No TCP/IP devices in saved state.")

        else:
            warn("Unknown option.")

        try:
            input(c("\n  Press Enter to continue…", DIM))
        except (EOFError, KeyboardInterrupt):
            print()
            break

# ─────────────────────────────────────────────
# CLI (non-interactive / scriptable)
# ─────────────────────────────────────────────


def cli():
    parser = argparse.ArgumentParser(
        description="Android Device Manager — USB↔TCP/IP · Wi-Fi Profiles · scrcpy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python android_manager.py                                 # Interactive TUI
  python android_manager.py list                            # List devices + SSID
  python android_manager.py tcpip -s SERIAL                 # Enable wireless ADB
  python android_manager.py usb   -s SERIAL                 # Back to USB
  python android_manager.py mirror                          # scrcpy all devices

  # Personal Wi-Fi
  python android_manager.py wifi connect -s SERIAL \\
      --ssid MyNetwork --password s3cr3t

  # Enterprise Wi-Fi (PEAP/MSCHAPV2)
  python android_manager.py wifi connect -s SERIAL \\
      --ssid CorpNet --security "WPA2-Enterprise (EAP/PEAP)" \\
      --eap PEAP --phase2 MSCHAPV2 \\
      --identity alice@corp.com --eap-password s3cr3t

  # Scan / disconnect
  python android_manager.py wifi scan       -s SERIAL
  python android_manager.py wifi disconnect -s SERIAL

  # Saved profiles
  python android_manager.py wifi profiles
  python android_manager.py wifi connect-profile -s SERIAL --profile "Office"
        """
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list",   help="List connected devices")

    p_tcp = sub.add_parser(
        "tcpip",  help="Switch device to TCP/IP wireless ADB")
    p_tcp.add_argument("-s", "--serial")
    p_tcp.add_argument("-p", "--port", type=int, default=DEFAULT_TCPIP_PORT)
    p_tcp.add_argument("--no-scrcpy", action="store_true")

    p_usb = sub.add_parser("usb",    help="Switch device back to USB mode")
    p_usb.add_argument("-s", "--serial")

    p_mir = sub.add_parser("mirror", help="Launch scrcpy")
    p_mir.add_argument("-s", "--serial")

    # ── wifi ──────────────────────────────────
    p_wifi = sub.add_parser("wifi",  help="Wi-Fi management")
    wsub = p_wifi.add_subparsers(dest="wcmd")

    # wifi connect
    wc = wsub.add_parser("connect", help="Connect device to a Wi-Fi network")
    wc.add_argument("-s", "--serial", required=True)
    wc.add_argument("--ssid",     required=True)
    wc.add_argument("--password", default="",
                    help="Pre-shared key (Personal). Prompted if omitted.")
    wc.add_argument("--hidden",   action="store_true")
    wc.add_argument("--security", default="WPA/WPA2-Personal",
                    choices=["WPA/WPA2-Personal", "WPA3-Personal (SAE)",
                             "WPA2-Enterprise (EAP/PEAP)",
                             "WPA3-Enterprise (EAP)", "Open"])
    wc.add_argument("--eap",      dest="eap_method", default="PEAP",
                    choices=["PEAP", "TTLS", "TLS", "PWD", "SIM", "AKA"])
    wc.add_argument("--phase2",   default="MSCHAPV2",
                    choices=["MSCHAPV2", "GTC", "PAP", "CHAP", "MSCHAP"])
    wc.add_argument("--identity", default="",
                    help="EAP identity / username")
    wc.add_argument("--eap-password", default="", dest="eap_password",
                    help="EAP password. Prompted if omitted.")
    wc.add_argument("--anonymous-identity", default="",
                    dest="anonymous_identity")

    # wifi disconnect
    wd = wsub.add_parser("disconnect", help="Disconnect device from Wi-Fi")
    wd.add_argument("-s", "--serial", required=True)

    # wifi scan
    ws = wsub.add_parser("scan", help="Scan nearby Wi-Fi networks on device")
    ws.add_argument("-s", "--serial", required=True)

    # wifi profiles
    wsub.add_parser("profiles", help="List all saved Wi-Fi profiles")

    # wifi add-profile (non-interactive via CLI flags)
    wap = wsub.add_parser(
        "add-profile", help="Add / update a saved Wi-Fi profile")
    wap.add_argument("--name",     required=True)
    wap.add_argument("--ssid",     required=True)
    wap.add_argument("--password", default="")
    wap.add_argument("--hidden",   action="store_true")
    wap.add_argument("--security", default="WPA/WPA2-Personal")
    wap.add_argument("--eap",      dest="eap_method", default="PEAP")
    wap.add_argument("--phase2",   default="MSCHAPV2")
    wap.add_argument("--identity", default="")
    wap.add_argument("--eap-password", default="", dest="eap_password")
    wap.add_argument("--anonymous-identity", default="",
                     dest="anonymous_identity")

    # wifi connect-profile
    wcp = wsub.add_parser("connect-profile",
                          help="Connect using a saved profile by name")
    wcp.add_argument("-s", "--serial", required=True)
    wcp.add_argument("--profile",   required=True, help="Profile name or SSID")

    args = parser.parse_args()
    require_adb()

    if args.cmd is None:
        interactive_menu()
        return

    # ── list ──────────────────────────────────────────────
    if args.cmd == "list":
        devices = get_devices()
        for i, d in enumerate(devices):
            devices[i] = enrich_device(d)
        print_banner()
        print_device_table(devices)

    # ── tcpip ─────────────────────────────────────────────
    elif args.cmd == "tcpip":
        devices = get_devices()
        serial = args.serial
        if not serial:
            usb_devs = [d for d in devices if d["conn_type"] == "usb"]
            if not usb_devs:
                err("No USB devices found.")
                sys.exit(1)
            if len(usb_devs) == 1:
                serial = usb_devs[0]["serial"]
            else:
                for i, d in enumerate(usb_devs):
                    usb_devs[i] = enrich_device(d)
                print_device_table(usb_devs)
                d = select_device(usb_devs)
                serial = d["serial"] if d else None
        if not serial:
            sys.exit(1)
        state = load_state()
        tcp_serial = enable_tcpip(serial, args.port)
        if tcp_serial:
            state[serial] = {"tcp_serial": tcp_serial, "port": args.port}
            save_state(state)
            if not args.no_scrcpy:
                dev = next((d for d in devices if d["serial"] == serial), {})
                time.sleep(1)
                launch_scrcpy(tcp_serial,
                              title=f"{dev.get('model', serial)} (Wi-Fi)")

    # ── usb ───────────────────────────────────────────────
    elif args.cmd == "usb":
        state = load_state()
        devices = get_devices()
        serial = args.serial
        if not serial:
            print_device_table(devices)
            d = select_device(devices)
            serial = d["serial"] if d else None
        if not serial:
            sys.exit(1)
        dev = next((d for d in devices if d["serial"] == serial),
                   {"conn_type": "usb"})
        if dev["conn_type"] == "tcpip":
            tcp_serial = serial
            usb_serial = None
            for usb_s, info_d in state.items():
                if info_d.get("tcp_serial") == tcp_serial:
                    usb_serial = usb_s
                    break
        else:
            usb_serial = serial
            tcp_serial = state.get(serial, {}).get("tcp_serial")
        disable_tcpip(usb_serial, tcp_serial)
        if usb_serial in state:
            del state[usb_serial]
            save_state(state)

    # ── mirror ────────────────────────────────────────────
    elif args.cmd == "mirror":
        devices = get_devices()
        if args.serial:
            launch_scrcpy(args.serial)
        else:
            launch_scrcpy_all(devices)

    # ── wifi ──────────────────────────────────────────────
    elif args.cmd == "wifi":
        wcmd = getattr(args, "wcmd", None)

        if wcmd == "connect":
            sec = args.security
            if "Enterprise" in sec:
                eap_pwd = args.eap_password or getpass.getpass(
                    "  EAP password: ")
                _wifi_connect_enterprise(
                    args.serial, args.ssid,
                    eap_method=args.eap_method,
                    phase2=args.phase2,
                    identity=args.identity,
                    password=eap_pwd,
                    anonymous_identity=args.anonymous_identity,
                    hidden=args.hidden,
                )
            elif "Open" in sec:
                api = _api_level(args.serial)
                if api >= 33:
                    adb_shell(
                        args.serial, f'cmd wifi connect-network "{args.ssid}" open')
                else:
                    adb_shell(args.serial, "svc wifi enable")
                _wait_for_wifi(args.serial, args.ssid)
            else:
                pwd = args.password or getpass.getpass("  Wi-Fi password: ")
                _wifi_connect_wpa(args.serial, args.ssid,
                                  pwd, hidden=args.hidden)

        elif wcmd == "disconnect":
            _disconnect_wifi(args.serial)

        elif wcmd == "scan":
            ssids = _scan_wifi(args.serial)
            if ssids:
                print(c(f"\n  Found {len(ssids)} networks:", BOLD))
                for s in ssids:
                    print(f"    • {s}")
            else:
                warn("No SSIDs found.")

        elif wcmd == "profiles":
            print_banner()
            print_profile_table(load_profiles())

        elif wcmd == "add-profile":
            profiles = load_profiles()
            p: dict = {
                "name":               args.name,
                "ssid":               args.ssid,
                "hidden":             args.hidden,
                "security":           args.security,
                "password":           args.password or
                (getpass.getpass("  Wi-Fi password: ")
                 if "Enterprise" not in args.security
                 and "Open" not in args.security else ""),
                "eap_method":         args.eap_method,
                "phase2":             args.phase2,
                "eap_identity":       args.identity,
                "eap_password":       args.eap_password or
                (getpass.getpass("  EAP password: ")
                 if "Enterprise" in args.security else ""),
                "anonymous_identity": args.anonymous_identity,
            }
            # Remove empty keys
            p = {k: v for k, v in p.items() if v not in (None, "")}
            # Replace existing
            profiles = [x for x in profiles
                        if x.get("name", "").lower() != args.name.lower()]
            profiles.append(p)
            save_profiles(profiles)
            ok(f"Profile '{args.name}' saved → {PROFILE_FILE}")

        elif wcmd == "connect-profile":
            profile = find_profile(args.profile)
            if not profile:
                err(f"Profile '{args.profile}' not found.")
                info(f"Run: python android_manager.py wifi profiles")
                sys.exit(1)
            _connect_profile(args.serial, profile)

        else:
            p_wifi.print_help()


# ─────────────────────────────────────────────
if __name__ == "__main__":
    require_adb()
    step("Checking dependencies…")
    require_scrcpy()
    signal.signal(signal.SIGINT,
                  lambda *_: (print(c("\n\n  Goodbye!\n", CYAN)), sys.exit(0)))
    cli()
