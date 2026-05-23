import asyncio
import sys
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

sys.path.insert(0, "/home/runner/workspace")

async def main():
    from bot import latingm_scraper

    ORDER_ID   = "291517"
    ID_FF      = "155164951"
    DIAMONDS   = 110

    print(f"\n{'='*60}")
    print(f"Completando pedido {ORDER_ID} — {DIAMONDS}💎 para ID FF {ID_FF}")
    print(f"{'='*60}\n")

    screenshot, resultado = await latingm_scraper.completar_pedido_existente(
        order_id=ORDER_ID,
        id_freefire=ID_FF,
        diamonds=DIAMONDS,
    )

    print(f"\n{'='*60}")
    print(f"RESULTADO: {resultado}")
    print(f"{'='*60}\n")

    if screenshot:
        path = "/tmp/completar_screenshot.png"
        with open(path, "wb") as f:
            f.write(screenshot)
        print(f"Screenshot guardado en {path}")

asyncio.run(main())
