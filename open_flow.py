
import subprocess
import time
import webbrowser

FLOW_URL = "https://labs.google/fx/tools/flow"

# Ask the existing Chrome window to open Flow in a new tab.
subprocess.Popen([
    "cmd", "/c", "start", "", "chrome",
    "--new-tab", FLOW_URL
], shell=False)

print("Requested Google Flow in a new Chrome tab.")
