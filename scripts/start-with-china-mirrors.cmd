@echo off
rem First launch from mainland China: download Python from npmmirror and the model from hf-mirror.com.
rem Only the first launch downloads anything; afterwards start "Local Photo Search.exe" directly.
rem Python packages themselves come from files.pythonhosted.org and download.pytorch.org (pinned in uv.lock).
set "UV_PYTHON_INSTALL_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone"
set "HF_ENDPOINT=https://hf-mirror.com"
start "" "%~dp0Local Photo Search.exe"
