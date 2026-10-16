import json
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from playwright.sync_api import sync_playwright
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QPushButton,
    QMessageBox,
)


# ============================================================
# CONFIGURATION
# ============================================================

BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"
OUTPUT = BASE / "output"
JOBS = BASE / "jobs"

UPSCALE_EXE = BASE / "upscaler" / "realesrgan-ncnn-vulkan.exe"
MODEL_NAME = "realesrgan-x4plus"
DEBUG_URL = "http://127.0.0.1:9222"
BAT_FILE = BASE / "start_flow_debug.bat"

# The app will open this exact Flow collection.
FLOW_URL = (
    "https://flow.google.com/project/"
    "98d6d8c6-c6af-448b-9314-458d3d92efb6/"
    "collection/"
    "25d8a7d7-b5b2-4446-bb6c-c244dde54a93"
)

# Flow UI selectors confirmed in the current setup.
PROMPT_BOX_NAME = "What do you want to create?"
GENERATE_BUTTON_NAME = "Start generation"
DOWNLOAD_BUTTON_NAME = "Download media"
BACK_BUTTON_NAME = "Back button to go to previous page"

RESULT_WAIT_SECONDS = 300
RESULT_QUIET_SECONDS = 12

for folder in (DOWNLOADS, OUTPUT, JOBS):
    folder.mkdir(parents=True, exist_ok=True)


# ============================================================
# JOB RECORDS
# ============================================================

def save_job(job):
    """Write the current job state to jobs/<job-id>.json."""
    path = JOBS / f"{job['id']}.json"
    path.write_text(
        json.dumps(job, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ============================================================
# GOOGLE CHROME / FLOW AUTOMATION
# ============================================================

class PipelineWorker(QThread):
    status_changed = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, prompt="", job_id="", reconnect_only=False):
        super().__init__()
        self.prompt = prompt
        self.job_id = job_id
        self.reconnect_only = reconnect_only

    def status(self, message):
        self.status_changed.emit(message)

    def check_cancelled(self):
        if self.isInterruptionRequested():
            raise RuntimeError("Operation cancelled.")

    def connect_browser(self, playwright):
        """Connect to the debug Chrome; launch it with the BAT if necessary."""
        self.status("Checking automation Chrome...")

        try:
            return playwright.chromium.connect_over_cdp(
                DEBUG_URL,
                timeout=2500,
            )
        except Exception:
            pass

        if not BAT_FILE.exists():
            raise FileNotFoundError(
                "Chrome launcher was not found:\n"
                f"{BAT_FILE}\n\n"
                "Make sure start_flow_debug.bat is in the same folder as main.py."
            )

        self.status("Starting automation Chrome...")
        subprocess.Popen(
            ["cmd.exe", "/c", str(BAT_FILE)],
            cwd=str(BASE),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )

        deadline = time.monotonic() + 90
        last_error = None

        while time.monotonic() < deadline:
            self.check_cancelled()
            try:
                browser = playwright.chromium.connect_over_cdp(
                    DEBUG_URL,
                    timeout=2500,
                )
                self.status("Connected to automation Chrome.")
                return browser
            except Exception as error:
                last_error = error
                time.sleep(2)

        raise RuntimeError(
            "Could not connect to Chrome within 90 seconds.\n"
            "Check that Chrome opened and that start_flow_debug.bat "
            "uses the correct Chrome installation.\n\n"
            f"Last connection error: {last_error}"
        )

    def get_flow_page(self, browser, navigate_to_collection=True):
        """
        Return the exact Flow tab used by this job.
        Prefer an existing Flow tab; create one if none exists.
        Navigate that tab to the configured collection when requested.
        """
        context = browser.contexts[0]
        pages = list(context.pages)

        flow_pages = [
            page for page in pages
            if "flow.google.com" in (page.url or "")
        ]

        # Prefer a tab already at the selected project/collection.
        page = next(
            (
                tab for tab in flow_pages
                if "/project/98d6d8c6-c6af-448b-9314-458d3d92efb6/"
                   "collection/25d8a7d7-b5b2-4446-bb6c-c244dde54a93"
                   in tab.url
            ),
            None,
        )

        # Otherwise use the first Flow tab, or create a new one.
        if page is None and flow_pages:
            page = flow_pages[0]
        if page is None:
            page = context.new_page()

        if navigate_to_collection:
            current = page.url or ""
            if (
                "flow.google.com" not in current
                or "/project/98d6c8d6-" in current
                or "collection/25d8a7d7-" not in current
            ):
                self.status("Opening your selected Flow collection...")
                page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=60000)
            elif "collection/25d8a7d7-b5b2-4446-bb6c-c244dde54a93" not in current:
                self.status("Opening your selected Flow collection...")
                page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=60000)

        return page

    @staticmethod
    def get_option_sources(page):
        """Collect visible source URLs from Flow's generated image options."""
        images = page.locator('img[alt^="Option"]')
        sources = []

        try:
            count = images.count()
        except Exception:
            return sources

        for index in range(count):
            image = images.nth(index)
            try:
                if not image.is_visible():
                    continue
                src = image.get_attribute("src")
                if src and src not in sources:
                    sources.append(src)
            except Exception:
                continue

        return sources

    def wait_for_new_result(self, page, previous_sources):
        """Wait for a new image URL to appear and remain stable."""
        deadline = time.monotonic() + RESULT_WAIT_SECONDS
        last_new_sources = []
        last_change = None

        while time.monotonic() < deadline:
            self.check_cancelled()
            page.wait_for_timeout(1500)

            current_sources = self.get_option_sources(page)
            new_sources = [
                src for src in current_sources
                if src not in previous_sources
            ]

            if new_sources and new_sources != last_new_sources:
                last_new_sources = new_sources
                last_change = time.monotonic()
                self.status(
                    f"New image options detected: {len(new_sources)}. "
                    "Waiting for results to settle..."
                )

            if (
                last_new_sources
                and last_change is not None
                and time.monotonic() - last_change >= RESULT_QUIET_SECONDS
            ):
                # Pick the last new option in the order displayed by Flow.
                return last_new_sources[-1]

        raise TimeoutError(
            "No stable new image option was detected within five minutes.\n"
            "Check Google Flow manually. The app did not download an image."
        )

    def download_new_result(self, page, previous_sources):
        selected_src = self.wait_for_new_result(page, previous_sources)

        self.status("Opening the newest generated image...")
        images = page.locator('img[alt^="Option"]')
        selected_image = None

        for index in range(images.count()):
            image = images.nth(index)
            try:
                if (
                    image.is_visible()
                    and image.get_attribute("src") == selected_src
                ):
                    selected_image = image
                    break
            except Exception:
                continue

        if selected_image is None:
            raise RuntimeError(
                "The selected new image disappeared before it could be opened."
            )

        # Flow's option image is inside a clickable card.
        selected_image.locator("xpath=..").click()
        page.wait_for_timeout(800)

        download_button = page.get_by_role(
            "button",
            name=DOWNLOAD_BUTTON_NAME,
        )
        download_button.wait_for(state="visible", timeout=30000)
        download_button.click()

        original_size = page.get_by_text(
            "Original size",
            exact=False,
        ).last
        original_size.wait_for(state="visible", timeout=15000)

        self.status("Downloading the original-size image...")
        with page.expect_download(timeout=60000) as download_event:
            original_size.click()

        download = download_event.value
        suggested_name = Path(
            download.suggested_filename or "generated_image.jpg"
        ).name
        destination = DOWNLOADS / f"{self.job_id}_{suggested_name}"
        download.save_as(str(destination))

        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError("The downloaded image file is missing or empty.")

        return destination, selected_src

    # ========================================================
    # IMAGE UPSCALING / 4K EXPORT
    # ========================================================

    def upscale(self, source_path):
        if not UPSCALE_EXE.exists():
            raise FileNotFoundError(
                "Real-ESRGAN executable was not found:\n"
                f"{UPSCALE_EXE}"
            )

        raw_path = OUTPUT / f"{self.job_id}_4x.png"
        final_path = OUTPUT / f"{self.job_id}_4K.png"

        self.status("Upscaling image with Real-ESRGAN...")
        command = [
            str(UPSCALE_EXE),
            "-i", str(source_path),
            "-o", str(raw_path),
            "-n", MODEL_NAME,
            "-s", "4",
        ]

        result = subprocess.run(
            command,
            cwd=str(UPSCALE_EXE.parent),
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0 or not raw_path.exists():
            details = "\n".join(
                part.strip() for part in (result.stderr, result.stdout)
                if part and part.strip()
            )
            raise RuntimeError(
                "Real-ESRGAN failed."
                + (f"\n\n{details}" if details else "")
            )

        self.status("Preparing the 3840 × 2160 4K canvas...")
        # Keep the entire image visible: preserve its aspect ratio, do not crop
        # or stretch. If needed, black padding fills the rest of the 16:9 canvas.
        with Image.open(raw_path) as source:
            image = ImageOps.contain(
                source.convert("RGB"),
                (3840, 2160),
                method=Image.Resampling.LANCZOS,
            )
            canvas = Image.new("RGB", (3840, 2160), (0, 0, 0))
            canvas.paste(
                image,
                (
                    (3840 - image.width) // 2,
                    (2160 - image.height) // 2,
                ),
            )
            canvas.save(final_path, format="PNG")

        with Image.open(final_path) as check:
            if check.size != (3840, 2160):
                raise RuntimeError(
                    f"Unexpected output dimensions: {check.size}"
                )

        return raw_path, final_path

    # ========================================================
    # WORKER ENTRY POINT
    # ========================================================

    def run(self):
        job = {
            "id": self.job_id,
            "prompt": self.prompt,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "connecting",
            "flow_url": FLOW_URL,
            "selection": "last_newly_detected_option",
            "original_image": None,
            "raw_upscaled_image": None,
            "upscaled_image": None,
        }
        save_job(job)

        try:
            with sync_playwright() as playwright:
                browser = self.connect_browser(playwright)

                # Important: keep and use this exact page. Do not search again
                # later and accidentally select a different Flow tab.
                page = self.get_flow_page(
                    browser,
                    navigate_to_collection=True,
                )
                page.bring_to_front()

                if self.reconnect_only:
                    self.status(
                        "Chrome is connected. Check Flow and sign in manually "
                        "if Google requests it."
                    )
                    self.succeeded.emit({
                        "reconnect": True,
                        "url": page.url,
                    })
                    return

                # If an image editor is open, return to the prompt screen.
                if "/edit/" in (page.url or ""):
                    self.status("Returning to the Flow prompt screen...")
                    page.get_by_role(
                        "button",
                        name=BACK_BUTTON_NAME,
                    ).click(timeout=15000)
                    page.wait_for_timeout(1500)

                prompt_box = page.get_by_role(
                    "textbox",
                    name=PROMPT_BOX_NAME,
                )
                prompt_box.wait_for(state="visible", timeout=60000)

                # Snapshot existing options before starting this generation.
                previous_sources = set(self.get_option_sources(page))

                self.status("Entering your prompt...")
                prompt_box.fill(self.prompt)

                generate_button = page.get_by_role(
                    "button",
                    name=GENERATE_BUTTON_NAME,
                )
                generate_button.wait_for(state="visible", timeout=20000)

                if not generate_button.is_enabled():
                    raise RuntimeError(
                        "Flow's Generate button is disabled. "
                        "Check sign-in, quota, selected model, and page state."
                    )

                job["status"] = "generating"
                save_job(job)

                self.status("Starting Google Flow generation...")
                generate_button.click()

                self.status("Waiting for the new generated image...")
                source_path, selected_src = self.download_new_result(
                    page,
                    previous_sources,
                )

                job["original_image"] = str(source_path)
                job["selected_image_url"] = selected_src
                job["status"] = "downloaded"
                save_job(job)

            # The browser connection is no longer needed while upscaling.
            job["status"] = "upscaling"
            save_job(job)

            raw_path, final_path = self.upscale(source_path)

            job["raw_upscaled_image"] = str(raw_path)
            job["upscaled_image"] = str(final_path)
            job["status"] = "completed"
            job["completed_at"] = datetime.now().isoformat(timespec="seconds")
            save_job(job)

            self.succeeded.emit({
                "reconnect": False,
                "prompt": self.prompt,
                "original": str(source_path),
                "upscaled": str(final_path),
                "job_file": str(JOBS / f"{self.job_id}.json"),
            })

        except Exception as error:
            job["status"] = "failed"
            job["error"] = str(error)
            job["failed_at"] = datetime.now().isoformat(timespec="seconds")
            save_job(job)
            self.failed.emit(str(error))


# ============================================================
# DESKTOP APPLICATION
# ============================================================

class Flow4KApp(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None

        self.setWindowTitle("Flow4K — AI Image Studio")
        self.resize(780, 650)
        self.setMinimumSize(600, 520)

        self.build_ui()
        self.apply_styles()

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("Flow4K")
        title.setObjectName("title")
        layout.addWidget(title)

        subtitle = QLabel(
            "Google Flow generation  •  automatic download  •  4K upscaling"
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layout.addWidget(QLabel("Image prompt"))

        self.prompt_input = QTextEdit()
        self.prompt_input.setPlaceholderText(
            "Describe the image you want to create...\n\n"
            "Example: A cinematic fantasy landscape at sunrise, "
            "detailed environment, soft volumetric light."
        )
        self.prompt_input.setMinimumHeight(170)
        layout.addWidget(self.prompt_input)

        self.status_label = QLabel("Ready. Enter a prompt to begin.")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        top_buttons = QHBoxLayout()

        self.reconnect_button = QPushButton("Start / Reconnect Chrome")
        self.reconnect_button.clicked.connect(self.reconnect_chrome)
        top_buttons.addWidget(self.reconnect_button)

        self.open_flow_button = QPushButton("Open Google Flow")
        self.open_flow_button.clicked.connect(self.open_flow)
        top_buttons.addWidget(self.open_flow_button)

        layout.addLayout(top_buttons)

        self.generate_button = QPushButton("Generate & Upscale")
        self.generate_button.setObjectName("primaryButton")
        self.generate_button.clicked.connect(self.generate_and_upscale)
        layout.addWidget(self.generate_button)

        self.open_output_button = QPushButton("Open Output Folder")
        self.open_output_button.clicked.connect(self.open_output_folder)
        layout.addWidget(self.open_output_button)

        note = QLabel(
            "Uses the dedicated Chrome profile created by "
            "start_flow_debug.bat. Sign in to Google Flow manually if asked. "
            "Final images are saved in the output folder."
        )
        note.setObjectName("note")
        note.setWordWrap(True)
        layout.addWidget(note)

    def apply_styles(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #151821;
                color: #E8ECF4;
                font-size: 14px;
            }
            QLabel#title {
                font-size: 30px;
                font-weight: 700;
                color: #70A5FF;
            }
            QLabel#subtitle {
                color: #B7C2D8;
                padding-bottom: 4px;
            }
            QLabel#status {
                background-color: #202635;
                border: 1px solid #30394D;
                border-radius: 8px;
                padding: 12px;
            }
            QLabel#note {
                color: #A5AEC0;
                font-size: 12px;
            }
            QTextEdit {
                background-color: #202635;
                border: 1px solid #39445B;
                border-radius: 8px;
                padding: 10px;
                selection-background-color: #3978D5;
            }
            QPushButton {
                background-color: #2C5FAD;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #3978D5;
            }
            QPushButton#primaryButton {
                background-color: #3677D5;
                padding: 15px;
                font-size: 15px;
            }
            QPushButton#primaryButton:hover {
                background-color: #4A8BE8;
            }
            QPushButton:disabled {
                background-color: #454B59;
                color: #A5AEC0;
            }
        """)

    def set_busy(self, busy):
        self.generate_button.setDisabled(busy)
        self.reconnect_button.setDisabled(busy)
        self.open_flow_button.setDisabled(busy)
        self.open_output_button.setDisabled(busy)

    def start_worker(self, prompt="", reconnect_only=False):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(
                self,
                "Please wait",
                "An operation is already running.",
            )
            return

        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.worker = PipelineWorker(
            prompt=prompt,
            job_id=job_id,
            reconnect_only=reconnect_only,
        )
        self.worker.status_changed.connect(self.status_label.setText)
        self.worker.succeeded.connect(self.operation_succeeded)
        self.worker.failed.connect(self.operation_failed)

        self.set_busy(True)
        self.worker.start()

    def reconnect_chrome(self):
        self.start_worker(reconnect_only=True)

    def open_flow(self):
        # Opens the selected collection in the user's default browser.
        # Generation automation itself uses the dedicated debug Chrome.
        webbrowser.open_new_tab(FLOW_URL)

    def open_output_folder(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(OUTPUT)])
            else:
                webbrowser.open(OUTPUT.as_uri())
        except Exception as error:
            QMessageBox.warning(
                self,
                "Could not open folder",
                f"Output folder:\n{OUTPUT}\n\n{error}",
            )

    def generate_and_upscale(self):
        prompt = self.prompt_input.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(
                self,
                "Missing prompt",
                "Enter an image prompt first.",
            )
            return

        self.start_worker(prompt=prompt)

    def operation_succeeded(self, result):
        self.set_busy(False)

        if result.get("reconnect"):
            message = (
                "Chrome is connected.\n\n"
                f"Current Flow page:\n{result.get('url', '')}\n\n"
                "If Google asks you to sign in, complete that step manually."
            )
            self.status_label.setText("Chrome connected. Ready to use Flow.")
            QMessageBox.information(self, "Chrome connected", message)
            return

        self.status_label.setText(
            "Pipeline complete!\n"
            f"4K output: {result['upscaled']}"
        )
        QMessageBox.information(
            self,
            "Generation complete",
            f"Original image:\n{result['original']}\n\n"
            f"4K image:\n{result['upscaled']}\n\n"
            f"Job record:\n{result['job_file']}",
        )

    def operation_failed(self, error):
        self.set_busy(False)
        self.status_label.setText("Operation failed. See error details.")
        QMessageBox.critical(self, "Flow4K error", error)

    def closeEvent(self, event):
        # Do not forcibly kill Chrome: it owns the user's signed-in session.
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self,
                "Operation in progress",
                "An operation is still running. Close Flow4K anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.requestInterruption()
            self.worker.wait(3000)

        event.accept()


# ============================================================
# APPLICATION ENTRY POINT
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = Flow4KApp()
    window.show()
    sys.exit(app.exec())
