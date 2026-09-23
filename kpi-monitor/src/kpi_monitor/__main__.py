"""Punto de entrada para `python -m kpi_monitor` y para el ejecutable."""

import sys

from kpi_monitor.app import main

if __name__ == "__main__":
    sys.exit(main())
