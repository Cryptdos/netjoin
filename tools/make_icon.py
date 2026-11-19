"""Draws NetJoin's icon (a network tree with a join arrow) and writes .ico + .png files.

Run:  python tools/make_icon.py   (needs Pillow). Output goes to netjoin/static/.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "netjoin" / "static"
BLUE, DARK, WHITE = (31, 95, 191, 255), (23, 74, 148, 255), (255, 255, 255, 255)


def draw(size: int = 1024) -> Image.Image:
    s = size / 1024
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(220 * s), fill=BLUE)
    d.rounded_rectangle([0, int(560 * s), size - 1, size - 1], radius=int(220 * s), fill=DARK)
    d.rectangle([0, int(560 * s), size - 1, int(800 * s)], fill=DARK)
    d.rounded_rectangle([0, int(560 * s), size - 1, int(700 * s)], radius=0, fill=DARK)
    w = int(56 * s)                                   # line width
    # top box (the laptop) and a downward "join" arrow
    d.rounded_rectangle([int(372 * s), int(150 * s), int(652 * s), int(330 * s)], radius=int(40 * s), fill=WHITE)
    d.rectangle([int(512 * s) - w // 2, int(330 * s), int(512 * s) + w // 2, int(560 * s)], fill=WHITE)
    # bus and three device boxes
    d.rectangle([int(222 * s), int(560 * s) - w // 2, int(802 * s), int(560 * s) + w // 2], fill=WHITE)
    for cx in (250, 512, 774):
        d.rectangle([int(cx * s) - w // 2, int(560 * s), int(cx * s) + w // 2, int(680 * s)], fill=WHITE)
        d.rounded_rectangle([int((cx - 100) * s), int(680 * s), int((cx + 100) * s), int(840 * s)],
                            radius=int(32 * s), fill=WHITE)
    # highlight the middle device: the one we join
    d.rounded_rectangle([int(437 * s), int(730 * s), int(587 * s), int(790 * s)], radius=int(20 * s), fill=BLUE)
    return im


def main():
    big = draw()
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    big.save(OUT / "netjoin.ico", sizes=[(n, n) for n in sizes])
    big.resize((64, 64), Image.LANCZOS).save(OUT / "favicon.png")
    big.resize((256, 256), Image.LANCZOS).save(OUT / "icon-256.png")
    print("wrote", OUT / "netjoin.ico")


if __name__ == "__main__":
    main()
