"""Security-lab helpers for Jarvis's cybersecurity project. Everything here is defensive, CTF, or scans a target
the user explicitly owns / controls:

  - net_scan       : discover live hosts and common open ports on YOUR OWN local network
  - local_audit    : audit THIS laptop (firewall, listening ports, Defender, updates)
  - ctf            : offline CTF helpers (hash, identify, encode/decode, dictionary hash-crack)
  - scan_target    : a port + banner scan of a host the user names and confirms is theirs (lab VM)

No external tools and no network downloads are needed; everything uses the Python standard library.

Deliberately NOT here, and refused upstream: attacking, cracking or altering systems the user doesn't own,
and anything meant to hide activity from a system's rightful owner."""
import base64
import codecs
import concurrent.futures as cf
import hashlib
import ipaddress
import re
import socket
import subprocess
import urllib.parse

# Ports worth reporting, with the service name people recognise.
COMMON_PORTS = {
    21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 80: "HTTP", 110: "POP3", 135: "RPC",
    139: "NetBIOS", 143: "IMAP", 443: "HTTPS", 445: "SMB", 587: "SMTP", 993: "IMAPS", 995: "POP3S",
    1433: "MSSQL", 1521: "Oracle", 3306: "MySQL", 3389: "RDP", 5432: "PostgreSQL", 5900: "VNC",
    6379: "Redis", 8080: "HTTP-alt", 8443: "HTTPS-alt", 27017: "MongoDB",
}
# Services that shouldn't normally be open to a whole network — worth flagging in a scan.
RISKY = {23: "Telnet sends everything in clear text", 21: "FTP is usually unencrypted",
         3389: "Remote Desktop exposed", 445: "SMB file sharing exposed", 5900: "VNC exposed",
         3306: "MySQL reachable over the network", 5432: "PostgreSQL reachable over the network",
         6379: "Redis often has no password", 27017: "MongoDB often has no password",
         1433: "MSSQL reachable over the network"}


def _check_port(host: str, port: int, timeout: float) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _local_subnets() -> list[ipaddress.IPv4Network]:
    nets = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith(("127.", "169.254.")):
                continue
            nets.append(ipaddress.ip_network(ip + "/24", strict=False))
    except socket.gaierror:
        pass
    # de-duplicate, keep order
    seen, out = set(), []
    for n in nets:
        if str(n) not in seen:
            seen.add(str(n))
            out.append(n)
    return out


def net_scan(ports: list[int] | None = None, timeout: float = 0.3) -> str:
    """Discover live hosts and open common ports on the local network(s) this machine is on."""
    nets = _local_subnets()
    if not nets:
        return "I couldn't work out this machine's local network."
    ports = ports or list(COMMON_PORTS)
    net = nets[0]
    hosts = [str(h) for h in net.hosts()][:254]
    found: dict[str, list[int]] = {}
    # A host is "up" if any probed port answers; scan host x port in parallel.
    with cf.ThreadPoolExecutor(max_workers=200) as ex:
        jobs = {ex.submit(_check_port, h, p, timeout): (h, p) for h in hosts for p in ports}
        for fut in cf.as_completed(jobs):
            h, p = jobs[fut]
            try:
                if fut.result():
                    found.setdefault(h, []).append(p)
            except OSError:
                pass
    if not found:
        return (f"Scanned {len(hosts)} addresses on {net} and found no open common ports. "
                "Hosts may be up but firewalled.")
    lines = [f"Network {net}: {len(found)} host(s) with open ports."]
    flags = []
    for h in sorted(found, key=lambda x: ipaddress.ip_address(x)):
        ps = sorted(found[h])
        named = ", ".join(f"{p} ({COMMON_PORTS.get(p, '?')})" for p in ps)
        lines.append(f"  {h}: {named}")
        for p in ps:
            if p in RISKY:
                flags.append(f"{h}:{p} - {RISKY[p]}")
    if flags:
        lines.append("Worth a look: " + "; ".join(flags) + ".")
    return "\n".join(lines)


def scan_target(target: str, timeout: float = 0.6) -> str:
    """Port + banner scan of a single host the user named (and confirmed is theirs). Lab / authorised use."""
    target = target.strip()
    # accept a URL, host:port, or bare host
    if "://" in target:
        target = urllib.parse.urlparse(target).netloc
    target = target.split("/")[0].split(":")[0].strip()
    try:
        ip = socket.gethostbyname(target)
    except socket.gaierror:
        return f"I couldn't resolve '{target}'."
    open_ports = {}
    with cf.ThreadPoolExecutor(max_workers=100) as ex:
        jobs = {ex.submit(_check_port, ip, p, timeout): p for p in COMMON_PORTS}
        for fut in cf.as_completed(jobs):
            p = jobs[fut]
            try:
                if fut.result():
                    open_ports[p] = _banner(ip, p, timeout)
            except OSError:
                pass
    if not open_ports:
        return f"{target} ({ip}): no common ports open, or they're firewalled."
    lines = [f"{target} ({ip}): {len(open_ports)} open port(s)."]
    for p in sorted(open_ports):
        b = open_ports[p]
        lines.append(f"  {p} ({COMMON_PORTS.get(p, '?')})" + (f" - {b}" if b else ""))
    return "\n".join(lines)


def _banner(host: str, port: int, timeout: float) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            if port in (80, 8080, 443, 8443):
                s.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
            data = s.recv(200)
        text = data.decode("latin-1", "replace").strip()
        m = re.search(r"Server:\s*(.+)", text, re.I)
        if m:
            return m.group(1).strip()[:80]
        first = text.splitlines()[0] if text else ""
        return first[:80]
    except OSError:
        return ""


# ---------------------------------------------------------------- local audit
def _ps(cmd: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                           timeout=timeout, creationflags=0x08000000)
        return (r.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def local_audit() -> str:
    findings, good = [], []
    fw = _ps("(Get-NetFirewallProfile | Where-Object Enabled -eq 'False').Name -join ','")
    if fw:
        findings.append(f"firewall is OFF for the {fw} profile(s)")
    else:
        good.append("firewall is on for all profiles")

    av = _ps("try { (Get-MpComputerStatus).RealTimeProtectionEnabled } catch { 'unknown' }")
    if av == "True":
        good.append("Defender real-time protection is on")
    elif av == "False":
        findings.append("Defender real-time protection is OFF")

    listening = _ps(
        "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "Where-Object { $_.LocalAddress -in '0.0.0.0','::' } | "
        "Select-Object -Expand LocalPort -Unique")
    exposed = sorted({int(x) for x in listening.split() if x.strip().isdigit()} & set(RISKY))
    if exposed:
        findings.append("services listening on all interfaces: "
                        + ", ".join(f"{p} ({COMMON_PORTS.get(p, '?')})" for p in exposed))

    pend = _ps("$d=(Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 1).InstalledOn; "
               "if ($d) { $d.ToString('yyyy-MM-dd') }")
    if pend:
        good.append(f"last Windows update installed {pend}")

    rdp = _ps("(Get-ItemProperty 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server' "
              "-Name fDenyTSConnections -ErrorAction SilentlyContinue).fDenyTSConnections")
    if rdp == "0":
        findings.append("Remote Desktop is enabled on this machine")

    out = ["Local security audit of this laptop:"]
    out.append("  Issues: " + ("; ".join(findings) if findings else "none found."))
    if good:
        out.append("  OK: " + "; ".join(good) + ".")
    return "\n".join(out)


# ---------------------------------------------------------------- CTF helpers
_HASH_FUNCS = {"md5": hashlib.md5, "sha1": hashlib.sha1, "sha224": hashlib.sha224, "sha256": hashlib.sha256,
               "sha384": hashlib.sha384, "sha512": hashlib.sha512}
_HASH_LENGTHS = {32: ["md5"], 40: ["sha1"], 56: ["sha224"], 64: ["sha256"], 96: ["sha384"], 128: ["sha512"]}


def ctf_hash(text: str, algo: str = "sha256") -> str:
    algo = algo.lower().replace("-", "")
    fn = _HASH_FUNCS.get(algo)
    if not fn:
        return f"I can do {', '.join(_HASH_FUNCS)}, not {algo}."
    return f"{algo}: {fn(text.encode()).hexdigest()}"


def ctf_identify(value: str) -> str:
    v = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]+", v) and len(v) in _HASH_LENGTHS:
        return f"{len(v)} hex chars - looks like {', '.join(_HASH_LENGTHS[len(v)])}."
    guesses = []
    if re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", v) and len(v) % 4 == 0:
        guesses.append("base64")
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", v):
        guesses.append("hex")
    if v.startswith("$2") and len(v) == 60:
        guesses.append("bcrypt")
    if re.fullmatch(r"[A-Z2-7]+=*", v) and len(v) % 8 == 0:
        guesses.append("base32")
    return ("Could be: " + ", ".join(guesses) + "." if guesses
            else "I couldn't recognise that format.")


def ctf_encode(text: str, scheme: str) -> str:
    s = scheme.lower().replace(" ", "").replace("-", "")
    try:
        if s in ("base64", "b64"):
            return base64.b64encode(text.encode()).decode()
        if s == "base32":
            return base64.b32encode(text.encode()).decode()
        if s == "hex":
            return text.encode().hex()
        if s == "url":
            return urllib.parse.quote(text)
        if s in ("rot13", "rot"):
            return codecs.encode(text, "rot_13")
        if s in ("binary", "bin"):
            return " ".join(format(b, "08b") for b in text.encode())
    except (ValueError, UnicodeError) as e:
        return f"Couldn't encode: {e}"
    return f"I can encode base64, base32, hex, url, rot13 or binary, not {scheme}."


def ctf_decode(text: str, scheme: str) -> str:
    s = scheme.lower().replace(" ", "").replace("-", "")
    t = text.strip()
    try:
        if s in ("base64", "b64"):
            return base64.b64decode(t + "=" * (-len(t) % 4)).decode("utf-8", "replace")
        if s == "base32":
            return base64.b32decode(t + "=" * (-len(t) % 8)).decode("utf-8", "replace")
        if s == "hex":
            return bytes.fromhex(re.sub(r"\s+", "", t)).decode("utf-8", "replace")
        if s == "url":
            return urllib.parse.unquote(t)
        if s in ("rot13", "rot"):
            return codecs.decode(t, "rot_13")
        if s in ("binary", "bin"):
            return "".join(chr(int(b, 2)) for b in t.split())
    except (ValueError, UnicodeError) as e:
        return f"Couldn't decode as {scheme}: {e}"
    return f"I can decode base64, base32, hex, url, rot13 or binary, not {scheme}."


def ctf_crack(digest: str, wordlist: list[str], algo: str = "") -> str:
    """Dictionary attack: hash each candidate word and compare. For CTF challenges and auditing your own hashes."""
    digest = digest.strip().lower()
    algos = [algo.lower()] if algo else _HASH_LENGTHS.get(len(digest), list(_HASH_FUNCS))
    for word in wordlist:
        for a in algos:
            fn = _HASH_FUNCS.get(a)
            if fn and fn(word.encode()).hexdigest() == digest:
                return f"Cracked: '{word}' ({a})."
    return f"No match in {len(wordlist)} words."
