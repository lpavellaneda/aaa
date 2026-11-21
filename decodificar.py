"""Reconstruye bytes UTF-8 del formato espacios|contenido."""
import argparse
from pathlib import Path


def decode_bytes(encoded):
    result = bytearray()
    lines = encoded.split(b'\n')
    for i, line in enumerate(lines):
        if i == len(lines) - 1 and not line:
            break
        prefix, sep, content = line.partition(b'|')
        if not sep or not prefix or not prefix.isdigit() or len(prefix) > 7:
            raise ValueError(f'Linea {i + 1}: formato invalido')
        count = int(prefix)
        if count > 1000000:
            raise ValueError(f'Linea {i + 1}: indentacion excesiva')
        result.extend(b' ' * count + content)
        if i < len(lines) - 1:
            result.extend(b'\n')
    decoded = bytes(result)
    decoded.decode('utf-8')
    return decoded


def decode_file(source, target, overwrite=False):
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError('Origen y destino deben ser distintos')
    data = decode_bytes(source.read_bytes())
    with target.open('wb' if overwrite else 'xb') as stream:
        stream.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('origen', nargs='?', default='codigo.txt')
    parser.add_argument('destino', nargs='?', default='cliente.py')
    parser.add_argument('--sobrescribir', action='store_true')
    args = parser.parse_args()
    decode_file(args.origen, args.destino, args.sobrescribir)
    print('Reconstruido:', args.destino)


if __name__ == '__main__':
    main()
