from pathlib import Path
from playwright.sync_api import sync_playwright

FLOW_URL = "https://labs.google/fx/tools/flow"
PROFILE_DIR = Path(__file__).resolve().parent / "flow-profile"

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=False,
        no_viewport=True,
    )

    page = context.pages[0] if context.pages else context.new_page()
    page.goto(FLOW_URL)

    print("Flow opened in a dedicated browser profile.")
    print("If Google permits sign-in, sign in manually.")
    print("Close the browser when you're finished.")

  
