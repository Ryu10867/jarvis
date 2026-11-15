"""Authenticode signature lookup (cached) so alerts can tell 'signed by Razer' from 'unsigned unknown binary'."""
import functools
import json
import subprocess


@functools.lru_cache(maxsize=512)
def signer(exe: str) -> str | None:
    """Return the signer's organisation if the file has a VALID signature, else None."""
    if not exe:
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$s = Get-AuthenticodeSignature -LiteralPath '{exe.replace(chr(39), chr(39) * 2)}';"
             "@{status=[string]$s.Status; subject=[string]$s.SignerCertificate.Subject} | ConvertTo-Json"],
            capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        data = json.loads(out)
    except Exception:
        return None
    if data.get("status") != "Valid":
        return None
    subject = data.get("subject") or ""
    for part in subject.split(","):
        if part.strip().startswith("O="):
            return part.strip()[2:].strip('"')
    return subject or "valid signature"
