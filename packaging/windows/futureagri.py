"""FutureAgri.exe: start the Mission Console and open it in the browser.

Frozen entry point for the Windows build (PyInstaller). Arguments are passed on to
``python -m fa_console`` (e.g. ``FutureAgri.exe --port 8100``); ``--no-browser`` skips
opening the browser. Farms and plans are kept in %USERPROFILE%\\.futureagri, as with the
script launcher.
"""
import multiprocessing
import sys


def run() -> int:
    multiprocessing.freeze_support()           # plan iterations use spawned worker processes
    args = sys.argv[1:]
    if "--no-browser" in args:
        args.remove("--no-browser")
    elif "--open" not in args:
        args.append("--open")
    from fa_console.__main__ import main
    return main(args)


if __name__ == "__main__":
    sys.exit(run())
