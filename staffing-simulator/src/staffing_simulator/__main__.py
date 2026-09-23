"""Punto de entrada para `python -m staffing_simulator` y para el ejecutable."""

import sys

from staffing_simulator.app import main

if __name__ == "__main__":
    sys.exit(main())
