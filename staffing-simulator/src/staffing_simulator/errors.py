"""Errores de la aplicación con un mensaje apto para mostrar al usuario."""


class AppError(Exception):
    """Error esperado: su mensaje se muestra tal cual en la interfaz."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class ValidationError(AppError):
    """Datos de entrada que no cumplen una regla de negocio."""


class MissingParameterError(ValidationError):
    """Falta un parámetro de costeo (tarifa, tasa o aporte) para el año pedido."""


class NotFoundError(AppError):
    """El registro pedido no existe (por ejemplo, fue eliminado en otra ventana)."""


class DataError(AppError):
    """Problema al leer o escribir la base de datos o un archivo."""


class OperationCancelledError(AppError):
    """El usuario canceló una tarea larga antes de que terminara."""

    def __init__(self, user_message: str = "La operación fue cancelada. No se guardaron cambios.") -> None:
        super().__init__(user_message)
