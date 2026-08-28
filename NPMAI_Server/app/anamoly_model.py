"""
Unsupervised anomaly scoring - the ONLY option available today, since no
labeled_incidents rows exist yet for this pilot (see migrations/
005_layer4_features_and_risk.sql's labeled_incidents table). One
IsolationForest PER JOINT, not one fleet-wide model: a 20A lighting
circuit and a 200A main breaker have completely different normal
EWMA(R)/CUSUM/slope ranges, so a shared model would need every feature
re-normalized per breaker class anyway - training separately per joint
sidesteps that entirely at this pilot's scale (dozens of breakers, not
thousands), at the cost of needing anomaly_model_min_training_rows of
history PER JOINT before that joint gets a model at all.

---- The future supervised path (not implemented here, deliberately) ----

Once labeled_incidents accumulates real "found and fixed a loose joint
here on this date" entries, the natural evolution is: for each labeled
incident, pull that joint's joint_features rows from
[incident_date - N days, incident_date] and label them "pre-failure",
pull rows from healthy, incident-free stretches and label them "normal",
then train a supervised model (e.g. LightGBM classifier for
"pre-failure vs normal", or better, a survival/regression model
predicting days-to-incident directly) instead of this unsupervised
scorer. That retraining logic doesn't exist yet because there isn't
enough labeled data yet to make it meaningful - this module is written
so swapping to it later means adding a new train_supervised() function
here and pointing feature_processor.py at it, not restructuring
anything upstream (joint_risk_score.anomaly_score is already a generic
"how anomalous is this row" column that works the same way regardless
of which model produced it).
"""
import logging
import os
from typing import Optional

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from app.config import settings

logger = logging.getLogger(__name__)


def _model_path(panel_id: str, breaker_id: str) -> str:
    safe_name = f"{panel_id}__{breaker_id}.joblib"
    return os.path.join(settings.anomaly_model_dir, safe_name)


def load_model(panel_id: str, breaker_id: str) -> Optional[IsolationForest]:
    path = _model_path(panel_id, breaker_id)
    if not os.path.exists(path):
        return None
    try:
        return joblib.load(path)
    except Exception:
        logger.exception("Failed to load anomaly model at %s - treating as no model available", path)
        return None


def train_and_save_model(panel_id: str, breaker_id: str, feature_rows: list[dict]) -> Optional[IsolationForest]:
    """
    feature_rows: list of dicts, each with keys ewma_r_effective,
    ewma_residual, cusum_value, hour_of_day, i_rms - i.e. exactly what's
    stored in joint_features, pulled by the caller
    (app/feature_processor.py's retrain path) from that table.

    Returns None (and logs, doesn't raise) if there isn't enough data or
    the fit itself fails - callers treat "no model" as "anomaly_score
    stays NULL for this joint", never as a pipeline failure.
    """
    if len(feature_rows) < settings.anomaly_model_min_training_rows:
        logger.debug(
            "panel=%s breaker=%s has only %d feature rows (need %d) - skipping anomaly model training",
            panel_id, breaker_id, len(feature_rows), settings.anomaly_model_min_training_rows,
        )
        return None

    try:
        X = _rows_to_matrix(feature_rows)
        model = IsolationForest(
            n_estimators=100,
            contamination="auto",  # let IsolationForest set its own threshold rather than us guessing an expected fault rate we don't know yet
            random_state=42,       # reproducibility across retrains matters for debugging "why did this score change"
        )
        model.fit(X)

        os.makedirs(settings.anomaly_model_dir, exist_ok=True)
        joblib.dump(model, _model_path(panel_id, breaker_id))
        logger.info("Trained anomaly model for panel=%s breaker=%s on %d rows", panel_id, breaker_id, len(feature_rows))
        return model
    except Exception:
        logger.exception("Anomaly model training failed for panel=%s breaker=%s - anomaly_score will stay NULL", panel_id, breaker_id)
        return None


def score(model: IsolationForest, feature_row: dict) -> Optional[float]:
    """
    Returns IsolationForest's raw decision_function score for one row -
    more NEGATIVE means more anomalous, per scikit-learn's convention
    (this is the opposite sign intuition of "higher score = worse", so
    Layer 5/dashboard code consuming this column must not assume higher
    is worse). Returns None on any scoring failure - never raises.
    """
    try:
        X = _rows_to_matrix([feature_row])
        return float(model.decision_function(X)[0])
    except Exception:
        logger.exception("Anomaly scoring failed for a single row - returning None")
        return None


def _rows_to_matrix(rows: list[dict]) -> np.ndarray:
    """
    Fixed feature order - MUST match between training and scoring calls,
    which is why this is the one shared function both paths go through
    rather than each building its own array. Missing values (None) are
    imputed as 0.0 - acceptable here because ewma_r_effective/
    ewma_residual are only ever None before a joint's baseline
    completes, and rows from that period are excluded by the caller
    (app/feature_processor.py only records joint_features once residual
    is available) before they ever reach this function.
    """
    return np.array([
        [
            r.get("ewma_r_effective") or 0.0,
            r.get("ewma_residual") or 0.0,
            r.get("cusum_value") or 0.0,
            r.get("hour_of_day") or 0,
            r.get("i_rms") or 0.0,
        ]
        for r in rows
    ])
