@echo off
:: PlantNet2Anki - Windows launcher

python plantnet2anki_gui.py 2>nul
if errorlevel 1 (
    py plantnet2anki_gui.py
)
