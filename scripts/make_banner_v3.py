"""
Toma el banner Gengar provisto (IMG_0731...) y genera un GIF animado:
  - Ojos rojos de TODOS los Gengar pulsan/brillan
  - Texto "Marke" en Bangers blanco justo debajo del Gengar central
  - El aura violeta del centro tiene un leve pulso de brillo

Salida: attached_assets/gengar_banner.gif (960x524, 24 frames a 12fps)
"""

import math, os
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import imageio.v3 as iio

SRC     = "attached_assets/IMG_0731_1777443720479.png"
FONT_B  = "/tmp/Bangers.ttf"
OUTPUT  = "attached_assets/gengar_banner.gif"

# Discord banner: max 960 de ancho para mantenerlo bajo 8 MB
OUT_W, OUT_H = 960, 524
FPS          = 12
N_FRAMES     = 24    # 2 s loop


# ─── Detección de ojos ────────────────────────────────────────────────────────

def detect_eye_clusters(arr: np.ndarray, min_size: int = 6):
    """
    Devuelve lista de (cy, cx, radius) para cada cluster de ojos rojos.
    Los ojos de Gengar son rojo intenso sobre fondo oscuro.
    """
    r = arr[:,:,0].astype(int)
    g = arr[:,:,1].astype(int)
    b = arr[:,:,2].astype(int)
    a = arr[:,:,3].astype(int) if arr.shape[2] == 4 else np.full(r.shape, 255)

    # Máscara: rojo brillante, poco verde y azul
    mask = (r > 160) & (r - g > 80) & (r - b > 80) & (a > 60)

    from scipy import ndimage as ndi
    labeled, n = ndi.label(mask)

    clusters = []
    for i in range(1, n + 1):
        region = np.argwhere(labeled == i)
        if len(region) < min_size:
            continue
        cy = int(region[:,0].mean())
        cx = int(region[:,1].mean())
        rh = (region[:,0].max() - region[:,0].min()) / 2
        rw = (region[:,1].max() - region[:,1].min()) / 2
        radius = max(int(max(rh, rw)) + 4, 6)
        clusters.append((cy, cx, radius))

    return clusters


# ─── Capas de glow ────────────────────────────────────────────────────────────

def eye_glow_frame(size, clusters, pulse):
    """Imagen RGBA con halos rojos sobre cada ojo, intensidad = pulse (0..1)."""
    layer = Image.new("RGBA", size, (0,0,0,0))
    draw  = ImageDraw.Draw(layer)

    for (cy, cx, base_r) in clusters:
        # Halo interior rojo-naranja brillante
        r1 = int(base_r * (1.0 + 0.5 * pulse))
        alpha1 = int(200 + 55 * pulse)
        draw.ellipse([cx-r1, cy-r1, cx+r1, cy+r1],
                     fill=(255, 60, 30, alpha1))

        # Halo exterior más grande y suave
        r2 = int(base_r * (2.2 + 1.0 * pulse))
        alpha2 = int(80 + 80 * pulse)
        draw.ellipse([cx-r2, cy-r2, cx+r2, cy+r2],
                     fill=(255, 80, 40, alpha2))

    # Difuminar todo el layer para que el glow quede suave
    return layer.filter(ImageFilter.GaussianBlur(base_r * 0.6 if clusters else 4))


def aura_pulse_layer(size, cx, cy, pulse):
    """Leve brillo blanco-violeta adicional sobre el aura central."""
    layer = Image.new("RGBA", size, (0,0,0,0))
    draw  = ImageDraw.Draw(layer)
    r = int(120 + 40 * pulse)
    a = int(30 + 25 * pulse)
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(200, 150, 255, a))
    return layer.filter(ImageFilter.GaussianBlur(30))


# ─── Texto "Marke" ────────────────────────────────────────────────────────────

def make_text_layer(size, font, pulse):
    """
    "Marke" en Bangers blanco con sombra violeta oscura.
    Leve escala de brillo que pulsa con los ojos.
    """
    W, H  = size
    layer = Image.new("RGBA", size, (0,0,0,0))

    text  = "Marke"
    bb    = font.getbbox(text)
    tw, th = bb[2]-bb[0], bb[3]-bb[1]

    # Centrado horizontal, en el tercio inferior (justo debajo del Gengar central)
    x = (W - tw) // 2 - bb[0]
    y = int(H * 0.70) - bb[1]    # 70% de la altura

    draw = ImageDraw.Draw(layer)

    # Sombra violeta difusa
    shadow = Image.new("RGBA", size, (0,0,0,0))
    sd = ImageDraw.Draw(shadow)
    for dx, dy in [(3,3),(2,2),(1,1)]:
        sd.text((x+dx, y+dy), text, font=font, fill=(60, 0, 100, 180))
    shadow = shadow.filter(ImageFilter.GaussianBlur(5))
    layer = Image.alpha_composite(layer, shadow)

    # Texto blanco principal
    draw = ImageDraw.Draw(layer)
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    # Glow blanco encima (pulsa suavemente)
    glow = Image.new("RGBA", size, (0,0,0,0))
    gd   = ImageDraw.Draw(glow)
    gd.text((x, y), text, font=font, fill=(255, 255, 255, int(110 * pulse)))
    glow = glow.filter(ImageFilter.GaussianBlur(4))
    layer = Image.alpha_composite(layer, glow)

    # Re-dibujar el texto encima para que quede nítido
    draw2 = ImageDraw.Draw(layer)
    draw2.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    return layer


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    try:
        from scipy import ndimage
    except ImportError:
        os.system("pip install scipy -q")

    font_size = max(int(OUT_H * 0.22), 90)   # ~22% de la altura
    font = ImageFont.truetype(FONT_B, font_size)

    # Cargar y redimensionar imagen base
    base_orig = Image.open(SRC).convert("RGBA")
    base      = base_orig.resize((OUT_W, OUT_H), Image.LANCZOS)
    arr       = np.array(base)

    # Detectar ojos
    try:
        from scipy import ndimage
        clusters = detect_eye_clusters(arr)
    except ImportError:
        clusters = []
    print(f"Clusters de ojos detectados: {len(clusters)}")
    for c in clusters:
        print(f"  ojo en ({c[1]}, {c[0]}), radio={c[2]}")

    # Centro aproximado del Gengar central (para el aura)
    cx, cy = OUT_W // 2, int(OUT_H * 0.42)

    frames = []
    for i in range(N_FRAMES):
        t     = i / N_FRAMES
        pulse = 0.5 + 0.5 * math.sin(2 * math.pi * t)

        frame = base.copy()

        # Aura pulse leve
        ap = aura_pulse_layer((OUT_W, OUT_H), cx, cy, pulse)
        frame = Image.alpha_composite(frame, ap)

        # Ojos pulsantes
        if clusters:
            eg = eye_glow_frame((OUT_W, OUT_H), clusters, pulse)
            frame = Image.alpha_composite(frame, eg)

        # Texto "Marke"
        tl = make_text_layer((OUT_W, OUT_H), font, pulse)
        frame = Image.alpha_composite(frame, tl)

        frames.append(np.array(frame.convert("RGB")))

    iio.imwrite(
        OUTPUT, frames,
        plugin="pillow", format="GIF",
        loop=0, duration=int(1000 / FPS),
    )
    size_mb = os.path.getsize(OUTPUT) / 1e6
    print(f"\nBanner guardado: {OUTPUT}  ({size_mb:.2f} MB, {N_FRAMES} frames)")


if __name__ == "__main__":
    main()
