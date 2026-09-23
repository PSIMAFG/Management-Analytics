"""Capa de servicios: casos de uso que la interfaz utiliza (nunca accede a la capa data)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from staffing_simulator.services.common import Clock
from staffing_simulator.services.cost_service import CostService, ScenarioKpis
from staffing_simulator.services.excel_service import ExcelService
from staffing_simulator.services.execution_service import ExecutionService
from staffing_simulator.services.financial_service import FinancialService
from staffing_simulator.services.parameter_service import ParameterService, RateRow
from staffing_simulator.services.scenario_service import PositionResult, ScenarioService, ScenarioUpdateResult

__all__ = [
    "CostService",
    "ExcelService",
    "ExecutionService",
    "FinancialService",
    "ParameterService",
    "PositionResult",
    "RateRow",
    "ScenarioKpis",
    "ScenarioService",
    "ScenarioUpdateResult",
    "Services",
]


@dataclass(frozen=True)
class Services:
    """Punto de acceso único a los servicios para la interfaz."""

    scenarios: ScenarioService
    costs: CostService
    parameters: ParameterService
    financial: FinancialService
    execution: ExecutionService
    excel: ExcelService

    @classmethod
    def create(cls, db_path: Path, clock: Clock | None = None) -> Services:
        return cls(
            scenarios=ScenarioService(db_path, clock),
            costs=CostService(db_path, clock),
            parameters=ParameterService(db_path, clock),
            financial=FinancialService(db_path, clock),
            execution=ExecutionService(db_path, clock),
            excel=ExcelService(db_path, clock),
        )
