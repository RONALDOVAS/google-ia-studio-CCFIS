"""Runtime patches for CGD automation on the self-hosted Windows runner."""
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

# The operational workflow already imports this runtime patch before starting
# scraper_sync_incremental.py. Chain the real CGD frequency-route fallback here
# so the workflow needs no second manual import or dispatch change.
try:
    import frequency_runtime_patch  # noqa: F401
except Exception as exc:
    print(f"PATCH_FREQUENCIA_LISTA_ERRO={exc!r}", flush=True)
else:
    print("PATCH_FREQUENCIA_LISTA=OK", flush=True)
