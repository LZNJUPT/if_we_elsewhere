# -*- mode: python ; coding: utf-8 -*-
"""
IfWe · PyInstaller 打包配置（onedir，双击即用）

构建（在仓库根执行）：
    pip install pyinstaller pywebview keyring
    pyinstaller IfWe.spec --noconfirm           # 产物：dist/IfWe/IfWe.exe

要点（与 GUI 路线图 P3 对应）：
  - **onedir 而非 onefile**：启动快、杀软误报率低
  - `app/`（含 *.sql 与 phase15_web 静态资源）与 `sample_data/` 一并打包；
    运行时 data/ 仍在 exe 同级目录，只读资源从 _MEIPASS 读取
  - 打包结果不包含任何用户数据：data/ 由 .gitignore 与发布脚本双重排除
"""
from pathlib import Path

ROOT = Path(SPECPATH)          # noqa: F821  (PyInstaller 注入)

APP_MODULES = [
    "config", "profiles", "secret_store", "analyze_pipeline", "doctor",
    "phase1_ingest", "phase2_llm", "phase4_retrieval", "phase5_common", "phase5_llm",
    "phase5_a2_loop", "phase5_a3_buffer", "phase5_a5_pcc", "phase6_engine",
    "phase15_api", "phase15_dial_engine",
    "importers", "importers.base", "importers.registry", "importers.chatlab_jsonl",
    "importers.wecomsg_csv", "importers.telegram_json",
]

HIDDEN = APP_MODULES + [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "keyring.backends.Windows", "keyring.backends.SecretService",
    "webview.platforms.edgechromium", "webview.platforms.winforms",
]

datas = [
    (str(ROOT / "app"), "app"),                       # 含 *.sql 结构与 phase15_web 静态资源
    (str(ROOT / "sample_data"), "sample_data"),
    (str(ROOT / "config.example.yaml"), "."),
]
icon = ROOT / "app" / "phase15_web" / "icon.ico"
if icon.is_file():
    datas.append((str(icon), "."))

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT), str(ROOT / "app")],
    binaries=[],
    datas=datas,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明确排除：研究脚本与重型可选件不进发布物
    excludes=["poc", "tests", "matplotlib", "scipy", "pandas", "IPython", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IfWe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX 会显著提高杀软误报率，明确关闭
    console=False,                   # 双击不弹黑框；调试可改 True
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon) if icon.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="IfWe",
)
