"""Errores de la aplicación con un mensaje apto para mostrar al usuario."""

from __future__ import annotations

from collections.abc import Mapping

# Mensaje para los errores no previstos: el detalle técnico queda en el log.
UNEXPECTED_ERROR = "Ocurrió un error inesperado. El detalle quedó registrado en el log de la aplicación."


class AppError(Exception):
    """Error esperado: su mensaje se muestra tal cual en la interfaz."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class ValidationError(AppError):
    """Datos de entrada que no cumplen una regla de negocio."""


class FieldFormatError(ValidationError):
    """Un valor escrito o leído no tiene un formato aceptable (RUT, monto, fecha...)."""


class FormError(ValidationError):
    """Uno o más campos de un formulario no son válidos.

    `field_errors` asocia el nombre interno de cada campo con su mensaje.
    """

    def __init__(self, field_errors: Mapping[str, str]) -> None:
        self.field_errors = dict(field_errors)
        detail = "; ".join(self.field_errors.values())
        super().__init__(f"Revise los datos ingresados: {detail}")


class DataError(AppError):
    """Problema al leer o escribir la base de datos o un archivo."""


class OcrUnavailableError(AppError):
    """El motor de OCR no se pudo cargar."""
