"""One-shot launcher: install deps, prep .env, start Roaster Bot."""

import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
ENV = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


def run(cmd, **kw):
    print(f"\n>>> {' '.join(str(x) for x in cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def main():
    print("\n🔥 Roaster Bot\n")

    print("[1/2] Installing dependencies...")
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

    print("[2/2] Preparing environment...")
    if not ENV.exists():
        if ENV_EXAMPLE.exists():
            shutil.copyfile(ENV_EXAMPLE, ENV)
        else:
            ENV.write_text("SERPAPI_KEY=\nHOST=127.0.0.1\nPORT=8002\n")
        print(f"Created .env — add your SERPAPI_KEY before running searches")

    # Get URL
    host, port = "127.0.0.1", "8002"
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if line.startswith("HOST="): host = line.split("=",1)[1].strip() or host
            if line.startswith("PORT="): port = line.split("=",1)[1].strip() or port
    url = f"http://{host}:{port}"

    print(f"\n✓ Starting Roaster Bot at {url}")
    print("Ctrl+C to stop.\n")

    def open_browser():
        time.sleep(2.5)
        try: webbrowser.open(url)
        except: pass
    threading.Thread(target=open_browser, daemon=True).start()

    os.chdir(ROOT)
    subprocess.run([sys.executable, str(ROOT / "main.py")])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"Failed: {e}")
