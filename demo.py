"""Build a CLI, merge two synthetic audiences and independently reconstruct all registers."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import zlib


ROOT = Path(__file__).resolve().parent


def reference_registers(ids, precision):
    # A bit-string reference intentionally avoids Go's shifts and leading-zero primitives.
    registers = bytearray(1 << precision)
    for visitor in ids:
        digest = hashlib.sha256(visitor.encode()).digest()[:8]
        bits = "".join(f"{b:08b}" for b in digest)
        bucket = int(bits[:precision], 2)
        suffix = bits[precision:]
        rank = len(suffix) - len(suffix.lstrip("0")) + 1
        registers[bucket] = max(registers[bucket], rank)
    return registers


def main():
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        binary = work / ("reach-sketch.exe" if os.name == "nt" else "reach-sketch")
        subprocess.run(["go", "build", "-o", binary, "."], cwd=ROOT, check=True)

        def cli(*args, data=None):
            return subprocess.check_output([binary, *args], cwd=work, input=data)

        web = [f"visitor-{i}" for i in range(20000)]
        app = [f"visitor-{i}" for i in range(12000, 30000)]
        exact = set(web) | set(app)
        for name, precision, audience in [("web", 14, web), ("app", 12, app)]:
            # Append repeat traffic, use an actual text file and exercise CRLF parsing.
            path = work / f"{name}.txt"
            path.write_bytes(("\r\n".join(audience + audience[:500]) + "\r\n").encode())
            cli("build", str(precision), path, name + ".rch")
        cli("merge", "union.rch", "web.rch", "app.rch")
        cli("merge", "replay.rch", "app.rch", "web.rch", "app.rch")
        cli("build", "12", "-", "direct.rch", data="\n".join(sorted(exact)).encode())
        bundle = (work / "union.rch").read_bytes()
        if any(bundle != (work / name).read_bytes() for name in ["replay.rch", "direct.rch"]):
            raise ValueError("merge, replay and direct build disagree")
        registers = reference_registers(exact, 12)
        payload = b"RCH1" + bytes([12]) + registers
        if bundle != payload + zlib.crc32(payload).to_bytes(4, "big"):
            raise ValueError("binary output differs from independent Python reconstruction")
        report = json.loads(cli("report", "union.rch"))
        estimate = report["estimated_distinct"]
        # The demo's population is in the harmonic-mean estimator's range.
        m = len(registers)
        raw = 0.7213 / (1 + 1.079/m) * m*m / math.fsum(2.0**-r for r in registers)
        if raw <= 2.5*m or estimate != math.floor(raw + 0.5):
            raise ValueError("reported estimate differs from the reference calculation")
        print(json.dumps({"web_distinct": len(set(web)), "app_distinct": len(set(app)),
                          "shared": len(set(web) & set(app)), "union_exact": len(exact),
                          "union_estimate": estimate, "relative_error_pct": round(100*(estimate/len(exact)-1), 3),
                          "file_bytes": len(bundle), "registers_checked": len(registers)}, indent=2))


if __name__ == "__main__":
    main()
