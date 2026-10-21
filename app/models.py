"""Persistent data model (spec Section 6).

Rolling notes are single overwritten text fields; numeric history (performances,
cardio records, body metrics) is append-only and never pruned (spec Section 2).
"""

from datetime import datetime

from datetime import timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    """Stores UTC, always returns aware datetimes - SQLite would otherwise drop the offset."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Base(DeclarativeBase):
    pass


class Profile(Base):
    """Global layer, a single row (id=1)."""

    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goals: Mapped[list] = mapped_column(JSON, default=list)
    training_structure: Mapped[str] = mapped_column(Text, default="")
    general_state: Mapped[str] = mapped_column(Text, default="")
    cardio_note: Mapped[str] = mapped_column(Text, default="")
    # Behavioral configuration editable via the show/edit mechanism (spec 3.4).
    tone: Mapped[str] = mapped_column(Text, default="")
    assertiveness: Mapped[str] = mapped_column(Text, default="")
    footnote_style: Mapped[str] = mapped_column(Text, default="")
    # Cardio-modality state (spec 6.4).
    default_cardio_modality: Mapped[str] = mapped_column(String(32), default="treadmill")
    treadmill_baseline: Mapped[list] = mapped_column(JSON, default=list)  # [{label, incline_pct, speed_kmh}]
    treadmill_note: Mapped[str] = mapped_column(Text, default="")


class Injury(Base):
    __tablename__ = "injuries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    body_area: Mapped[str] = mapped_column(String(128), default="")
    muscle_groups: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | resolved
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_mentioned_at: Mapped[datetime] = mapped_column(UTCDateTime())
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Temporary substitutions tied to this injury, to propose reverting on resolution (spec 5.1).
    temporary_substitutions: Mapped[list] = mapped_column(JSON, default=list)  # [str]
    reverts_proposed: Mapped[bool] = mapped_column(Boolean, default=False)


class BodyMetric(Base):
    __tablename__ = "body_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    measured_at: Mapped[datetime] = mapped_column(UTCDateTime())
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    muscle_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class Split(Base):
    __tablename__ = "splits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    specialization: Mapped[str] = mapped_column(Text, default="")
    claimed_groups: Mapped[list] = mapped_column(JSON, default=list)
    rolling_note: Mapped[str] = mapped_column(Text, default="")
    structure_override: Mapped[str] = mapped_column(Text, default="")


class Exercise(Base):
    __tablename__ = "exercises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    muscle_groups: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    rolling_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class TrainingSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_activity_at: Mapped[datetime] = mapped_column(UTCDateTime())
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | closed
    primary_split_id: Mapped[int | None] = mapped_column(ForeignKey("splits.id"), nullable=True)
    # Working memory: raw conversation for this visit only; cleared on close (spec 2, 5).
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    # Changes already written live during the session, so synthesis doesn't redo them.
    applied_changes: Mapped[list] = mapped_column(JSON, default=list)
    strength_done: Mapped[bool] = mapped_column(Boolean, default=False)
    # UI state for button-driven logging: {"plan_item_id": int} while awaiting numbers.
    pending_entry: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)

    primary_split: Mapped[Split | None] = relationship()
    plans: Mapped[list["Plan"]] = relationship(back_populates="session", order_by="Plan.id")


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    # accepted -> fulfilled | abandoned ("proposed" lives only in the conversation)
    status: Mapped[str] = mapped_column(String(16), default="accepted")
    finalized_at: Mapped[datetime] = mapped_column(UTCDateTime())
    cardio_plan: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")

    session: Mapped[TrainingSession] = relationship(back_populates="plans")
    items: Mapped[list["PlanItem"]] = relationship(back_populates="plan", order_by="PlanItem.position")


class PlanItem(Base):
    __tablename__ = "plan_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"))
    position: Mapped[int] = mapped_column(Integer)
    target_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_reps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_sets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    superset_group: Mapped[str] = mapped_column(String(8), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="planned")  # planned | done | not_done
    substitutes_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    plan: Mapped[Plan] = relationship(back_populates="items")
    exercise: Mapped[Exercise] = relationship()


class Performance(Base):
    """One row per exercise performed in a session (spec 6.3 performance history)."""

    __tablename__ = "performances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"))
    plan_item_id: Mapped[int | None] = mapped_column(ForeignKey("plan_items.id"), nullable=True)
    performed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    reps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")

    exercise: Mapped[Exercise] = relationship()


class CardioRecord(Base):
    __tablename__ = "cardio_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    performed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    modality: Mapped[str] = mapped_column(String(32), default="treadmill")  # treadmill | elliptical | ...
    duration_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    zone_seconds: Mapped[list] = mapped_column(JSON, default=list)  # [z1..z5] seconds
    calories: Mapped[int | None] = mapped_column(Integer, nullable=True)
    carbs_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    treadmill_intervals: Mapped[list] = mapped_column(JSON, default=list)  # [{label, incline_pct, speed_kmh}]
    elliptical_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")


class ProcessedUpdate(Base):
    """Telegram update_id dedupe (spec 10.1 idempotency)."""

    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime())


class KV(Base):
    """Small app state: out-of-session chat buffer, job bookkeeping."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)


class LlmUsage(Base):
    """One row per model call: tokens, estimated cost, latency (for cost review; not read by the bot)."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime())
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(64))
    input: Mapped[int] = mapped_column(Integer)
    cached: Mapped[int] = mapped_column(Integer)
    output: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    seconds: Mapped[float] = mapped_column(Float)


class ArchivedTranscript(Base):
    """Copy of a closed session's conversation for reviewing the bot's replies (not read by the bot)."""

    __tablename__ = "archived_transcripts"

    session_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    archived_at: Mapped[datetime] = mapped_column(UTCDateTime())
    model: Mapped[str] = mapped_column(String(64))
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
