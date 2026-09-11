"""Runtime patch for CGD automation on the self-hosted Windows runner.

The CGD Cloudflare path rejects Chromium headless, but the scraper does not need
an operator watching a browser. Keep Playwright headed for the anti-bot path while
placing the window off-screen and minimized. This preserves the browser execution
model without turning the job into a visible/manual workflow.
"""
from playwright.sync_api import BrowserType

_original_launch = BrowserType.launch


def _launch_offscreen(self, *args, **kwargs):
    launch_args = list(kwargs.pop("args", None) or [])
    required = [
        "--start-minimized",
        "--window-position=-32000,-32000",
        "--disable-background-networking",
    ]
    for flag in required:
        if flag not in launch_args:
            launch_args.append(flag)
    kwargs["headless"] = False
    kwargs["args"] = launch_args
    return _original_launch(self, *args, **kwargs)


BrowserType.launch = _launch_offscreen
print("PATCH_BROWSER_OFFSCREEN=OK", flush=True)
