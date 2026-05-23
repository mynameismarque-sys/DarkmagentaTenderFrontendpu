"""
Banner Discord - estilo "Legit Army" pero con Gengar y "MARKE SENSI":
  - Fondo negro
  - Rayos de luz blancos desde el centro (igual que la referencia)
  - "MARKE" en Bangers blanco grande con sombra 3D
  - "SENSI" en Permanent Marker violeta debajo
  - Gengar x2 a los costados con ojos rojos pulsantes (GIF animado)

Salida: attached_assets/gengar_banner.gif (960x540, 24 frames)
"""

import math, os
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

GENGAR_SRC   = "attached_assets/gengar_nobg.png"
FONT_MARKE   = "/tmp/Bangers.ttf"
FONT_SENSI   = "/tmp/DancingScript.ttf"   # cursiva elegante, disponible
OUTPUT       = "attached_assets/gengar_banner.gif"

W, H       = 960, 540
FPS        = 12
N_FRAMES   = 24          # 2 s en loop


# ─────────────────────────── helpers ──────────────────────────────────────────

def make_spotlight(size, cx, cy, intensity=1.0):
    """Capa de rayos/spotlight radiales desde (cx,cy), igual que la referencia."""
    W, H = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw  = ImageDraw.Draw(layer)

    import random
    rng = random.Random(42)

    n_rays = 18
    for i in range(n_rays):
        angle   = rng.uniform(0, 2 * math.pi)
        spread  = rng.uniform(0.03, 0.14)        # apertura del rayo
        length  = max(W, H) * 1.5
        alpha   = int(rng.uniform(20, 55) * intensity)

        a1 = angle - spread / 2
        a2 = angle + spread / 2

        px1 = int(cx + math.cos(a1) * length)
        py1 = int(cy + math.sin(a1) * length)
        px2 = int(cx + math.cos(a2) * length)
        py2 = int(cy + math.sin(a2) * length)

        draw.polygon([(cx, cy), (px1, py1), (px2, py2)],
                     fill=(255, 255, 255, alpha))

    # Blur para suavizar los rayos
    layer = layer.filter(ImageFilter.GaussianBlur(6))
    return layer


def make_center_glow(size, cx, cy, radius=220, alpha=110):
    """Halo blanco suave en el centro detrás del texto."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    for r in range(radius, 0, -8):
        a = int(alpha * (1 - r / radius) ** 2)
        draw = ImageDraw.Draw(layer)
        draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(255, 255, 255, a))
    return layer.filter(ImageFilter.GaussianBlur(20))


def render_text_layer(size, font_marke, font_sensi):
    """Dibuja MARKE (blanco) + SENSI (violeta) con sombras, igual que la referencia."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw  = ImageDraw.Draw(layer)
    W, H  = size

    # --- "MARKE" ---
    text1 = "MARKE"
    bb1   = font_marke.getbbox(text1)
    tw1   = bb1[2] - bb1[0]
    th1   = bb1[3] - bb1[1]
    x1    = (W - tw1) // 2 - bb1[0]
    y1    = H // 2 - th1 - 10 - bb1[1]

    # sombra 3D gris oscuro (desplazada 4px)
    for dx, dy in [(4,4),(3,3),(2,2)]:
        draw.text((x1+dx, y1+dy), text1, font=font_marke, fill=(40, 40, 50, 200))

    # texto blanco principal
    draw.text((x1, y1), text1, font=font_marke, fill=(255, 255, 255, 255))

    # contorno muy fino negro
    for dx, dy in [(-1,0),(1,0),(0,-1),(0,1)]:
        draw.text((x1+dx, y1+dy), text1, font=font_marke, fill=(0, 0, 0, 120))
    draw.text((x1, y1), text1, font=font_marke, fill=(255, 255, 255, 255))

    # --- "SENSI" ---
    text2 = "SENSI"
    bb2   = font_sensi.getbbox(text2)
    tw2   = bb2[2] - bb2[0]
    th2   = bb2[3] - bb2[1]
    x2    = (W - tw2) // 2 - bb2[0]
    y2    = y1 + th1 + 4 - bb2[1]

    # sombra violeta oscuro
    for dx, dy in [(3,3),(2,2)]:
        draw.text((x2+dx, y2+dy), text2, font=font_sensi, fill=(60, 0, 90, 180))

    # texto violeta brillante
    draw.text((x2, y2), text2, font=font_sensi, fill=(160, 60, 255, 255))

    # leve glow blanco encima de SENSI
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    gd.text((x2, y2), text2, font=font_sensi, fill=(200, 100, 255, 140))
    glow = glow.filter(ImageFilter.GaussianBlur(4))
    layer = Image.alpha_composite(layer, glow)
    final = ImageDraw.Draw(layer)
    final.text((x2, y2), text2, font=font_sensi, fill=(160, 60, 255, 255))

    return layer, (x1, y1, tw1, th1), (x2, y2, tw2, th2)


def find_eyes(arr: np.ndarray):
    """Detecta píxeles de ojos (rojo/rosa brillante) en el Gengar."""
    r, g, b = arr[:,:,0], arr[:,:,1], arr[:,:,2]
    a = arr[:,:,3] if arr.shape[2] == 4 else np.ones_like(r) * 255
    # Ojos de Gengar: rojo > 180, azul < 160, verde < rojo, con alfa > 50
    mask = (r.astype(int) - b.astype(int) > 60) & (r > 150) & (a > 50)
    coords = np.argwhere(mask)
    return coords   # (row, col)


def eye_glow_layer(gengar_size, eye_coords, pulse, side="left"):
    """Capa de brillo en los ojos según posición detectada."""
    gh, gw = gengar_size
    layer  = Image.new("RGBA", (gw, gh), (0,0,0,0))
    draw   = ImageDraw.Draw(layer)

    if len(eye_coords) == 0:
        # Fallback: zona estimada de ojos (40% altura, 40-60% ancho)
        cy, cx = int(gh * 0.40), int(gw * 0.50)
        clusters = [(cy, cx - int(gw*0.10)), (cy, cx + int(gw*0.10))]
    else:
        # Agrupar puntos en dos clusters (ojo izq y ojo der)
        ys, xs = eye_coords[:,0], eye_coords[:,1]
        mid_x  = int(xs.mean())
        left   = eye_coords[xs < mid_x]
        right  = eye_coords[xs >= mid_x]
        clusters = []
        for grp in [left, right]:
            if len(grp):
                clusters.append((int(grp[:,0].mean()), int(grp[:,1].mean())))

    alpha_base = int(180 + 70 * pulse)
    for (cy, cx) in clusters:
        rad = int(12 + 14 * pulse)
        draw.ellipse([cx-rad, cy-rad, cx+rad, cy+rad],
                     fill=(255, 80, 80, alpha_base))
        # Halo exterior
        rad2 = rad + 10
        draw.ellipse([cx-rad2, cy-rad2, cx+rad2, cy+rad2],
                     fill=(255, 150, 150, int(60 * pulse)))

    return layer.filter(ImageFilter.GaussianBlur(6))


# ─────────────────────────── main ─────────────────────────────────────────────

def main():
    # Cargar fuentes
    font_marke = ImageFont.truetype(FONT_MARKE, 180)
    font_sensi = ImageFont.truetype(FONT_SENSI,  62)

    # Gengar RGBA (transparente)
    gengar_orig = Image.open(GENGAR_SRC).convert("RGBA")
    gengar_h    = int(H * 0.78)             # altura = 78% del banner
    gengar_w    = int(gengar_h * gengar_orig.width / gengar_orig.height)
    gengar_l    = gengar_orig.resize((gengar_w, gengar_h), Image.LANCZOS)
    gengar_r    = gengar_l.transpose(Image.FLIP_LEFT_RIGHT)

    # Posiciones: gengar izquierdo y derecho
    gx_l = -gengar_w // 7            # se sale un poco por la izquierda
    gy   = (H - gengar_h) // 2 + 20  # centrado vertical + bajado un poco
    gx_r = W - gengar_w + gengar_w // 7

    # Detectar ojos
    arr_l = np.array(gengar_l)
    arr_r = np.array(gengar_r)
    eyes_l = find_eyes(arr_l)
    eyes_r = find_eyes(arr_r)

    # Spotlight y glow de centro (capas estáticas)
    cx, cy = W // 2, H // 2
    spot   = make_spotlight((W, H), cx, cy)
    cglow  = make_center_glow((W, H), cx, cy, radius=250, alpha=90)

    # Capa de texto (estática)
    text_layer, _, _ = render_text_layer((W, H), font_marke, font_sensi)

    # ─── Generar frames ───────────────────────────────────────────────────────
    frames = []
    for i in range(N_FRAMES):
        t     = i / N_FRAMES
        pulse = 0.5 + 0.5 * math.sin(2 * math.pi * t)   # 0..1

        # Fondo negro
        frame = Image.new("RGBA", (W, H), (6, 4, 12, 255))

        # Rayos (intensidad levemente pulsante)
        sp = make_spotlight((W, H), cx, cy, intensity=0.7 + 0.3 * pulse)
        frame = Image.alpha_composite(frame, sp)
        frame = Image.alpha_composite(frame, cglow)

        # Gengar izquierdo + glow de ojos
        g_left = gengar_l.copy()
        eye_layer_l = eye_glow_layer((gengar_h, gengar_w), eyes_l, pulse, "left")
        g_left.paste(Image.alpha_composite(g_left, eye_layer_l))
        frame.paste(g_left, (gx_l, gy), g_left)

        # Gengar derecho + glow de ojos
        g_right = gengar_r.copy()
        eye_layer_r = eye_glow_layer((gengar_h, gengar_w), eyes_r, pulse, "right")
        g_right.paste(Image.alpha_composite(g_right, eye_layer_r))
        frame.paste(g_right, (gx_r, gy), g_right)

        # Texto encima
        frame = Image.alpha_composite(frame, text_layer)

        frames.append(np.array(frame.convert("RGB")))

    # Guardar GIF
    import imageio.v3 as iio
    iio.imwrite(
        OUTPUT, frames,
        plugin="pillow", format="GIF",
        loop=0, duration=int(1000 / FPS),
    )
    size_mb = os.path.getsize(OUTPUT) / 1e6
    print(f"Banner guardado: {OUTPUT}  ({size_mb:.2f} MB, {N_FRAMES} frames)")


if __name__ == "__main__":
    main()
