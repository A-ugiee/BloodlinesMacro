import asyncio
import traceback

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import PartyUI as P

print("1. OCR packages:", "installed" if P.OcrEngine else "MISSING - run: py -V:3.13 -m pip install -r requirements.txt")
engine = P.ocr_engine()
print("2. OCR engine:", f"ok ({engine.recognizer_language.display_name})" if engine else "MISSING - install the Windows OCR language pack")

if engine:
    img = Image.new("RGB", (90, 44), (30, 30, 34))
    ImageDraw.Draw(img).text((12, 2), "39", fill=(240, 230, 250), font=ImageFont.truetype("arial.ttf", 32))
    a = np.asarray(img).astype(int)
    mask = P.clean((a[:, :, 1] > 140) & (a.min(axis=2) > 120))
    try:
        print("3. Test read (should be 39):", asyncio.run(P.ocr_number(engine, mask)))
    except Exception:
        print("3. Test read FAILED:")
        traceback.print_exc()

input("\nPress Enter to close...")
