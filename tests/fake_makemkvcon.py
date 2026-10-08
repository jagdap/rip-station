"""Stand-in for makemkvcon: replays a fixture for `info`, fakes progress + a file for `mkv`.

FAKE_SCENARIO simulates damaged discs/drives, reacting to per-run settings the way
MakeMKV would (read from $HOME/.MakeMKV or $HOME/Library/MakeMKV settings.conf):
  scratched  read errors; succeeds once io_ErrorRetryCount is raised
  damaged    read errors; succeeds only with io_IgnoreReadErrors
  dead       read errors on every attempt
  drive      hardware/connection error
"""
import os
import sys
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("-")]
fixture = Path(os.environ["FAKE_FIXTURE"]).read_text()
scenario = os.environ.get("FAKE_SCENARIO", "")
settings = ""
for d in (".MakeMKV", "Library/MakeMKV"):
    f = Path(os.environ.get("HOME", "/nonexistent"), d, "settings.conf")
    if f.exists():
        settings += f.read_text()
retries = "io_ErrorRetryCount" in settings
salvage = 'io_IgnoreReadErrors = "true"' in settings

READ_ERR = (
    "MSG:2003,0,3,\"Error 'Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR' occurred while reading "
    "'/VIDEO_TS/VTS_01_3.VOB' at offset '1048576'\",\"Error '%1' occurred while reading '%2' at offset '%3'\","
    "\"Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR\",\"/VIDEO_TS/VTS_01_3.VOB\",\"1048576\""
)
DRIVE_ERR = "MSG:2003,0,1,\"Error 'Scsi error - HARDWARE ERROR:TIMEOUT ON LOGICAL UNIT' occurred\",\"%1\",\"x\""

if args[0] == "info":
    sys.stdout.write(fixture)
elif args[0] == "mkv":
    if log := os.environ.get("FAKE_LOG"):
        with open(log, "a") as fh:
            fh.write(("retries " if retries else "") + ("salvage" if salvage else "") + "|\n")
    _, source, title, out = args
    label = next(l for l in fixture.splitlines() if l.startswith("CINFO:32")).split('"')[1]
    print('PRGT:3100,0,"Opening DVD disc"')
    print('PRGC:3104,0,"Decrypting data"')
    print("PRGV:58982,58982,65536", flush=True)  # ~90% of the *opening* bar
    print('PRGT:5024,0,"Saving all titles to MKV files"')
    ok = not scenario or (scenario == "scratched" and retries) or (scenario == "damaged" and salvage)
    for i in range(0, 65537, 16384):
        print(f"PRGV:{i},{i},65536", flush=True)
        if scenario in ("scratched", "damaged", "dead") and i == 32768:
            print(READ_ERR)
            print(READ_ERR)
        if scenario == "drive" and i == 16384:
            print(DRIVE_ERR, flush=True)
            sys.exit(1)
    target = Path(out, f"{label}_t{int(title):02d}.mkv")
    if ok:
        target.write_bytes(b"\x1aE\xdf\xa3" + bytes(1000 + int(title)))
        print('MSG:5036,0,1,"Copy complete. 1 titles saved.","%1","1"')
    else:
        target.write_bytes(b"partial")
        print('MSG:5003,0,2,"Failed to save title 0 to file","Failed to save title %1 to file %2","0","x"')
        print('MSG:5037,0,2,"Copy complete. 0 titles saved, 1 failed.","x","0","1"')
