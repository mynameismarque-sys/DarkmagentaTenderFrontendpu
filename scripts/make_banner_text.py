"""
Toma el banner GIF existente y superpone:
  - Texto "Marke" en Dancing Script (cursiva blanca elegante)
  - Sombra suave violeta detrás del texto
  - Brillo/glow sutil que pulsa con la animación
  - Un underline decorativo que aparece gradualmente

Salida: attached_assets/gengar_banner.gif  (640x360, bajo 8MB)
"""
import math, os
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import imageio.v3 as iio

SRC_GIF   = "attached_assets/gengar_banner.gif"   # banner animado existente
FONT_PATH = "/tmp/DancingScript.ttf"
DEST_GIF  = "attached_assets/gengar_banner.gif"
TMP_OUT   = "attached_assets/gengar_banner_new.gif"

FONT_SIZE = 110          # tamaño del texto "Marke"
TEXT      = "Marke"
TEXT_X_FRAC = 0.50       # centrado horizontal
TEXT_Y_FRAC = 0.58       # un poco más abajo del centro


def read_gif_frames(path: str):
    """Lee todos los frames de un GIF y los devuelve como lista de PIL.Image RGB."""
    raw = iio.imread(path, plugin="pillow", index=None)
    frames = []
    for f in raw:
        img = Image.fromarray(f).convert("RGBA").resize((640, 360), Image.LANCZOS)
        frames.append(img)
    return frames


def make_text_layer(size: tuple, t: float, font: ImageFont.FreeTypeFont) -> Image.Image:
    """
    Devuelve una imagen RGBA con el texto "Marke" animado.
    t: 0..1 posición en el loop de animación.
    """
    w, h = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw  = ImageDraw.Draw(layer)

    # Bounding box del texto
    bbox = font.getbbox(TEXT)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = int(w * TEXT_X_FRAC - tw / 2)
    y = int(h * TEXT_Y_FRAC - th / 2)

    # Pulsación suave
    pulse = 0.5 + 0.5 * math.sin(2 * math.pi * t)   # 0..1

    # 1) Sombra/glow violeta difusa (debajo)
    shadow_alpha = int(180 + 60 * pulse)
    shadow_layer = Image.new("RGBA", size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    sd.text((x + 2, y + 4), TEXT, font=font, fill=(160, 60, 255, shadow_alpha))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(8))
    layer.paste(Image.alpha_composite(layer, shadow_layer))

    # 2) Texto blanco principal
    draw.text((x, y), TEXT, font=font, fill=(255, 255, 255, 245))

    # 3) Resplandor blanco fino encima (intensidad pulsante)
    glow_layer = Image.new("RGBA", size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    gd.text((x, y), TEXT, font=font, fill=(255, 255, 255, int(160 * pulse)))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(3))

    result = Image.alpha_composite(layer, glow_layer)

    # 4) Línea decorativa bajo el texto (aparece suavemente)
    ld = ImageDraw.Draw(result)
    line_y = y + th + 6
    line_alpha = int(180 + 60 * pulse)
    line_color = (255, 255, 255, line_alpha)
    ld.line([(x + tw//6, line_y), (x + tw - tw//6, line_y)],
            fill=line_color, width=2)

    return result


def main():
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    frames_pil = read_gif_frames(SRC_GIF)
    n = len(frames_pil)

    out_frames = []
    for i, frame in enumerate(frames_pil):
        t = i / max(n - 1, 1)
        text_layer = make_text_layer(frame.size, t, font)
        composite = Image.alpha_composite(frame, text_layer)
        out_frames.append(np.array(composite.convert("RGB")))

    iio.imwrite(
        TMP_OUT,
        out_frames,
        plugin="pillow",
        format="GIF",
        loop=0,
        duration=125,   # ~8fps
    )

    os.replace(TMP_OUT, DEST_GIF)
    size_mb = os.path.getsize(DEST_GIF) / 1e6
    print(f"Banner GIF guardado en {DEST_GIF} ({size_mb:.2f} MB, {n} frames)")


if __name__ == "__main__":
    main()
