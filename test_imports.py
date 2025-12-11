print('>>> SCRIPT START')
import sys
try:
    import tkinter as tk
    print('TKINTER OK')
except Exception as e:
    print('TKINTER ERROR:', e)
try:
    import pyautogui
    print('PYAUTOGUI OK')
except Exception as e:
    print('PYAUTOGUI ERROR:', e)
print('>>> SCRIPT END')