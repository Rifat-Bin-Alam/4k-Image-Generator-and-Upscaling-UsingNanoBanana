
from pathlib import Path
import subprocess
import time

BASE = Path(__file__).resolve().parent
DOWNLOADS = Path(r"F:\Downloads")
UPSCALE_EXE = BASE / "upscaler" / "realesrgan-ncnn-vulkan.exe"
OUTPUT = BASE / "output"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
OUTPUT.mkdir(exist_ok=True)

# Ignore files already present when the script starts.
seen = {
    p.name.lower()
    for p in DOWNLOADS.iterdir()
    if p.is_file()
}

print(f"Watching for NEW images in: {DOWNLOADS}")
print("Press Ctrl+C to stop.")

try:
    while True:
        for image in DOWNLOADS.iterdir():
            if not image.is_file():
                continue

            if image.suffix.lower() not in IMAGE_EXTENSIONS:
                continue

            if image.name.lower() in seen:
                continue

            if image.name.lower().endswith(".png"):
                pass

            # Wait until the file finishes downloading.
            try:
                size1 = image.stat().st_size
                time.sleep(1)
                size2 = image.stat().st_size
            except (FileNotFoundError, PermissionError):
                continue

            if size1 == 0 or size1 != size2:
                continue

            seen.add(image.name.lower())

            destination = OUTPUT / f"{image.stem}_4x.png"
            print(f"\nNew image: {image.name}")

            try:
                subprocess.run(
                    [
                        str(UPSCALE_EXE),
                        "-i", str(image),
                        "-o", str(destination),
                        "-n", "realesrgan-x4plus",
                        "-s", "4"
                    ],
                    check=True
                )
                print(f"Saved: {destination}")
            except (subprocess.CalledProcessError, OSError) as error:
                print(f"Upscaling failed: {error}")

        time.sleep(2)

except KeyboardInterrupt:
    print("\nStopped watching downloads.")
