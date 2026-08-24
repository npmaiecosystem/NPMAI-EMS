from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    # ---- Database ----
    database_url: str = "postgresql://npmai:npmai_dev_password@localhost:5432/npmai"
    database_pool_min_size: int = 4
    database_pool_max_size: int = 16

    # ---- Redis (event handoff to Layer 3) ----
    redis_url: str = "redis://localhost:6379/0"
    redis_stream_name: str = "telemetry:new"
    redis_stream_maxlen: int = 100_000
    redis_consumer_group: str = "physics-service"

    # ---- MQTT ----
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    # Credential for the ingestion service's OWN subscriber connection -
    # deliberately separate from any individual device's credential. See
    # scripts/create_ingestion_mqtt_user.py.
    mqtt_ingestion_username: str = "ingestion-service"
    mqtt_ingestion_password: str = "changeme"
    mqtt_topic_filter: str = "building/+/panel/+/breaker/+/telemetry"

    # ---- Batch writer (see app/batch_writer.py for the full rationale) ----
    batch_max_size: int = 500
    batch_flush_interval_seconds: float = 0.5
    ingest_queue_max_size: int = 20_000

    # ---- Device auth cache ----
    device_cache_refresh_seconds: float = 30.0

    # ---- Layer 3: physics processing service ----
    # How many valid R_effective readings a brand-new breaker needs before
    # we trust an average of them as its own healthy R_nominal. See
    # app/baseline.py for the full reasoning.
    baseline_min_readings: int = 100

    # Below this current, T_hotspot-vs-T_ambient is dominated by sensor
    # noise / whatever the joint's resting temperature happens to be, not
    # I^2*R heating - R_effective back-calc is meaningless (denominator
    # blows up as I_rms -> 0), so we skip physics entirely and just record
    # status='low_current_skip'.
    min_current_for_physics_amps: float = 1.0

    # Nominal LV panel line-to-line voltage for the PyPSA cross-check
    # network (app/physics/pypsa_crosscheck.py). 0.4 kV = 400V three-phase,
    # typical commercial building LV distribution. Adjust per site if your
    # panels run a different voltage class.
    panel_v_nom_kv: float = 0.4

    # Default joint/terminal geometry used by the CoolProp/Churchill-Chu
    # theta_thermal correlation (app/physics/thermal_model.py) when a
    # specific breaker has no calibrated override in
    # app/breaker_thermal_config.py. Same "generic placeholder, review per
    # site" spirit as config.h's example CT/thermal-zone values on the
    # firmware side.
    default_joint_area_m2: float = 0.0015
    default_joint_char_length_m: float = 0.02
    default_joint_emissivity: float = 0.9

    # Consumer identity prefix for the Redis consumer group (see
    # app/physics_service.py) - each running process gets
    # f"{prefix}-{pid}", so multiple physics workers can share the same
    # group and split the stream without stepping on each other.
    physics_consumer_name_prefix: str = "physics-worker"
    
    # ---- Layer 4: feature engineering + risk scoring ----
    # How often feature_service.py's poller checks for new derived_metrics
    # rows per joint. This is a POLLING design, not event-driven off a
    # Redis stream Layer 3 doesn't currently publish (see
    # app/feature_service.py's module docstring for the full trade-off
    # and the one-line change needed if you later want it event-driven).
    feature_poll_interval_seconds: float = 60.0

    # EWMA smoothing window, in NUMBER OF READINGS (not time) - with this
    # pilot's ~60s send interval, span=48 is roughly a 48-minute window
    # per reading's typical cadence per breaker. Tune per your actual
    # send interval if it differs meaningfully from 60s.
    ewma_span_readings: int = 48

    # CUSUM slack parameter, in standard deviations of this joint's own
    # residual-noise baseline. 0.5 is the conventional default for
    # detecting a sustained ~1-sigma shift without alarming on pure
    # noise - see app/feature_math.py's cusum_step docstring.
    cusum_k_slack: float = 0.5

    # How many valid residual_c readings (status='ok' from Layer 3) a
    # joint needs before its OWN residual-noise baseline (mean/std) is
    # trusted enough to run CUSUM against - separate from Layer 3's own
    # baseline_min_readings, which governs R_nominal instead. See
    # app/feature_baseline.py.
    feature_baseline_min_readings: int = 168  # ~7 days at 1 reading/hour-equivalent cadence

    # Rolling window (days) used for the dR/dt slope regression in
    # app/feature_processor.py.
    slope_window_days: int = 7

    # Minimum number of joint_features rows needed within
    # slope_window_days before a slope is computed at all - fewer than
    # this and the regression is too noisy/short to trust, so
    # joint_risk_score gets status='insufficient_history_for_slope'
    # instead of a spurious slope.
    slope_min_readings: int = 24

    # Default 'critical' temperature rise used to derive each joint's
    # own critical_r_ohms (app/feature_math.py's critical_r_for_breaker)
    # when a specific breaker has no override in
    # app/breaker_risk_config.py. 15C = the boundary between
    # "intermediate" and "serious" on the Delta-T severity scale - i.e.
    # critical_r_ohms is defined as "the R that would put this joint at
    # the edge of 'serious' if it were running at its full rated
    # current", a deliberately conservative (early) trigger point.
    default_critical_delta_t_c: float = 15.0

    # Default rated current (amps) used for critical_r_ohms when a
    # specific breaker has no override - same "generic placeholder,
    # review per site" spirit as config.h's example CT ratios and
    # app/breaker_thermal_config.py's default joint geometry. MUST be
    # overridden per real breaker in app/breaker_risk_config.py before
    # relying on projected_days_to_critical for anything operational -
    # an unreviewed default rated current makes that number meaningless
    # for a specific real breaker.
    default_rated_current_amps: float = 20.0

    # Where anomaly_model.py persists trained per-joint IsolationForest
    # models (joblib files, one per panel_id/breaker_id).
    anomaly_model_dir: str = "./models/anomaly"

    # Minimum joint_features rows required before a joint's first
    # anomaly model is trained at all - below this, joint_risk_score's
    # anomaly_score stays NULL rather than training on too little data
    # to mean anything.
    anomaly_model_min_training_rows: int = 336  # ~2 weeks at hourly-equivalent cadence

    # How often (hours) anomaly_model.py's retrain job re-fits each
    # joint's model against its latest accumulated history.
    anomaly_retrain_interval_hours: float = 168.0  # weekly

    """
Append to the SAME Settings class in app/config.py - see Layer 3/4's
own config_additions.py files for why there must only ever be one.
"""

    # ---- Layer 5: provider selection ----
    # Which provider tries FIRST for every call. Change this (env var
    # LAYER5_PRIMARY_PROVIDER) to switch your default model with zero
    # code change - this is the concrete answer to "give option to
    # change" for provider choice, same philosophy as audit_times being
    # data (building_audit_config) rather than a hardcoded schedule.
    layer5_primary_provider: str = "anthropic"

    # Tried in order after the primary fails. Empty list = no fallback,
    # a primary failure means the whole call fails.
    layer5_fallback_providers: list[str] = ["openai", "gemini", "grok"]

    layer5_max_output_tokens: int = 800

    # Per-building daily spend ceiling in USD, checked BEFORE every call
    # (app/llm/usage_tracker.py's check_spend_cap). None = no cap
    # (unlimited spend) - an explicit choice, not a silent default.
    layer5_daily_spend_cap_usd: float | None = 5.00

    # ---- Provider credentials and model selection ----
    # Every one of these can be swapped independently - e.g. set
    # anthropic_model to a different Claude model, or leave any
    # unused provider's API key blank (app/llm/registry.py's
    # build_provider() logs and skips a provider that fails to
    # construct, e.g. from a missing key, rather than crashing).
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-pro"

    grok_api_key: str = ""
    grok_model: str = "grok-4"


settings = Settings()
