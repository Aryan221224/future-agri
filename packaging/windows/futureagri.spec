# PyInstaller spec for FutureAgri.exe (one folder: FutureAgri\FutureAgri.exe + _internal).
# Build from the repository root:  pyinstaller packaging/windows/futureagri.spec --noconfirm
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ["packages/fa_core", "services/twin", "services/planner", "services/loop", "services/farm",
       "services/scan", "services/drone", "services/runner", "services/console", "services/agent",
       "services/aircraft"]
OURS = ["fa_core", "fa_twin", "fa_planner", "fa_loop", "fa_farm", "fa_scan", "fa_drone", "fa_runner",
        "fa_console", "fa_agent", "fa_aircraft"]
# Third-party packages that carry data files, compiled extensions or plugins.
THIRD = ["numpy", "pymavlink", "serial", "pcse", "pyproj", "rasterio", "shapely", "ortools", "duckdb", "pyarrow", "icechunk", "zarr",
         "numcodecs", "xarray", "reportlab", "uvicorn", "pystac_client", "fsspec", "s3fs", "botocore",
         "aiobotocore", "certifi", "traitlets", "pandas", "scipy", "yaml", "PIL", "multipart", "fastapi",
         "starlette", "pydantic", "pydantic_core", "requests"]

pathex = [str(ROOT / p) for p in SRC]
datas = [
    (str(ROOT / "vendor"), "vendor"),
    (str(ROOT / "services/console/fa_console/static"), "fa_console/static"),
    (str(ROOT / "services/drone/fa_drone/data"), "fa_drone/data"),
    (str(ROOT / "services/farm/fa_farm/data"), "fa_farm/data"),
    (str(ROOT / "services/scan/fa_scan/data"), "fa_scan/data"),
    (str(ROOT / "services/agent/fa_agent/data"), "fa_agent/data"),
    (str(ROOT / "services/aircraft/fa_aircraft/params"), "fa_aircraft/params"),
]
binaries, hidden = [], []
import sys
sys.path[:0] = pathex
for m in OURS:
    hidden += collect_submodules(m)
def _no_tests(name: str) -> bool:
    return ".tests" not in name and ".testing" not in name and not name.endswith(".conftest")


for m in THIRD:
    try:
        hidden += collect_submodules(m, filter=_no_tests)
        datas += collect_data_files(m, excludes=["**/tests/**", "**/testing/**"])
        binaries += collect_dynamic_libs(m)
    except Exception as e:  # a package not installed in this environment
        print(f"spec: skipping {m}: {e}")
hidden += ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
           "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"]

a = Analysis([str(ROOT / "packaging/windows/futureagri.py")], pathex=pathex, binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=["torch", "tensorflow", "matplotlib", "IPython", "jupyter",
                                             "notebook", "tkinter", "pytest"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="FutureAgri", console=True,
          icon=None, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="FutureAgri", upx=False)
