"""Punto de entrada para `python -m receipt_reader` y para el ejecutable."""

import sys

from receipt_reader.app import main

if __name__ == "__main__":
    sys.exit(main())
