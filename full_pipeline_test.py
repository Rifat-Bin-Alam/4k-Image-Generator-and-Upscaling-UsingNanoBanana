
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"
OUTPUT = BASE / "output"
JOBS = BASE / "jobs"

UPSCALE_EXE = BASE / "upscaler" / "realesrgan-ncnn-vulkan.exe"
MODEL_NAME = "realesrgan-x4plus"
DEBUG_URL = "http://127.0.0.1:9222"

for folder in (DOWNLOADS, OUTPUT, JOBS):
    folder.mkdir(parents=True, exist_ok=True)


def save_job(job):
    (JOBS / f"{job['id']}.json").write_text(
        json.dumps(job, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_option_sources(page):
    """Return the currently visible generated-option image URLs in DOM order."""
    images = page.locator('img[alt^="Option"]')
    sources = []

    for i in range(images.count()):
        image = images.nth(i)
        if image.is_visible():
            src = image.get_attribute("src")
            if src:
                sources.append(src)

    return sources


def wait_for_new_results(page, old_sources, timeout=300):
    """
    Wait for new image URLs to appear, then wait for the page to settle.
    Returns the last new option detected in DOM order.
    """
    deadline = time.monotonic() + timeout
    last_new_sources = []
    last_change = None
    saw_new = False

    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)

        current_sources = get_option_sources(page)
        new_sources = [src for src in current_sources if src not in old_sources]

        if new_sources:
            if new_sources != last_new_sources:
                last_new_sources = new_sources
                last_change = time.monotonic()
            saw_new = True

        # Require new results and 12 seconds without further option changes.
        if (
            saw_new
            and last_change is not None
            and time.monotonic() - last_change >= 12
        ):
            return last_new_sources[-1]

    raise TimeoutError(
        "No stable new image options were detected within five minutes. "
        "Check Google Flow manually; nothing was downloaded."
    )


def upscale_image(source_path, job_id):
    raw_path = OUTPUT / f"{job_id}_4x.png"
    final_path = OUTPUT / f"{job_id}_4K.png"

    if not UPSCALE_EXE.exists():
        raise FileNotFoundError(f"Real-ESRGAN not found:\n{UPSCALE_EXE}")

    result = subprocess.run(
        [
            str(UPSCALE_EXE),
            "-i", str(source_path),
            "-o", str(raw_path),
            "-n", MODEL_NAME,
            "-s", "4",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0 or not raw_path.exists():
        raise RuntimeError(
            result.stderr or result.stdout or "Upscaling failed."
        )

    # Produce exact UHD dimensions without cropping or stretching.
    with Image.open(raw_path) as source:
        image = ImageOps.contain(
            source.convert("RGB"),
            (3840, 2160),
            method=Image.Resampling.LANCZOS,
        )
        canvas = Image.new("RGB", (3840, 2160), (0, 0, 0))
        canvas.paste(
            image,
            ((3840 - image.width) // 2, (2160 - image.height) // 2),
        )
        canvas.save(final_path, format="PNG")

    return raw_path, final_path


def main():
    prompt = input("Enter your image prompt:\n> ").strip()
    if not prompt:
        print("No prompt entered.")
        return

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    job = {
        "id": job_id,
        "prompt": prompt,
        "created_at": datetime.now().isoformat(),
        "status": "starting",
        "selection": "last_newly_detected_option",
        "original_image": None,
        "upscaled_image": None,
    }
    save_job(job)

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(DEBUG_URL)
            pages = [
                tab for tab in browser.contexts[0].pages
                if "flow.google.com" in tab.url
            ]
            if not pages:
                raise RuntimeError(
                    "Open Google Flow in the automation Chrome window."
                )

            page = pages[0]
            page.bring_to_front()

            # If an image editor is open, return to the prompt editor first.
            if "/edit/" in page.url:
                page.get_by_role(
                    "button",
                    name="Back button to go to previous page",
                ).click()
                page.wait_for_timeout(1500)

            prompt_box = page.get_by_role(
                "textbox",
                name="What do you want to create?",
            )
            prompt_box.wait_for(state="visible", timeout=30000)

            # Capture the baseline only after returning to the prompt screen.
            old_sources = set(get_option_sources(page))

            prompt_box.fill(prompt)
            generate_button = page.get_by_role(
                "button",
                name="Start generation",
            )
            generate_button.wait_for(state="visible", timeout=15000)

            if not generate_button.is_enabled():
                raise RuntimeError("Generate button is disabled.")

            job["status"] = "generating"
            save_job(job)

            generate_button.click()
            print("Generation started. Waiting for new image options...")

            # Wait for genuinely new image URLs, not pre-existing options.
            selected_src = wait_for_new_results(page, old_sources)
            print("New results detected. Selecting the last new option.")

            # Re-find the matching option in case the DOM was refreshed.
            matching_images = page.locator(
                'img[alt^="Option"]'
            ).filter(has=page.locator("body"))  # locator remains live

            images = page.locator('img[alt^="Option"]')
            selected_image = None

            for i in range(images.count()):
                image = images.nth(i)
                if (
                    image.is_visible()
                    and image.get_attribute("src") == selected_src
                ):
                    selected_image = image
                    break

            if selected_image is None:
                raise RuntimeError(
                    "The selected new image disappeared. "
                    "No download was attempted."
                )

            selected_image.locator("xpath=..").click()

            download_button = page.get_by_role(
                "button",
                name="Download media",
            )
            download_button.wait_for(state="visible", timeout=30000)
            download_button.click()

            original_size = page.get_by_text(
                "Original size",
                exact=False,
            ).last
            original_size.wait_for(state="visible", timeout=15000)

            with page.expect_download(timeout=60000) as event:
                original_size.click()

            download = event.value
            filename = Path(
                download.suggested_filename or "generated_image.jpg"
            ).name
            source_path = DOWNLOADS / f"{job_id}_{filename}"
            download.save_as(str(source_path))

            job["original_image"] = str(source_path)
            job["selected_image_url"] = selected_src
            job["status"] = "downloaded"
            save_job(job)
            print("Downloaded:", source_path)

        job["status"] = "upscaling"
        save_job(job)

        raw_path, final_path = upscale_image(source_path, job_id)

        job["raw_upscaled_image"] = str(raw_path)
        job["upscaled_image"] = str(final_path)
        job["status"] = "completed"
        job["completed_at"] = datetime.now().isoformat()
        save_job(job)

        print("\nPIPELINE COMPLETE")
        print("Prompt:", prompt)
        print("Original:", source_path)
        print("4x image:", raw_path)
        print("4K image:", final_path)
        print("Job record:", JOBS / f"{job_id}.json")

    except Exception as error:
        job["status"] = "failed"
        job["error"] = str(error)
        save_job(job)
        print("\nPipeline stopped:", error)
        print("Job record:", JOBS / f"{job_id}.json")


if __name__ == "__main__":
    main()
