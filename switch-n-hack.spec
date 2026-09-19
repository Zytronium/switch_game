"""PyInstaller specification for the Switch 'n Hack Linux executable."""

from pathlib import Path
import importlib.util

from PyInstaller.utils.hooks import collect_submodules


PROJECT_ROOT = Path(SPECPATH)
GAME_ROOT = PROJECT_ROOT / "game"

switch_net_spec = importlib.util.find_spec("switch_net")
if switch_net_spec is None or switch_net_spec.origin is None:
    raise RuntimeError(
        "switch_net is not installed; build and install the Maturin wheel before "
        "running PyInstaller."
    )

switch_net_binary = Path(switch_net_spec.origin)
if not switch_net_binary.is_file():
    raise RuntimeError(f"switch_net extension does not exist: {switch_net_binary}")


a = Analysis(
    [str(GAME_ROOT / "main.py")],
    pathex=[str(GAME_ROOT)],
    binaries=[(str(switch_net_binary), ".")],
    datas=[(str(GAME_ROOT / "tutorial.json"), ".")],
    hiddenimports=["switch_net", *collect_submodules("curses")],
    excludes=[
        "test_switch_net",
        "tests",
        "pytest",
        "setuptools",
        "pip",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="switch-n-hack",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)