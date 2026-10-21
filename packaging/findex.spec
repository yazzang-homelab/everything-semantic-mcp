# PyInstaller spec - one-file `findex` executable for Windows, macOS and Linux.
# Build: pyinstaller packaging/findex.spec   (run on each target OS; CI does this)
# The semantic model (~120 MB) is not bundled: it downloads on first semantic use and is cached.
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

def _no_cli(name: str) -> bool:
    # mcp.cli needs typer (mcp[cli]); findex never uses the MCP CLI.
    return ".cli" not in name


hidden = (
    collect_submodules("everything_semantic_mcp")
    + collect_submodules("mcp", filter=_no_cli)
    + collect_submodules("model2vec")
    + collect_submodules("uvicorn")
    + ["tokenizers", "safetensors", "safetensors.numpy", "huggingface_hub"]
)
datas = (
    collect_data_files("model2vec")
    + collect_data_files("mcp")
    + copy_metadata("mcp")
    + copy_metadata("model2vec")
    + copy_metadata("huggingface_hub")
    + copy_metadata("tokenizers")
    + copy_metadata("safetensors")
    + copy_metadata("numpy")
)

a = Analysis(
    ["findex_entry.py"],
    pathex=["../src"],
    hiddenimports=hidden,
    datas=datas,
    excludes=["torch", "tensorflow", "transformers", "onnxruntime", "IPython", "matplotlib", "tkinter",
              "PIL", "pandas", "scipy", "sklearn", "pytest", "typer", "mcp.cli"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="findex",
    console=True,  # MCP speaks over stdio; the UI prints its URL to the console
    upx=False,
    strip=False,
    target_arch=None,
    icon=None,
)
