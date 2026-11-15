"""On-demand security sweep ("Jarvis, scan the system"): a quick active check rather than waiting for a monitor."""
import psutil

from .process_watch import antivirus_status, startup_entries
from .signing import signer


def sweep(cfg: dict, security=None) -> str:
    sus_dirs = [d.lower() for d in cfg["security"]["process_watch"]["suspicious_dirs"]]
    bad_ports = set(cfg["security"]["network_watch"]["suspicious_ports"])
    findings = []

    # 1. programs running from temp/download folders without a valid signature
    risky, seen = [], set()
    for p in psutil.process_iter(["name", "exe"]):
        exe = (p.info["exe"] or "")
        low = exe.lower()
        if exe and low not in seen and any(d in low for d in sus_dirs):
            seen.add(low)
            if not signer(exe):
                risky.append(p.info["name"])
    if risky:
        findings.append(f"{len(risky)} unsigned program(s) running from temp or download folders: {', '.join(risky[:4])}")

    # 2. connections to known backdoor ports
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception:
        conns = []
    hits = {c.raddr.port for c in conns if c.raddr and c.raddr.port in bad_ports and c.raddr.ip not in ("127.0.0.1", "::1")}
    if hits:
        findings.append(f"connections to suspicious ports {sorted(hits)}")
    listening = len({c.laddr.port for c in conns if c.status == psutil.CONN_LISTEN})

    # 3. antivirus + startup
    av = antivirus_status()
    if av is not None and not any(av.values()):
        findings.append("no antivirus real-time protection is active")
    av_on = [n for n, on in (av or {}).items() if on]
    startups = len(startup_entries())

    # 4. who is at the keyboard
    who = ""
    if security and security.face:
        v = security.owner_verified()
        who = {True: " Owner identity verified by camera.", False: " Warning: the person at the keyboard is not verified.",
               None: ""}[v]
        if v is False:
            findings.append("an unverified person is at the keyboard")

    base = (f"Sweep complete: {len(psutil.pids())} processes, {listening} listening ports and {startups} startup "
            f"entries checked. Antivirus: {', '.join(av_on) if av_on else 'unknown'}.{who}")
    if findings:
        return base + " Findings: " + "; ".join(findings) + "."
    return base + " No threats detected."
