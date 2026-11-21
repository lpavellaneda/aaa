"""Transferencia de texto UTF-8, sin normalizar saltos, BOM ni tabuladores."""
import argparse
from pathlib import Path


def encode_bytes(source):
    # Decode only to reject binary/non-UTF-8 inputs; preserve every original byte.
    source.decode('utf-8')
    result = bytearray()
    # Split only LF: bytes.splitlines would also split other source characters.
    lines = source.split(b'\n')
    for i, line in enumerate(lines):
        if i == len(lines) - 1 and not line:
            break
        count = len(line) - len(line.lstrip(b' '))
        result.extend(str(count).encode('ascii') + b'|' + line[count:])
        if i < len(lines) - 1:
            result.extend(b'\n')
    return bytes(result)


def encode_file(source, target):
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError('Origen y destino deben ser distintos')
    target.write_bytes(encode_bytes(source.read_bytes()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('origen', nargs='?', default='cliente.py')
    parser.add_argument('destino', nargs='?', default='codigo.txt')
    args = parser.parse_args()
    encode_file(args.origen, args.destino)
    print('Generado:', args.destino)


if __name__ == '__main__':
    main()
