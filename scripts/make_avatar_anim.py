"""
Genera avatar animado de Gengar con:
  - Ojos brillantes pulsantes (glow rojo/rosa)
  - Aura violeta pulsante alrededor del cuerpo
Salida: attached_assets/gengar_avatar.gif  (512x512, 24 frames, ~12fps)
"""
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
import imageio.v3 as iio

SRC  = "attached_assets/gengar_nobg.png"
DEST = "attached_assets/gengar_avatar.gif"
SIZE = 512
FPS  = 12
DURATION_S = 2.0
N_FRAMES = int(FPS * DURATION_S)   # 24


def add_aura(base_rgba: np.ndarray, t: float) -> np.ndarray:
    """Dibuja un halo violeta pulsante alrededor de la silueta."""
    img = Image.fromarray(base_rgba, "RGBA")
    alpha = img.split()[3]          # canal alfa = silueta del pokemon

    # Expandimos la máscara y la coloreamos de violeta
    pulse = 0.5 + 0.5 * math.sin(2 * math.pi * t)   # 0..1
    radius = int(8 + 14 * pulse)                      # 8-22 px
    glow_alpha = int(80 + 130 * pulse)                # 80-210

    mask = alpha.filter(ImageFilter.MaxFilter(radius * 2 + 1))
    mask = mask.filter(ImageFilter.GaussianBlur(radius))

    # Capa de aura violeta semitransparente
    aura = Image.new("RGBA", img.size, (180, 80, 255, 0))
    aura_pixels = np.array(aura)
    mask_arr = np.array(mask)
    aura_pixels[:, :, 3] = (mask_arr * glow_alpha / 255).clip(0, 255).astype(np.uint8)
    aura = Image.fromarray(aura_pixels, "RGBA")

    # Componer: aura debajo del pokemon
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(aura, (0, 0))
    out.paste(img, (0, 0), img)
    return np.array(out)


def glow_eyes(base_rgba: np.ndarray, t: float) -> np.ndarray:
    """Resalta los píxeles rojizos/rosados (ojos de Gengar) con un glow pulsante."""
    arr = base_rgba.astype(np.float32)
    r, g, b, a = arr[:,:,0], arr[:,:,1], arr[:,:,2], arr[:,:,3]

    # Máscara: píxeles con mucho rojo y poca saturación de azul = ojos
    eye_mask = (r > 160) & (b < 180) & (a > 50)
    if not eye_mask.any():
        # Fallback: usar zona superior central del Gengar
        h, w = arr.shape[:2]
        cy, cx = int(h * 0.38), int(w * 0.50)
        radius = int(h * 0.07)
        yy, xx = np.ogrid[:h, :w]
        eye_mask = ((yy - cy)**2 + (xx - cx)**2) < radius**2

    pulse = 0.55 + 0.45 * math.sin(2 * math.pi * t)

    # Hacer los píxeles de ojo más brillantes
    for c in range(3):
        arr[:,:,c] = np.where(eye_mask, np.clip(arr[:,:,c] * (1 + pulse * 1.8), 0, 255), arr[:,:,c])

    # Agregar un halo difuso sobre los ojos
    eye_img = Image.new("RGBA", (base_rgba.shape[1], base_rgba.shape[0]), (0,0,0,0))
    draw = ImageDraw.Draw(eye_img)
    coords = np.argwhere(eye_mask)
    if len(coords):
        ys, xs = coords[:,0], coords[:,1]
        cy, cx = int(ys.mean()), int(xs.mean())
        rad = max(int((ys.max()-ys.min())/2 + 12), 12)
        draw.ellipse([cx-rad, cy-rad, cx+rad, cy+rad],
                     fill=(255, 200, 255, int(120 * pulse)))
    eye_img = eye_img.filter(ImageFilter.GaussianBlur(10))
    base = Image.fromarray(arr.astype(np.uint8), "RGBA")
    base.paste(eye_img, mask=eye_img)
    return np.array(base)


def make_frame(base: np.ndarray, frame_idx: int) -> np.ndarray:
    t = frame_idx / N_FRAMES              # 0..1 en el loop
    arr = add_aura(base, t)
    arr = glow_eyes(arr, t)

    # Fondo muy oscuro (casi negro con tinte violeta)
    bg = Image.new("RGBA", (SIZE, SIZE), (8, 4, 16, 255))
    fg = Image.fromarray(arr, "RGBA")
    bg.paste(fg, (0, 0), fg)
    return np.array(bg.convert("RGB"))


def main():
    src = Image.open(SRC).convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
    base = np.array(src)

    frames = [make_frame(base, i) for i in range(N_FRAMES)]

    iio.imwrite(
        DEST,
        frames,
        plugin="pillow",
        format="GIF",
        loop=0,
        duration=int(1000 / FPS),
    )
    import os
    size_mb = os.path.getsize(DEST) / 1e6
    print(f"Avatar GIF guardado en {DEST} ({size_mb:.2f} MB, {N_FRAMES} frames)")


if __name__ == "__main__":
    main()
