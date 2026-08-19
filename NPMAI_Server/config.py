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


settings = Settings()
