#!/usr/bin/env python3
"""Une dos capturas del mismo tamaño con un corte diagonal.

    corte_diagonal.py ARRIBA.png ABAJO.png SALIDA.png [--banda 0.08] [--pixel 4]

La mitad superior sale de ARRIBA y la inferior de ABAJO. La diagonal no es una
línea dura: es una franja de píxeles sueltos (bloques de --pixel lados) donde la
probabilidad de tomar ABAJO crece de 0 a 1 a lo ancho de la franja. La semilla
es fija, así que regenerar da la misma imagen byte a byte.
"""
import argparse

import numpy as np
from PIL import Image


def componer(arriba: Image.Image, abajo: Image.Image, banda: float, pixel: int) -> Image.Image:
    if arriba.size != abajo.size:
        raise ValueError(f"tamaños distintos: {arriba.size} y {abajo.size}")
    ancho, alto = arriba.size
    filas, columnas = -(-alto // pixel), -(-ancho // pixel)
    y, x = np.mgrid[0:filas, 0:columnas].astype(float)
    y, x = y * pixel / alto, x * pixel / ancho
    # Recta de (0, 0.62) a (1, 0.38): sube de izquierda a derecha por el centro.
    distancia = y - (0.62 - 0.24 * x)
    probabilidad = np.clip(distancia / banda + 0.5, 0, 1)
    azar = np.random.default_rng(20261025).random((filas, columnas))
    mascara = (azar < probabilidad).repeat(pixel, 0).repeat(pixel, 1)[:alto, :ancho]
    a, b = np.asarray(arriba.convert("RGBA")), np.asarray(abajo.convert("RGBA"))
    return Image.fromarray(np.where(mascara[..., None], b, a))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("arriba")
    p.add_argument("abajo")
    p.add_argument("salida")
    p.add_argument("--banda", type=float, default=0.035, help="ancho de la franja, en fracción del alto")
    p.add_argument("--pixel", type=int, default=3, help="lado de cada píxel del difuminado")
    a = p.parse_args()
    componer(Image.open(a.arriba), Image.open(a.abajo), a.banda, a.pixel).save(a.salida, optimize=True)


if __name__ == "__main__":
    main()
