"""Convierte XLSX a PNG reversible y recupera el archivo original.

Instalar: python -m pip install Pillow
Codificar: python excel_imagen.py convertir libro.xlsx imagen.png
Recuperar: python excel_imagen.py recuperar imagen.png recuperado.xlsx

El PNG contiene los bytes del archivo, no una captura de sus celdas.
Conservar el PNG original: recortar, redimensionar o convertir a JPG
destruye los datos. Esto no cifra ni protege la informacion.
"""

import argparse
import hashlib
import math
from pathlib import Path
import struct
import sys


MAGIC = b"XLSXPNG1"
HEADER = struct.Struct(">8sQ32s")


def convertir(origen, destino, image_module):
    if origen.suffix.lower() != ".xlsx":
        raise ValueError("La entrada debe ser un archivo .xlsx.")
    datos = origen.read_bytes()
    paquete = HEADER.pack(MAGIC, len(datos), hashlib.sha256(datos).digest()) + datos
    pixeles = (len(paquete) + 2) // 3
    ancho = min(1024, max(1, math.isqrt(pixeles)))
    alto = (pixeles + ancho - 1) // ancho
    paquete += b"\x00" * (ancho * alto * 3 - len(paquete))
    imagen = image_module.frombytes("RGB", (ancho, alto), paquete)
    with destino.open("xb") as salida:
        imagen.save(salida, format="PNG")


def recuperar(origen, destino, image_module):
    if destino.suffix.lower() != ".xlsx":
        raise ValueError("La salida recuperada debe terminar en .xlsx.")
    with image_module.open(origen) as imagen:
        if imagen.format != "PNG" or imagen.mode != "RGB":
            raise ValueError("Se requiere el PNG RGB original generado por este script.")
        paquete = imagen.tobytes()
    if len(paquete) < HEADER.size:
        raise ValueError("La imagen no contiene un archivo recuperable.")
    marca, longitud, huella = HEADER.unpack_from(paquete)
    if marca != MAGIC or longitud > len(paquete) - HEADER.size:
        raise ValueError("Imagen incompatible o datos incompletos.")
    datos = paquete[HEADER.size:HEADER.size + longitud]
    if hashlib.sha256(datos).digest() != huella:
        raise ValueError("La imagen fue modificada: los datos no coinciden con el original.")
    with destino.open("xb") as salida:
        salida.write(datos)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("accion", choices=["convertir", "recuperar"])
    parser.add_argument("origen", type=Path)
    parser.add_argument("destino", type=Path)
    args = parser.parse_args()
    try:
        from PIL import Image
    except ImportError:
        parser.exit(1, "Instala Pillow: python -m pip install Pillow\n")
    try:
        if args.destino.exists():
            raise ValueError("El destino ya existe. Elige otro nombre para no sobrescribirlo.")
        if args.accion == "convertir":
            if args.destino.suffix.lower() != ".png":
                raise ValueError("La imagen de salida debe terminar en .png.")
            convertir(args.origen, args.destino, Image)
        else:
            recuperar(args.origen, args.destino, Image)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Archivo creado: {args.destino.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
