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
import tempfile
import json


MAGIC = b"XLSXPNG1"
HEADER = struct.Struct(">8sQ32s")


def dividir(origen, destino, image_module):
    if origen.suffix.lower() != '.xlsx':
        raise ValueError('La entrada debe ser .xlsx.')
    datos = origen.read_bytes()
    tam = 20_000_000
    total = max(1, (len(datos) + tam - 1) // tam)
    huella = hashlib.sha256(datos).hexdigest()
    # Prepare all parts before publishing the new output directory.
    with tempfile.TemporaryDirectory(dir=destino.parent) as temporal:
        carpeta = Path(temporal)
        for i in range(total):
            meta = json.dumps({'parte': i, 'total': total, 'sha256': huella}).encode()
            fragmento = carpeta / 'fragmento.xlsx'
            fragmento.write_bytes(b'PARTPNG1' + struct.pack('>I', len(meta)) + meta + datos[i*tam:(i+1)*tam])
            png = carpeta / f'parte_{i+1:04d}.png'
            convertir(fragmento, png, image_module)
            if png.stat().st_size >= 24_000_000:
                raise ValueError('Una parte supera el limite de 24 MB.')
        fragmento.unlink()
        carpeta.rename(destino)


def unir(origen, destino, image_module):
    partes = {}
    esperado = None
    with tempfile.TemporaryDirectory() as temporal:
        for png in sorted(origen.glob('parte_*.png')):
            fragmento = Path(temporal) / 'fragmento.xlsx'
            recuperar(png, fragmento, image_module)
            paquete = fragmento.read_bytes()
            fragmento.unlink()
            if paquete[:8] != b'PARTPNG1':
                raise ValueError('Parte incompatible.')
            longitud = struct.unpack('>I', paquete[8:12])[0]
            meta = json.loads(paquete[12:12+longitud])
            identidad = (meta['total'], meta['sha256'])
            if esperado is not None and esperado != identidad:
                raise ValueError('Hay partes de diferentes archivos.')
            esperado = identidad
            indice = meta['parte']
            if indice in partes:
                raise ValueError('Parte duplicada.')
            partes[indice] = paquete[12+longitud:]
    if esperado is None or set(partes) != set(range(esperado[0])):
        raise ValueError('Faltan partes para recuperar el Excel.')
    datos = b''.join(partes[i] for i in range(esperado[0]))
    if hashlib.sha256(datos).hexdigest() != esperado[1]:
        raise ValueError('Los datos recuperados no coinciden con el original.')
    if destino.suffix.lower() != '.xlsx':
        raise ValueError('La salida debe ser .xlsx.')
    with destino.open('xb') as salida:
        salida.write(datos)


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
    parser.add_argument("accion", choices=["convertir", "recuperar", "dividir", "unir"])
    parser.add_argument("origen", type=Path)
    parser.add_argument("destino", type=Path, nargs='?', help='Opcional: se guarda junto al origen por defecto')
    args = parser.parse_args()
    args.origen = args.origen.resolve()
    if args.destino is None:
        if args.accion == 'dividir':
            args.destino = args.origen.with_name(args.origen.stem + '_png')
        elif args.accion == 'convertir':
            args.destino = args.origen.with_suffix('.png')
        else:
            args.destino = args.origen.with_name(args.origen.stem + '_recuperado.xlsx')
    try:
        from PIL import Image
    except ImportError:
        parser.exit(1, "Instala Pillow: python -m pip install Pillow\n")
    try:
        if args.destino.exists():
            raise ValueError("El destino ya existe. Elige otro nombre para no sobrescribirlo.")
        if args.accion == 'dividir':
            dividir(args.origen, args.destino, Image)
        elif args.accion == 'unir':
            unir(args.origen, args.destino, Image)
        elif args.accion == "convertir":
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
