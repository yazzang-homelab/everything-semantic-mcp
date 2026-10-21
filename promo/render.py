"""Render the promo PNGs with headless Chrome (Windows).  python promo/render.py"""

import subprocess
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HERE = Path(__file__).resolve().parent

JOBS = [
    ("threads-1-hook.png", "cards.html?c=c1", (1080, 1350)),
    ("threads-2-features.png", "cards.html?c=c2", (1080, 1350)),
    ("threads-3-numbers.png", "cards.html?c=c3", (1080, 1350)),
    ("landing.png", "index.html?still=1", (1280, 2600)),
]

for out, page, (w, h) in JOBS:
    url = (HERE / page.split("?")[0]).as_uri() + "?" + page.split("?", 1)[1]
    subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size={w},{h}",
            f"--screenshot={HERE / out}",
            "--virtual-time-budget=3000",
            url,
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    print(out, (HERE / out).stat().st_size, "bytes")
