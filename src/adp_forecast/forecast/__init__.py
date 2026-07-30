"""Forecast layer: baselines, the ridge model, and the registry that selects them.

Everything is reached through :class:`~adp_forecast.forecast.port.ForecastPort`, so the
evaluation layer scores a naive rule and the fitted model through one interface.
"""

from typing import Callable, Final, Mapping

from .baselines import (
    DEFAULT_INTERVAL_LEVEL,
    DriftForecaster,
    MovingAverageForecaster,
    RandomWalkForecaster,
    usable_changes,
)
from .design import (
    DEFAULT_TERMS,
    DesignMatrix,
    FeatureTerm,
    Transform,
    build_design_matrix,
)
from .port import Driver, Forecast, ForecastPort
from .ridge import DEFAULT_ALPHAS, RidgeFit, RidgeForecaster

#: Name to factory. Selecting a model is a lookup here, so the CLI, the backtest and any
#: future HTTP layer all resolve models the same way and none of them holds a list.
MODEL_REGISTRY: Final[Mapping[str, Callable[[], ForecastPort]]] = {
    "ridge": RidgeForecaster,
    "random_walk": RandomWalkForecaster,
    "mean_3m": lambda: MovingAverageForecaster(3),
    "mean_6m": lambda: MovingAverageForecaster(6),
    "drift": DriftForecaster,
}

#: The model used when none is named.
DEFAULT_MODEL: Final[str] = "ridge"

#: Naive rules the real model is measured against. Ordered weakest-claim-first; the
#: random walk is the honest bar for an already-seasonally-adjusted series.
BASELINE_MODELS: Final[tuple[str, ...]] = ("random_walk", "mean_3m", "mean_6m", "drift")


def get_model(name: str = DEFAULT_MODEL) -> ForecastPort:
    """Construct a model by registered name.

    Args:
        name: Key from :data:`MODEL_REGISTRY`.

    Returns:
        A fresh, unfitted model.

    Raises:
        KeyError: If ``name`` is not registered, listing what is.
    """
    try:
        factory = MODEL_REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown model '{name}'. Registered: {', '.join(sorted(MODEL_REGISTRY))}"
        ) from None
    return factory()


__all__ = [
    "BASELINE_MODELS",
    "DEFAULT_ALPHAS",
    "DEFAULT_INTERVAL_LEVEL",
    "DEFAULT_MODEL",
    "DEFAULT_TERMS",
    "MODEL_REGISTRY",
    "DesignMatrix",
    "DriftForecaster",
    "Driver",
    "FeatureTerm",
    "Forecast",
    "ForecastPort",
    "MovingAverageForecaster",
    "RandomWalkForecaster",
    "RidgeFit",
    "RidgeForecaster",
    "Transform",
    "build_design_matrix",
    "get_model",
    "usable_changes",
]
