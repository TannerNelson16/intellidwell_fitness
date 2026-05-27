import atexit
import copy
import json
import os
import secrets
from datetime import date, datetime, time, timedelta
import logging
from pathlib import Path
from typing import List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_from_directory,
)
from pywebpush import WebPushException, webpush
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Float,
    String,
    Time,
    UniqueConstraint,
    create_engine,
    func,
    inspect,
    or_,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, selectinload
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
app.config["VAPID_PUBLIC_KEY"] = os.environ.get("VAPID_PUBLIC_KEY", "")
app.config["VAPID_PRIVATE_KEY"] = os.environ.get("VAPID_PRIVATE_KEY", "")
app.config["VAPID_EMAIL"] = os.environ.get("VAPID_EMAIL", "admin@example.com")
app.config["HEALTHKIT_TOKEN"] = os.environ.get("HEALTHKIT_TOKEN", "")
app.permanent_session_lifetime = timedelta(days=60)

# Ensure we log healthkit payloads to stdout/stderr (captured by gunicorn/systemd)
logging.basicConfig(level=logging.INFO)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", f"sqlite:///{(BASE_DIR / 'intellidwell_fitness.db').as_posix()}"
)
engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)
Base = declarative_base()
scheduler = BackgroundScheduler(daemon=True)

# Add meal-specific calorie columns if they don't exist (SQLite only)
def ensure_calorie_meal_columns():
    with engine.begin() as conn:
        existing_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(daily_entries)").fetchall()}
        new_cols = []
        for col in ["cal_breakfast", "cal_lunch", "cal_dinner", "cal_snack"]:
            if col not in existing_cols:
                new_cols.append(col)
        for col in new_cols:
            conn.exec_driver_sql(f"ALTER TABLE daily_entries ADD COLUMN {col} INTEGER DEFAULT 0;")


try:
    ensure_calorie_meal_columns()
except Exception as e:
    logging.warning("Could not ensure meal calorie columns: %s", e)

FAST_START = time(hour=19)  # 7:00 PM
FAST_DURATION = timedelta(hours=16)  # ends 11 AM next day
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    notification_pref = relationship(
        "NotificationPreference",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys=lambda: [NotificationPreference.user_id],
        primaryjoin=lambda: NotificationPreference.user_id == User.id,
    )
    subscriptions = relationship(
        "NotificationSubscription",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    entries = relationship(
        "DailyEntry",
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="DailyEntry.entry_date",
    )
    profile = relationship(
        "UserProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys=lambda: [UserProfile.user_id],
    )

    def verify_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class DailyEntry(Base):
    __tablename__ = "daily_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "entry_date", name="uq_daily_entries_user_date"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    entry_date = Column(Date, nullable=False)
    calories = Column(Integer, default=0)
    protein = Column(Integer, default=0)
    water_oz = Column(Integer, default=0)
    weight = Column(Float, nullable=True)
    body_fat = Column(Float, nullable=True)
    mindset_energy = Column(String(255), default="")
    mindset_highlight = Column(String(255), default="")
    mindset_proud = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="entries")
    exercises = relationship(
        "Exercise",
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="Exercise.timestamp",
    )
    photos = relationship(
        "ProgressPhoto",
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="desc(ProgressPhoto.created_at)",
    )


class Exercise(Base):
    __tablename__ = "exercises"

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey("daily_entries.id"), nullable=False)
    type = Column(String(32), nullable=False)
    duration = Column(String(64), default="")
    incline = Column(String(32), default="")
    vest = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

    entry = relationship("DailyEntry", back_populates="exercises")


class ProgressPhoto(Base):
    __tablename__ = "progress_photos"

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey("daily_entries.id"), nullable=False)
    file_path = Column(String(255), nullable=False)
    caption = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    entry = relationship("DailyEntry", back_populates="photos")


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    exercise_time = Column(Time, nullable=True)
    last_fast_break = Column(Date, nullable=True)
    last_fast_start = Column(Date, nullable=True)
    last_exercise = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship(
        "User",
        back_populates="notification_pref",
        foreign_keys=[user_id],
    )


class NotificationSubscription(Base):
    __tablename__ = "notification_subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    endpoint = Column(String(512), nullable=False)
    p256dh = Column(String(255), nullable=False)
    auth = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship(
        "User",
        back_populates="subscriptions",
        foreign_keys=[user_id],
    )


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    start_weight = Column(Float, nullable=True)
    goal_weight = Column(Float, nullable=True)
    maintenance_calories = Column(Integer, nullable=True)
    calorie_target = Column(Integer, nullable=True)
    protein_target = Column(Integer, nullable=True)
    water_target = Column(Integer, nullable=True)
    game_plan = Column(String(1024), default="")
    partner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship(
        "User",
        back_populates="profile",
        foreign_keys=[user_id],
    )
    partner_user = relationship(
        "User",
        foreign_keys=[partner_user_id],
        post_update=True,
    )


class ConnectionRequest(Base):
    __tablename__ = "connection_requests"
    __table_args__ = (
        UniqueConstraint("requester_id", "target_id", name="uq_connection_request_pair"),
    )

    id = Column(Integer, primary_key=True)
    requester_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(16), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)

    requester = relationship("User", foreign_keys=[requester_id], backref="sent_requests")
    target = relationship("User", foreign_keys=[target_id], backref="received_requests")


class MobileApiToken(Base):
    __tablename__ = "mobile_api_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="uq_mobile_api_tokens_token"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String(128), nullable=False)
    label = Column(String(64), default="android-app")
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])


class HealthMetricSample(Base):
    __tablename__ = "health_metric_samples"
    __table_args__ = (
        UniqueConstraint("user_id", "metric_type", "recorded_at", name="uq_health_metric_samples_key"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    metric_type = Column(String(32), nullable=False)
    value = Column(Float, nullable=False)
    unit = Column(String(32), default="")
    source = Column(String(64), default="android-health-connect")
    recorded_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


class HealthWorkoutSample(Base):
    __tablename__ = "health_workout_samples"
    __table_args__ = (
        UniqueConstraint("user_id", "workout_type", "start_time", "end_time", name="uq_health_workout_samples_key"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    workout_type = Column(String(64), nullable=False)
    title = Column(String(128), default="")
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    duration_minutes = Column(Float, nullable=True)
    source = Column(String(64), default="android-health-connect")
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


Base.metadata.create_all(engine)


def seed_default_users() -> None:
    defaults = [
        {
            "username": "Tayleur",
            "password": "Tf2241994!",
            "profile": {
                "start_weight": 188,
                "goal_weight": 165,
                "maintenance_calories": 2200,
                "calorie_target": 1650,
                "protein_target": 130,
                "water_target": 110,
            },
        },
        {
            "username": "Tanner23456",
            "password": "Tn7281994!",
            "profile": {
                "start_weight": 206,
                "goal_weight": 185,
                "maintenance_calories": 2300,
                "calorie_target": 1800,
                "protein_target": 130,
                "water_target": 110,
            },
        },
    ]

    with SessionLocal() as db:
        for info in defaults:
            user = db.query(User).filter_by(username=info["username"]).first()
            if not user:
                user = User(
                    username=info["username"],
                    password_hash=generate_password_hash(info["password"]),
                )
                db.add(user)
                db.commit()
                db.refresh(user)
            else:
                user.password_hash = generate_password_hash(info["password"])
                db.commit()
            ensure_profile_defaults(db, user, info.get("profile", {}))


def ensure_profile_defaults(db, user: User, defaults: dict) -> None:
    profile = db.query(UserProfile).filter_by(user_id=user.id).first()
    if not profile:
        profile = UserProfile(user_id=user.id, **defaults)
        db.add(profile)
        db.commit()
    elif defaults:
        updated = False
        for key, value in defaults.items():
            current = getattr(profile, key)
            if current in (None, "", 0):
                setattr(profile, key, value)
                updated = True
        if updated:
            db.commit()


seed_default_users()


def ensure_schema() -> None:
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("daily_entries")}
    if "user_id" not in columns:
        migrate_daily_entries()
        columns = {col["name"] for col in inspect(engine).get_columns("daily_entries")}
    with engine.begin() as conn:
        if "weight" not in columns:
            conn.execute(text("ALTER TABLE daily_entries ADD COLUMN weight FLOAT"))
        if "body_fat" not in columns:
            conn.execute(text("ALTER TABLE daily_entries ADD COLUMN body_fat FLOAT"))
        profile_cols = {col["name"] for col in inspector.get_columns("user_profiles")}
        if "partner_user_id" not in profile_cols:
            conn.execute(text("ALTER TABLE user_profiles ADD COLUMN partner_user_id INTEGER"))


def migrate_daily_entries() -> None:
    inspector = inspect(engine)
    if "daily_entries" not in inspector.get_table_names():
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE daily_entries RENAME TO daily_entries_legacy"))
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        owner = db.query(User).filter_by(username="Tayleur").first() or db.query(User).first()
        owner_id = owner.id if owner else 1
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO daily_entries (
                    id, user_id, entry_date, calories, protein, water_oz, weight,
                    body_fat, mindset_energy, mindset_highlight, mindset_proud,
                    created_at, updated_at
                )
                SELECT
                    id, :user_id, entry_date, calories, protein, water_oz, weight,
                    body_fat, mindset_energy, mindset_highlight, mindset_proud,
                    created_at, updated_at
                FROM daily_entries_legacy
                """
            ),
            {"user_id": owner_id},
        )
        conn.execute(text("DROP TABLE daily_entries_legacy"))


ensure_schema()


@app.context_processor
def inject_globals():
    return {
        "vapid_public_key": app.config.get("VAPID_PUBLIC_KEY", ""),
        "current_year": date.today().year,
    }


FITNESS_PLAN = {
    "target": {
        "current": None,
        "goal": None,
        "loss_needed": None,
        "timeline": "4–6 months",
        "rate": "1–1.5 lb/week",
    },
    "fasting": {
        "style": "16:8",
        "window": "Fast 7:00 PM → 11:00 AM, eat 11:00 AM → 7:00 PM",
        "why": [
            "Controls calories without obsessive tracking",
            "Improves insulin sensitivity",
            "Still family-friendly and social",
        ],
        "allowance": [
            "Water",
            "Black coffee",
            "Unsweetened tea",
            "Electrolytes without sugar",
        ],
    },
    "nutrition": {
        "calories": {
            "maintenance": 2200,
            "target": 1650,
        },
        "protein": "120–130 g/day (~30% of intake)",
        "macros": "Protein ~30%, Carbs 35–40%, Fat 30–35%",
        "mantra": "Protein first, plants second, carbs last.",
    },
    "water": {
        "target": "90–110 oz/day",
        "extra": "Add 10–20 oz on treadmill/vest days",
        "tip": "Clear-ish pee = winning; apple juice color = hydrate.",
    },
    "exercise": {
        "weekly_structure": [
            "Mon – Upper Body Push + Cardio Finisher",
            "Tue – Lower Body + Core",
            "Wed – Conditioning (Bike/Treadmill) + Light Weights",
            "Thu – Upper Body Pull + Core",
            "Fri – Full-Body Metabolic Strength",
        ],
        "schedule": [
            {
                "day": "Monday",
                "title": "Upper Push (Chest / Shoulders / Triceps)",
                "strength": [
                    "3 rounds, minimal rest",
                    "Dumbbell or Barbell Bench Press – 8–10 reps",
                    "Standing DB Shoulder Press – 8 reps",
                    "Incline Push-Ups – to near failure",
                    "DB Overhead Triceps Extension – 10–12 reps",
                ],
                "finisher": [
                    "Treadmill incline walk or jog",
                    "1 min fast / 1 min easy × 5",
                    "Old rule: if your shoulders aren’t tired, you didn’t push hard enough.",
                ],
            },
            {
                "day": "Tuesday",
                "title": "Legs + Core",
                "strength": [
                    "Superset style",
                    "Barbell Squats – 4 × 6–8",
                    "DB Romanian Deadlifts – 3 × 10",
                    "DB Walking Lunges – 2 × 20 steps",
                ],
                "core": [
                    "Plank – 3 × 30–45 sec",
                    "Hanging or Lying Leg Raises – 2 × 12",
                ],
                "finisher": [
                    "Optional: 5 min easy bike spin",
                ],
            },
            {
                "day": "Wednesday",
                "title": "Conditioning Day (Fat Loss Focus)",
                "strength": [
                    "Choose one:",
                    "Bike Intervals (5 min warm-up; 20 min of 30 sec hard / 90 sec easy; 5 min cool-down)",
                    "OR Treadmill: brisk walk at incline 6–10%, sustained effort",
                ],
                "finisher": [
                    "This is where fat loss shows—don’t sandbag it.",
                ],
            },
            {
                "day": "Thursday",
                "title": "Upper Pull + Core",
                "strength": [
                    "Barbell Bent-Over Row – 4 × 8",
                    "One-Arm DB Row – 3 × 10/side",
                    "DB Hammer Curls – 3 × 10",
                    "Rear Delt Flys – 2 × 12",
                ],
                "core": [
                    "Russian Twists (DB) – 3 × 20",
                    "Side Plank – 2 × 30 sec/side",
                ],
            },
            {
                "day": "Friday",
                "title": "Full-Body Metabolic Strength",
                "strength": [
                    "Circuit, 3–4 rounds:",
                    "DB Goblet Squat – 12",
                    "DB Push Press – 8",
                    "Barbell Deadlift – 6",
                    "Renegade Rows – 8/side",
                    "Burpees or Mountain Climbers – 30 sec",
                    "Rest 60–90 sec between rounds",
                    "This one should make you question your life choices (that’s normal).",
                ],
            },
        ],
    },
    "mindset": [
        "Consistency beats intensity.",
        "Showing up counts.",
        "You’re building something worth keeping.",
    ],
}


def build_plan_for_user(profile: UserProfile | None) -> dict:
    plan = copy.deepcopy(FITNESS_PLAN)
    if profile:
        plan["target"]["current"] = profile.start_weight
        plan["target"]["goal"] = profile.goal_weight
        if profile.start_weight is not None and profile.goal_weight is not None:
            plan["target"]["loss_needed"] = round(abs(profile.start_weight - profile.goal_weight), 1)
        else:
            plan["target"]["loss_needed"] = None
        if profile.calorie_target:
            plan["nutrition"]["calories"]["target"] = profile.calorie_target
        if profile.maintenance_calories:
            plan["nutrition"]["calories"]["maintenance"] = profile.maintenance_calories
        if profile.protein_target:
            plan["nutrition"]["protein"] = f"Protein: {profile.protein_target} g/day"
        if profile.water_target:
            plan["water"]["target"] = f"Target: {profile.water_target} oz/day"
    plan["custom_text"] = profile.game_plan.strip() if profile and profile.game_plan else ""
    return plan

MINDSET_PROMPTS = [
    "Energy check-in — how are you feeling today?",
    "One thing you did well today?",
    "One thing you’re proud of?",
    "What felt surprisingly easy today?",
    "What helped you stay on track?",
    "How did you care for yourself today?",
    "What tiny win deserves a celebration?",
]


def is_authenticated() -> bool:
    return bool(session.get("user_id"))


@app.before_request
def require_login() -> None:
    exempt_endpoints = {
        "static",
        "login",
        "signup",
        "subscribe_notifications",
        "healthkit_webhook",
        "mobile_login",
        "mobile_auth_token",
        "mobile_health_sync",
        "mobile_today_summary",
        "mobile_quick_log",
        "mobile_history",
    }
    if request.endpoint in exempt_endpoints or request.endpoint is None:
        return
    if not is_authenticated():
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if is_authenticated():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with SessionLocal() as db:
            user = db.query(User).filter(func.lower(User.username) == username.lower()).first()
            if user and user.verify_password(password):
                session.permanent = True
                session["user_id"] = user.id
                session["auth_username"] = user.username
                flash("Welcome back! Let’s build another win.")
                return redirect(url_for("dashboard"))
        error = "Invalid credentials. Check spelling and try again."
    return render_template("login.html", error=error)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if is_authenticated():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not username or not password:
            error = "Username and password are required."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            with SessionLocal() as db:
                existing = (
                    db.query(User)
                        .filter(func.lower(User.username) == username.lower())
                        .first()
                )
                if existing:
                    error = "That username is already taken."
                else:
                    user = User(username=username, password_hash=generate_password_hash(password))
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                    profile = UserProfile(
                        user_id=user.id,
                        start_weight=None,
                        goal_weight=None,
                        calorie_target=1650,
                        protein_target=120,
                        water_target=100,
                    )
                    db.add(profile)
                    db.commit()
                    session.permanent = True
                    session["user_id"] = user.id
                    session["auth_username"] = user.username
                    flash("Account created. Welcome to the Tracker.")
                    return redirect(url_for("dashboard"))
    return render_template("signup.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    flash("You’ve been signed out.")
    return redirect(url_for("login"))


def get_or_create_entry(db, entry_date: date, user_id: int, load_related: bool = False) -> DailyEntry:
    query = db.query(DailyEntry).filter_by(entry_date=entry_date, user_id=user_id)
    if load_related:
        query = query.options(
            selectinload(DailyEntry.exercises),
            selectinload(DailyEntry.photos),
        )
    entry = query.first()
    if not entry:
        entry = DailyEntry(entry_date=entry_date, user_id=user_id)
        db.add(entry)
        db.commit()
        if load_related:
            entry = (
                db.query(DailyEntry)
                .options(
                    selectinload(DailyEntry.exercises),
                    selectinload(DailyEntry.photos),
                )
                .filter_by(entry_date=entry_date, user_id=user_id)
                .first()
            )
        else:
            db.refresh(entry)
    return entry


def get_current_user(db) -> User | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return (
        db.query(User)
        .options(
            selectinload(User.profile),
            selectinload(User.notification_pref),
            selectinload(User.subscriptions),
        )
        .filter_by(id=user_id)
        .first()
    )


def get_or_create_pref(db, user_id: int) -> NotificationPreference:
    pref = db.query(NotificationPreference).filter_by(user_id=user_id).first()
    if not pref:
        pref = NotificationPreference(user_id=user_id)
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return pref


def get_or_create_profile(db, user_id: int) -> UserProfile:
    profile = db.query(UserProfile).filter_by(user_id=user_id).first()
    if not profile:
        profile = UserProfile(user_id=user_id)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def get_or_create_mobile_api_token(db, user_id: int, label: str = "android-app") -> MobileApiToken:
    token_record = db.query(MobileApiToken).filter_by(user_id=user_id, label=label).first()
    if not token_record:
        token_record = MobileApiToken(
            user_id=user_id,
            label=label,
            token=secrets.token_urlsafe(32),
        )
        db.add(token_record)
        db.commit()
        db.refresh(token_record)
    return token_record


def get_mobile_api_user(db) -> Optional[User]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None
    token_record = db.query(MobileApiToken).filter_by(token=token).first()
    if not token_record:
        return None
    token_record.last_used_at = datetime.utcnow()
    db.add(token_record)
    db.commit()
    return (
        db.query(User)
        .options(
            selectinload(User.profile),
            selectinload(User.notification_pref),
        )
        .filter_by(id=token_record.user_id)
        .first()
    )


def apply_mobile_metric_update(entry: DailyEntry, metric_type: str, value, source: Optional[str] = None) -> bool:
    if metric_type == "weight":
        if value is None:
            return False
        entry.weight = float(value)
        return True
    if metric_type == "body_fat":
        if value is None:
            return False
        entry.body_fat = float(value)
        return True
    if metric_type == "calories":
        if value is None:
            return False
        entry.calories = int(value)
        return True
    if metric_type == "protein":
        if value is None:
            return False
        entry.protein = int(value)
        return True
    if metric_type == "water_oz":
        if value is None:
            return False
        entry.water_oz = int(value)
        return True
    if metric_type == "active_calories":
        return False
    if metric_type == "steps":
        return False
    if metric_type == "hydration_oz":
        if value is None:
            return False
        entry.water_oz = int(value)
        return True
    return False


def persist_health_metric_sample(db, user_id: int, metric_type: str, value: float, recorded_at: datetime, unit: str = "", source: str = "android-health-connect") -> bool:
    sample = (
        db.query(HealthMetricSample)
        .filter_by(user_id=user_id, metric_type=metric_type, recorded_at=recorded_at)
        .first()
    )
    created = False
    if not sample:
        sample = HealthMetricSample(
            user_id=user_id,
            metric_type=metric_type,
            recorded_at=recorded_at,
        )
        created = True
    sample.value = float(value)
    sample.unit = unit or ""
    sample.source = source
    db.add(sample)
    return created


def persist_health_workout_sample(
    db,
    user_id: int,
    workout_type: str,
    start_time: datetime,
    end_time: datetime,
    duration_minutes: Optional[float] = None,
    title: str = "",
    source: str = "android-health-connect",
) -> bool:
    sample = (
        db.query(HealthWorkoutSample)
        .filter_by(
            user_id=user_id,
            workout_type=workout_type,
            start_time=start_time,
            end_time=end_time,
        )
        .first()
    )
    created = False
    if not sample:
        sample = HealthWorkoutSample(
            user_id=user_id,
            workout_type=workout_type,
            start_time=start_time,
            end_time=end_time,
        )
        created = True
    sample.title = title or workout_type
    sample.duration_minutes = duration_minutes
    sample.source = source
    db.add(sample)
    return created


def get_partner_user(db, profile: UserProfile) -> User | None:
    if not profile.partner_user_id:
        return None
    return db.query(User).filter_by(id=profile.partner_user_id).first()


def handle_connection_request(db, user: User, profile: UserProfile) -> List[str]:
    errors: List[str] = []
    if profile.partner_user_id:
        errors.append("You are already connected. Disconnect first.")
        return errors
    username = (request.form.get("connect_username") or "").strip()
    if not username:
        errors.append("Enter a username to connect with.")
        return errors
    target = (
        db.query(User)
        .filter(func.lower(User.username) == username.lower())
        .first()
    )
    if not target:
        errors.append("User not found.")
        return errors
    if target.id == user.id:
        errors.append("You cannot connect with yourself.")
        return errors
    target_profile = get_or_create_profile(db, target.id)
    if target_profile.partner_user_id:
        errors.append("That user is already connected with someone else.")
        return errors
    existing = (
        db.query(ConnectionRequest)
        .filter_by(requester_id=user.id, target_id=target.id, status="pending")
        .first()
    )
    reverse = (
        db.query(ConnectionRequest)
        .filter_by(requester_id=target.id, target_id=user.id, status="pending")
        .first()
    )
    if existing:
        errors.append("You already have a pending request for that user.")
        return errors
    if reverse:
        errors.append("That user already requested you. Check your pending requests.")
        return errors
    db.add(ConnectionRequest(requester_id=user.id, target_id=target.id))
    db.commit()
    flash("Connection request sent.")
    return errors


def handle_connection_response(db, user: User, profile: UserProfile) -> List[str]:
    errors: List[str] = []
    request_id = request.form.get("request_id")
    decision = request.form.get("decision")
    if not request_id or decision not in {"accept", "decline"}:
        errors.append("Invalid request.")
        return errors
    req = (
        db.query(ConnectionRequest)
        .filter_by(id=int(request_id), target_id=user.id, status="pending")
        .first()
    )
    if not req:
        errors.append("Request not found.")
        return errors
    if decision == "decline":
        db.delete(req)
        db.commit()
        flash("Request declined.")
        return errors
    # accept
    if profile.partner_user_id and profile.partner_user_id != req.requester_id:
        errors.append("Disconnect current partner first.")
        return errors
    requester_profile = get_or_create_profile(db, req.requester_id)
    if requester_profile.partner_user_id and requester_profile.partner_user_id != user.id:
        errors.append("Requester is already connected with someone else.")
        return errors
    profile.partner_user_id = req.requester_id
    requester_profile.partner_user_id = user.id
    db.delete(req)
    cleanup_other_requests(db, user.id, req.requester_id)
    db.commit()
    flash("Connection accepted. You can now view side-by-side progress.")
    return errors


def cleanup_other_requests(db, user_id: int, partner_id: int) -> None:
    ids = {user_id}
    if partner_id and partner_id > 0:
        ids.add(partner_id)
    ids_list = list(ids)
    requests = (
        db.query(ConnectionRequest)
        .filter(
            or_(
                ConnectionRequest.requester_id.in_(ids_list),
                ConnectionRequest.target_id.in_(ids_list),
            )
        )
        .all()
    )
    for req in requests:
        if req.target_id in ids or req.requester_id in ids:
            db.delete(req)


def disconnect_partner(db, profile: UserProfile) -> None:
    partner = get_partner_user(db, profile)
    if partner:
        partner_profile = get_or_create_profile(db, partner.id)
        partner_profile.partner_user_id = None
    profile.partner_user_id = None
    cleanup_other_requests(db, profile.user_id, partner.id if partner else None)


def remove_user_account(db, user: User) -> None:
    profile = get_or_create_profile(db, user.id)
    disconnect_partner(db, profile)
    db.query(ConnectionRequest).filter(
        or_(ConnectionRequest.requester_id == user.id, ConnectionRequest.target_id == user.id)
    ).delete(synchronize_session=False)
    for entry in list(user.entries):
        for photo in list(entry.photos):
            delete_photo_file(photo)
    db.delete(user)


def delete_photo_file(photo: ProgressPhoto) -> None:
    path = BASE_DIR / "static" / photo.file_path
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def calculate_fast_status() -> dict:
    now = datetime.now()
    start_today = datetime.combine(now.date(), FAST_START)
    current_start = start_today if now >= start_today else start_today - timedelta(days=1)
    current_end = current_start + FAST_DURATION

    is_fasting = current_start <= now < current_end
    if is_fasting:
        hours_elapsed = (now - current_start).total_seconds() / 3600
        progress = min(hours_elapsed / (FAST_DURATION.total_seconds() / 3600), 1.0)
        next_event = current_end
        message = "Stay fasted until 11:00 AM — hydration + electrolytes only."
        next_label = "Break your fast"
    else:
        hours_elapsed = 0
        progress = 0
        next_event = current_start + timedelta(days=1)
        message = "Fuel window open. Protein-focused meals + hydration."
        next_label = "Start fasting"

    countdown_hours = max((next_event - now).total_seconds() / 3600, 0)
    return {
        "is_active": is_fasting,
        "hours_elapsed": hours_elapsed,
        "progress": progress,
        "target_hours": FAST_DURATION.total_seconds() / 3600,
        "message": message,
        "next_event_label": next_label,
        "next_event_time_str": next_event.strftime("%I:%M %p").lstrip("0"),
        "countdown_hours": countdown_hours,
        "schedule_text": "Fast 7:00 PM → 11:00 AM, meals 11:00 AM → 7:00 PM",
    }


def send_push_message(subscription: NotificationSubscription, title: str, body: str) -> None:
    if not subscription.endpoint or not subscription.p256dh or not subscription.auth:
        app.logger.info("Subscription missing required keys; skipping push.")
        return
    if not app.config["VAPID_PRIVATE_KEY"] or not app.config["VAPID_PUBLIC_KEY"]:
        app.logger.warning("VAPID keys missing; cannot send push.")
        return
    payload = {
        "endpoint": subscription.endpoint,
        "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
    }
    try:
        webpush(
            subscription_info=payload,
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=app.config["VAPID_PRIVATE_KEY"],
            vapid_claims={"sub": f"mailto:{app.config['VAPID_EMAIL']}"},
        )
        app.logger.info("Push dispatched to %s…", subscription.endpoint[:35])
    except WebPushException as exc:
        app.logger.error("Push failed: %s", exc)
        # If push fails (expired subscription), clear it to avoid repeated errors.
        with SessionLocal() as db:
            db_sub = (
                db.query(NotificationSubscription)
                .filter_by(endpoint=subscription.endpoint)
                .first()
            )
            if db_sub:
                db.delete(db_sub)
                db.commit()
        raise


def send_scheduled_notifications():
    now = datetime.now()
    today = now.date()
    with SessionLocal() as db:
        prefs = db.query(NotificationPreference).all()
        for pref in prefs:
            user = pref.user
            if not user or not user.subscriptions:
                continue

            fast_break_dt = datetime.combine(today, time(hour=11))
            if (
                fast_break_dt <= now < fast_break_dt + timedelta(minutes=2)
                and pref.last_fast_break != today
            ):
                for sub in user.subscriptions:
                    send_push_message(sub, "Break Fast", "Fuel up with protein and plants.")
                pref.last_fast_break = today

            fast_start_dt = datetime.combine(today, FAST_START)
            if (
                fast_start_dt <= now < fast_start_dt + timedelta(minutes=2)
                and pref.last_fast_start != today
            ):
                for sub in user.subscriptions:
                    send_push_message(
                        sub, "Start Fast", "Kitchen closes at 7 PM. Finish meals now."
                    )
                pref.last_fast_start = today

            if pref.exercise_time:
                exercise_dt = datetime.combine(today, pref.exercise_time)
                if (
                    exercise_dt <= now < exercise_dt + timedelta(minutes=5)
                    and pref.last_exercise != today
                ):
                    for sub in user.subscriptions:
                        send_push_message(
                            sub,
                            "Exercise Reminder",
                            "Movement window! Vest walk or strength circuit.",
                        )
                    pref.last_exercise = today
        db.commit()


def calculate_weekly_summary(entries: List[DailyEntry], user_id: int) -> dict:
    summary = {
        "water_total": 0,
        "protein_total": 0,
        "calories_total": 0,
        "days_with_logs": 0,
        "vest_walks": 0,
        "strength_sessions": 0,
        "active_days": 0,
        "movement_streak": 0,
    }
    for entry in entries:
        summary["days_with_logs"] += 1
        summary["water_total"] += entry.water_oz or 0
        summary["protein_total"] += entry.protein or 0
        summary["calories_total"] += entry.calories or 0
        if entry.exercises:
            summary["active_days"] += 1
        for workout in entry.exercises:
            if workout.type == "vest":
                summary["vest_walks"] += 1
            if workout.type == "strength":
                summary["strength_sessions"] += 1
    summary["movement_streak"] = calculate_movement_streak(user_id)
    return summary


def calculate_movement_streak(user_id: int) -> int:
    streak = 0
    cursor = date.today()
    with SessionLocal() as db:
        while True:
            entry = (
                db.query(DailyEntry)
                .filter_by(entry_date=cursor, user_id=user_id)
                .first()
            )
            if entry and entry.exercises:
                streak += 1
                cursor = cursor - timedelta(days=1)
            else:
                break
    return streak


def build_success_markers(entry: DailyEntry, profile: UserProfile | None) -> List[dict]:
    water_goal = (profile.water_target if profile and profile.water_target else 90)
    protein_goal = (profile.protein_target if profile and profile.protein_target else 120)
    return [
        {
            "label": "Water intake",
            "value": f"{entry.water_oz} oz",
            "target": f"{water_goal} oz",
            "met": (entry.water_oz or 0) >= water_goal,
        },
        {
            "label": "Protein",
            "value": f"{entry.protein} g",
            "target": f"{protein_goal} g",
            "met": (entry.protein or 0) >= protein_goal,
        },
        {
            "label": "Movement",
            "value": f"{len(entry.exercises)} sessions",
            "target": "Show up",
            "met": bool(entry.exercises),
        },
    ]


def get_today_workout(plan: dict, target_day: date | None = None) -> dict:
    target_day = target_day or date.today()
    weekday_name = target_day.strftime("%A")
    default_workout = {
        "day": weekday_name,
        "title": "Recovery / reset day",
        "strength": [],
        "core": [],
        "finisher": ["Walk, recover, and stay consistent."],
        "is_rest_day": True,
    }
    schedule = ((plan or {}).get("exercise") or {}).get("schedule") or []
    for workout in schedule:
        if workout.get("day") == weekday_name:
            enriched = dict(workout)
            enriched["strength"] = workout.get("strength") or []
            enriched["core"] = workout.get("core") or []
            enriched["finisher"] = workout.get("finisher") or []
            enriched["is_rest_day"] = False
            return enriched
    return default_workout


def build_dashboard_hero(today_entry: DailyEntry, profile: UserProfile | None, plan: dict, weekly_summary: dict) -> dict:
    workout = get_today_workout(plan)
    targets = {
        "calories": profile.calorie_target if profile and profile.calorie_target else None,
        "protein": profile.protein_target if profile and profile.protein_target else None,
        "water": profile.water_target if profile and profile.water_target else None,
    }
    completion_checks = [
        {"label": "Workout logged", "done": bool(today_entry.exercises)},
        {"label": "Calories logged", "done": (today_entry.calories or 0) > 0},
        {"label": "Protein logged", "done": (today_entry.protein or 0) > 0},
        {"label": "Water logged", "done": (today_entry.water_oz or 0) > 0},
        {"label": "Weight logged", "done": today_entry.weight is not None},
    ]
    completed_count = sum(1 for check in completion_checks if check["done"])
    return {
        "workout": workout,
        "targets": targets,
        "checks": completion_checks,
        "completed_count": completed_count,
        "total_checks": len(completion_checks),
        "last_workout": today_entry.exercises[-1] if today_entry.exercises else None,
        "active_days": weekly_summary.get("active_days", 0),
        "movement_streak": weekly_summary.get("movement_streak", 0),
    }


def build_forward_filled_series(entries_list: List[DailyEntry], labels_window: List[date]) -> dict:
    lookup = {e.entry_date: e for e in entries_list}
    last_weight = None
    last_body_fat = None
    last_calories = 0
    last_protein = 0
    last_water = 0

    labels = []
    weight_series = []
    body_fat_series = []
    calories_series = []
    protein_series = []
    water_series = []
    points = []

    for day in labels_window:
        entry = lookup.get(day)
        if entry:
            if entry.weight is not None:
                last_weight = entry.weight
            if entry.body_fat is not None:
                last_body_fat = entry.body_fat
            if entry.calories not in (None, 0):
                last_calories = entry.calories
            if entry.protein not in (None, 0):
                last_protein = entry.protein
            if entry.water_oz not in (None, 0):
                last_water = entry.water_oz

        label = day.strftime("%m/%d")
        labels.append(label)
        weight_series.append(last_weight)
        body_fat_series.append(last_body_fat)
        calories_series.append(last_calories)
        protein_series.append(last_protein)
        water_series.append(last_water)
        points.append(
            {
                "label": label,
                "calories": last_calories,
                "protein": last_protein,
                "water": last_water,
            }
        )

    return {
        "labels": labels,
        "weight": weight_series,
        "body_fat": body_fat_series,
        "calories": calories_series,
        "protein": protein_series,
        "water": water_series,
        "points": points,
    }


def get_recent_photos(db, user_id: int, limit: int = 6) -> List[ProgressPhoto]:
    return (
        db.query(ProgressPhoto)
        .join(DailyEntry, ProgressPhoto.entry_id == DailyEntry.id)
        .filter(DailyEntry.user_id == user_id)
        .order_by(ProgressPhoto.created_at.desc())
        .limit(limit)
        .all()
    )


def get_mindset_prompt(day_key: str) -> str:
    idx = sum(ord(ch) for ch in day_key) % len(MINDSET_PROMPTS)
    return MINDSET_PROMPTS[idx]


@app.route("/", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        with SessionLocal() as db:
            user = get_current_user(db)
            if not user:
                return redirect(url_for("logout"))
            today_entry = get_or_create_entry(db, date.today(), user.id)
            pref = get_or_create_pref(db, user.id)
            handle_form_submission(db, today_entry, pref)
            db.commit()
        return redirect(url_for("dashboard"))

    with SessionLocal() as db:
        user = get_current_user(db)
        if not user:
            return redirect(url_for("logout"))
        pref = get_or_create_pref(db, user.id)
        profile = get_or_create_profile(db, user.id)
        today_entry = get_or_create_entry(db, date.today(), user.id, load_related=True)
        start_range = date.today() - timedelta(days=6)
        weekly_entries = (
            db.query(DailyEntry)
            .filter(
                DailyEntry.entry_date >= start_range,
                DailyEntry.user_id == user.id,
            )
            .options(selectinload(DailyEntry.exercises))
            .order_by(DailyEntry.entry_date.asc())
            .all()
        )
        labels_window = [(start_range + timedelta(days=i)) for i in range(7)]
        self_series = build_forward_filled_series(weekly_entries, labels_window)
        weekly_points = self_series["points"]
        weight_self = self_series["weight"]
        body_fat_self = self_series["body_fat"]
        calories_self = self_series["calories"]
        protein_self = self_series["protein"]
        water_self = self_series["water"]
        recent_photos = get_recent_photos(db, user.id)
        view_mode = request.args.get("view", "solo")
        partner_user = get_partner_user(db, profile)
        partner_snapshot = None
        compare_weight_chart = None
        compare_macro_chart = None
        if partner_user:
            partner_profile = get_or_create_profile(db, partner_user.id)
            partner_entry = get_or_create_entry(db, date.today(), partner_user.id, load_related=True)
            partner_weekly = (
                db.query(DailyEntry)
                .filter(
                    DailyEntry.entry_date >= start_range,
                    DailyEntry.user_id == partner_user.id,
                )
                .options(selectinload(DailyEntry.exercises))
                .order_by(DailyEntry.entry_date.asc())
                .all()
            )
            partner_series = build_forward_filled_series(partner_weekly, labels_window)
            weight_partner = partner_series["weight"]
            calories_partner = partner_series["calories"]
            protein_partner = partner_series["protein"]
            water_partner = partner_series["water"]
            labels_formatted = partner_series["labels"]
            compare_weight_chart = {
                "labels": labels_formatted,
                "self": weight_self,
                "partner": weight_partner,
            }
            compare_macro_chart = {
                "labels": labels_formatted,
                "calories_self": calories_self,
                "calories_partner": calories_partner,
                "protein_self": protein_self,
                "protein_partner": protein_partner,
                "water_self": water_self,
                "water_partner": water_partner,
            }
            partner_points = partner_series["points"]
            partner_targets = {
                "calories": partner_profile.calorie_target or partner_profile.maintenance_calories or None,
                "protein": partner_profile.protein_target or None,
                "water": partner_profile.water_target or None,
            }
            partner_snapshot = {
                "username": partner_user.username,
                "entry": partner_entry,
                "targets": partner_targets,
                "weekly_summary": calculate_weekly_summary(partner_weekly, partner_user.id),
                "weekly_points": partner_points,
            }
        else:
            view_mode = "solo"
        if view_mode not in {"solo", "partner"} or not partner_snapshot:
            view_mode = "solo"

        subscriptions = list(user.subscriptions)
        metrics_labels = self_series["labels"]
        metrics_weight = weight_self
        metrics_body = body_fat_self
        metrics_available = any(v is not None for v in metrics_weight + metrics_body)
        plan_context = build_plan_for_user(profile)
        weekly_summary = calculate_weekly_summary(weekly_entries, user.id)
        dashboard_hero = build_dashboard_hero(today_entry, profile, plan_context, weekly_summary)
        targets = {
            "calories": profile.calorie_target or profile.maintenance_calories or None,
            "protein": profile.protein_target or None,
            "water": profile.water_target or None,
        }

        context = {
            "plan": plan_context,
            "plan_game_text": plan_context.get("custom_text", ""),
            "today": date.today().isoformat(),
            "today_entry": today_entry,
            "targets": targets,
            "fast_status": calculate_fast_status(),
            "weekly_summary": weekly_summary,
            "dashboard_hero": dashboard_hero,
            "success_markers": build_success_markers(today_entry, profile),
            "mindset_prompt": get_mindset_prompt(date.today().isoformat()),
            "recent_photos": [
                {
                    "file_path": photo.file_path,
                    "caption": photo.caption,
                    "created": photo.created_at.strftime("%b %d, %Y"),
                }
                for photo in recent_photos
            ],
            "metrics_chart": {
                "labels": metrics_labels,
                "weight": metrics_weight,
                "bodyFat": metrics_body,
            },
            "metrics_available": metrics_available,
            "weekly_points": weekly_points,
            "compare_weight_chart": compare_weight_chart,
            "compare_macro_chart": compare_macro_chart,
            "notification_time": pref.exercise_time.strftime("%H:%M")
            if pref.exercise_time
            else "",
            "notifications_enabled": bool(subscriptions),
            "current_year": date.today().year,
            "view_mode": view_mode,
            "partner_summary": partner_snapshot,
        }
    return render_template("dashboard.html", **context)


def handle_form_submission(db, entry: DailyEntry, pref: NotificationPreference) -> None:
    action = request.form.get("action")
    if action == "log_water":
        amount = int(request.form.get("water_oz", 0) or 0)
        entry.water_oz += max(amount, 0)
        flash("Water logged. Hydration quietly wins.")
    elif action == "log_nutrition":
        calories = int(request.form.get("calories", 0) or 0)
        protein = int(request.form.get("protein", 0) or 0)
        entry.calories += max(calories, 0)
        entry.protein += max(protein, 0)
        flash("Nutrition added. Protein first, always.")
    elif action == "log_exercise":
        workout_type = request.form.get("workout_type", "vest")
        duration = request.form.get("duration", "").strip() or "n/a"
        incline = request.form.get("incline", "").strip()
        vest = request.form.get("vest") == "on"
        db.add(
            Exercise(
                entry_id=entry.id,
                type=workout_type,
                duration=duration,
                incline=incline,
                vest=vest,
            )
        )
        flash("Movement logged. Showing up builds momentum.")
    elif action == "log_mindset":
        entry.mindset_energy = request.form.get("energy", "")
        entry.mindset_highlight = request.form.get("highlight", "")
        entry.mindset_proud = request.form.get("proud", "")
        flash("Mindset check-in saved. Gentle awareness matters.")
    elif action == "upload_photo":
        file = request.files.get("photo_file")
        caption = request.form.get("caption", "").strip()
        if not file or file.filename == "":
            flash("Choose an image before uploading.")
            return
        if not allowed_file(file.filename):
            flash("Unsupported file type. Use jpg, png, gif, or webp.")
            return
        safe_name = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"{entry.entry_date.isoformat()}_{timestamp}_{safe_name}"
        path = UPLOAD_DIR / filename
        file.save(path)
        db.add(
            ProgressPhoto(
                entry_id=entry.id,
                file_path=f"uploads/{filename}",
                caption=caption,
            )
        )
        flash("Progress photo added. Keep tracking the visual wins.")
    elif action == "save_notifications":
        exercise_time_str = request.form.get("exercise_time", "").strip()
        if exercise_time_str:
            try:
                pref.exercise_time = datetime.strptime(exercise_time_str, "%H:%M").time()
                pref.last_exercise = None
                flash("Exercise reminder updated.")
            except ValueError:
                flash("Invalid time format. Use HH:MM.")
        else:
            pref.exercise_time = None
            flash("Exercise reminder cleared.")
    elif action == "log_metrics":
        weight = request.form.get("weight", "").strip()
        body_fat = request.form.get("body_fat", "").strip()
        if weight:
            try:
                entry.weight = float(weight)
            except ValueError:
                flash("Weight must be a number.")
                return
        else:
            entry.weight = None
        if body_fat:
            try:
                entry.body_fat = float(body_fat)
            except ValueError:
                flash("Body fat must be a number.")
                return
        else:
            entry.body_fat = None
        flash("Metrics updated.")


@app.route("/api/tracker")
def tracker_api():
    with SessionLocal() as db:
        user = get_current_user(db)
        if not user:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        entry = get_or_create_entry(db, date.today(), user.id)
        data = {
            "date": entry.entry_date.isoformat(),
            "calories": entry.calories,
            "protein": entry.protein,
            "water_oz": entry.water_oz,
            "weight": entry.weight,
            "body_fat": entry.body_fat,
            "exercises": [
                {
                    "type": ex.type,
                    "duration": ex.duration,
                    "incline": ex.incline,
                    "vest": ex.vest,
                    "timestamp": ex.timestamp.isoformat(),
                }
                for ex in entry.exercises
            ],
            "mindset": {
                "energy": entry.mindset_energy,
                "highlight": entry.mindset_highlight,
                "proud": entry.mindset_proud,
            },
        }
    return jsonify(data)


@app.route("/api/mobile/login", methods=["POST"])
def mobile_login():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""

    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password are required"}), 400

    with SessionLocal() as db:
        user = db.query(User).filter(func.lower(User.username) == username.lower()).first()
        if not user or not user.verify_password(password):
            return jsonify({"ok": False, "error": "Invalid credentials"}), 401
        token_record = get_or_create_mobile_api_token(db, user.id)
        return jsonify(
            {
                "ok": True,
                "token": token_record.token,
                "label": token_record.label,
                "user": {
                    "id": user.id,
                    "username": user.username,
                },
            }
        )


@app.route("/api/mobile/auth-token", methods=["POST"])
def mobile_auth_token():
    with SessionLocal() as db:
        user = get_current_user(db)
        if not user:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        token_record = get_or_create_mobile_api_token(db, user.id)
        return jsonify(
            {
                "ok": True,
                "token": token_record.token,
                "label": token_record.label,
                "user": {
                    "id": user.id,
                    "username": user.username,
                },
            }
        )


@app.route("/api/mobile/health/sync", methods=["POST"])
def mobile_health_sync():
    payload = request.get_json(silent=True) or {}
    records = payload.get("records") or []
    workouts = payload.get("workouts") or []
    source = payload.get("source") or "android-health-connect"
    sync_started_at = payload.get("sync_started_at")

    try:
        with SessionLocal() as db:
            user = get_mobile_api_user(db)
            if not user:
                return jsonify({"ok": False, "error": "Unauthorized"}), 401

            processed = 0
            persisted = 0
            skipped = 0
            dates_touched = set()
            sample_counts = {
                "metrics": 0,
                "workouts": 0,
            }

            for record in records:
                metric_type = (record.get("type") or "").strip()
                value = record.get("value")
                unit = (record.get("unit") or "").strip()
                recorded_at_raw = record.get("recorded_at") or record.get("start_time")
                if not metric_type or value is None or recorded_at_raw is None:
                    skipped += 1
                    continue
                try:
                    recorded_at = datetime.fromisoformat(recorded_at_raw.replace("Z", "+00:00"))
                except Exception:
                    skipped += 1
                    continue
                entry_date = recorded_at.date()
                entry = get_or_create_entry(db, entry_date, user.id)
                did_persist_entry = apply_mobile_metric_update(entry, metric_type, value, source=source)
                did_persist_sample = persist_health_metric_sample(
                    db,
                    user_id=user.id,
                    metric_type=metric_type,
                    value=float(value),
                    recorded_at=recorded_at,
                    unit=unit,
                    source=source,
                )
                processed += 1
                if did_persist_entry:
                    db.add(entry)
                    dates_touched.add(entry_date.isoformat())
                if did_persist_sample:
                    sample_counts["metrics"] += 1
                if did_persist_entry or did_persist_sample:
                    persisted += 1
                else:
                    skipped += 1

            for workout in workouts:
                workout_type = (workout.get("type") or "workout").strip()
                title = (workout.get("title") or workout_type).strip()
                start_time_raw = workout.get("start_time")
                end_time_raw = workout.get("end_time")
                if not start_time_raw or not end_time_raw:
                    skipped += 1
                    continue
                try:
                    start_time = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
                    end_time = datetime.fromisoformat(end_time_raw.replace("Z", "+00:00"))
                except Exception:
                    skipped += 1
                    continue
                duration_minutes = workout.get("duration_minutes")
                did_persist_workout = persist_health_workout_sample(
                    db,
                    user_id=user.id,
                    workout_type=workout_type,
                    start_time=start_time,
                    end_time=end_time,
                    duration_minutes=float(duration_minutes) if duration_minutes is not None else None,
                    title=title,
                    source=source,
                )
                processed += 1
                if did_persist_workout:
                    persisted += 1
                    sample_counts["workouts"] += 1
                    dates_touched.add(start_time.date().isoformat())
                else:
                    skipped += 1

            db.commit()

            return jsonify(
                {
                    "ok": True,
                    "source": source,
                    "sync_started_at": sync_started_at,
                    "processed": processed,
                    "persisted": persisted,
                    "skipped": skipped,
                    "dates_touched": sorted(dates_touched),
                    "sample_counts": sample_counts,
                }
            )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/mobile/today", methods=["GET"])
def mobile_today_summary():
    with SessionLocal() as db:
        user = get_mobile_api_user(db)
        if not user:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        today_entry = get_or_create_entry(db, date.today(), user.id, load_related=True)
        latest_weight = (
            db.query(HealthMetricSample)
            .filter_by(user_id=user.id, metric_type="weight")
            .order_by(HealthMetricSample.recorded_at.desc())
            .first()
        )
        latest_body_fat = (
            db.query(HealthMetricSample)
            .filter_by(user_id=user.id, metric_type="body_fat")
            .order_by(HealthMetricSample.recorded_at.desc())
            .first()
        )
        latest_steps = (
            db.query(HealthMetricSample)
            .filter_by(user_id=user.id, metric_type="steps")
            .order_by(HealthMetricSample.recorded_at.desc())
            .first()
        )
        today_steps_total = (
            db.query(func.coalesce(func.sum(HealthMetricSample.value), 0))
            .filter(
                HealthMetricSample.user_id == user.id,
                HealthMetricSample.metric_type == "steps",
                func.date(HealthMetricSample.recorded_at) == date.today().isoformat(),
            )
            .scalar()
        )
        today_calories_total = (
            db.query(func.coalesce(func.sum(HealthMetricSample.value), 0))
            .filter(
                HealthMetricSample.user_id == user.id,
                HealthMetricSample.metric_type == "calories",
                func.date(HealthMetricSample.recorded_at) == date.today().isoformat(),
            )
            .scalar()
        )
        today_protein_total = (
            db.query(func.coalesce(func.sum(HealthMetricSample.value), 0))
            .filter(
                HealthMetricSample.user_id == user.id,
                HealthMetricSample.metric_type == "protein",
                func.date(HealthMetricSample.recorded_at) == date.today().isoformat(),
            )
            .scalar()
        )
        today_body_fat = (
            db.query(HealthMetricSample)
            .filter(
                HealthMetricSample.user_id == user.id,
                HealthMetricSample.metric_type == "body_fat",
                func.date(HealthMetricSample.recorded_at) == date.today().isoformat(),
            )
            .order_by(HealthMetricSample.recorded_at.desc())
            .first()
        )
        latest_calories = (
            db.query(HealthMetricSample)
            .filter_by(user_id=user.id, metric_type="calories")
            .order_by(HealthMetricSample.recorded_at.desc())
            .first()
        )
        latest_protein = (
            db.query(HealthMetricSample)
            .filter_by(user_id=user.id, metric_type="protein")
            .order_by(HealthMetricSample.recorded_at.desc())
            .first()
        )
        planned_workout = get_today_workout(build_plan_for_user(user.profile))
        weekly_workout_schedule = ((build_plan_for_user(user.profile) or {}).get("exercise") or {}).get("schedule") or []
        latest_workout = (
            db.query(HealthWorkoutSample)
            .filter_by(user_id=user.id)
            .order_by(HealthWorkoutSample.start_time.desc())
            .first()
        )
        last_sync_sample = (
            db.query(HealthMetricSample)
            .filter_by(user_id=user.id)
            .order_by(HealthMetricSample.created_at.desc())
            .first()
        )

        return jsonify(
            {
                "ok": True,
                "today": {
                    "date": today_entry.entry_date.isoformat(),
                    "calories": int(today_calories_total) if today_calories_total else (today_entry.calories or 0),
                    "protein": int(today_protein_total) if today_protein_total else (today_entry.protein or 0),
                    "water_oz": today_entry.water_oz or 0,
                    "weight": latest_weight.value if latest_weight else today_entry.weight,
                    "body_fat": today_body_fat.value if today_body_fat else (latest_body_fat.value if latest_body_fat else today_entry.body_fat),
                    "exercise_count": len(today_entry.exercises),
                },
                "latest": {
                    "weight": latest_weight.value if latest_weight else None,
                    "body_fat": latest_body_fat.value if latest_body_fat else None,
                    "steps": float(today_steps_total) if today_steps_total else (latest_steps.value if latest_steps else None),
                    "last_sync_at": last_sync_sample.created_at.isoformat() if last_sync_sample else None,
                },
                "workout": {
                    "title": planned_workout.get("title"),
                    "type": latest_workout.workout_type if latest_workout else None,
                    "start_time": latest_workout.start_time.isoformat() if latest_workout else None,
                    "duration_minutes": latest_workout.duration_minutes if latest_workout else None,
                    "day": planned_workout.get("day"),
                    "strength": planned_workout.get("strength") or [],
                    "core": planned_workout.get("core") or [],
                    "finisher": planned_workout.get("finisher") or [],
                    "is_rest_day": bool(planned_workout.get("is_rest_day")),
                },
                "weekly_workouts": [
                    {
                        "day": workout.get("day"),
                        "title": workout.get("title"),
                        "strength": workout.get("strength") or [],
                        "core": workout.get("core") or [],
                        "finisher": workout.get("finisher") or [],
                        "is_rest_day": False,
                    }
                    for workout in weekly_workout_schedule
                ],
            }
        )


@app.route("/api/mobile/log", methods=["POST"])
def mobile_quick_log():
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip()

    with SessionLocal() as db:
        user = get_mobile_api_user(db)
        if not user:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        entry = get_or_create_entry(db, date.today(), user.id, load_related=True)

        if action == "log_water":
            amount = int(payload.get("amount") or 0)
            entry.water_oz = (entry.water_oz or 0) + amount
        elif action == "log_protein":
            amount = int(payload.get("amount") or 0)
            entry.protein = (entry.protein or 0) + amount
        elif action == "log_calories":
            amount = int(payload.get("amount") or 0)
            entry.calories = (entry.calories or 0) + amount
        elif action == "log_weight":
            value = payload.get("value")
            if value is None:
                return jsonify({"ok": False, "error": "Weight value required"}), 400
            entry.weight = float(value)
            persist_health_metric_sample(
                db,
                user_id=user.id,
                metric_type="weight",
                value=float(value),
                recorded_at=datetime.utcnow(),
                unit="kg",
                source="android-quick-log",
            )
        elif action == "log_workout":
            workout_type = (payload.get("workout_type") or "strength").strip()
            duration_minutes = int(payload.get("duration_minutes") or 30)
            db.add(
                Exercise(
                    entry_id=entry.id,
                    type=workout_type,
                    duration=f"{duration_minutes} min",
                    incline="",
                    vest=False,
                )
            )
            now = datetime.utcnow()
            persist_health_workout_sample(
                db,
                user_id=user.id,
                workout_type=workout_type,
                start_time=now - timedelta(minutes=duration_minutes),
                end_time=now,
                duration_minutes=float(duration_minutes),
                title=workout_type.replace("_", " ").title(),
                source="android-quick-log",
            )
        else:
            return jsonify({"ok": False, "error": "Unsupported action"}), 400

        db.add(entry)
        db.commit()
        db.refresh(entry)

        return jsonify(
            {
                "ok": True,
                "action": action,
                "today": {
                    "date": entry.entry_date.isoformat(),
                    "calories": entry.calories,
                    "protein": entry.protein,
                    "water_oz": entry.water_oz,
                    "weight": entry.weight,
                    "body_fat": entry.body_fat,
                    "exercise_count": len(entry.exercises),
                },
            }
        )


@app.route("/api/mobile/history", methods=["GET"])
def mobile_history():
    limit = request.args.get("limit", default=20, type=int)
    limit = max(1, min(limit, 100))

    with SessionLocal() as db:
        user = get_mobile_api_user(db)
        if not user:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        metric_samples = (
            db.query(HealthMetricSample)
            .filter_by(user_id=user.id)
            .order_by(HealthMetricSample.recorded_at.desc())
            .limit(limit)
            .all()
        )
        workouts = (
            db.query(HealthWorkoutSample)
            .filter_by(user_id=user.id)
            .order_by(HealthWorkoutSample.start_time.desc())
            .limit(limit)
            .all()
        )
        daily_entries = (
            db.query(DailyEntry)
            .filter_by(user_id=user.id)
            .order_by(DailyEntry.entry_date.desc())
            .limit(limit)
            .all()
        )

        return jsonify(
            {
                "ok": True,
                "metrics": [
                    {
                        "type": sample.metric_type,
                        "value": sample.value,
                        "unit": sample.unit,
                        "source": sample.source,
                        "recorded_at": sample.recorded_at.isoformat(),
                    }
                    for sample in metric_samples
                ],
                "workouts": [
                    {
                        "title": workout.title,
                        "type": workout.workout_type,
                        "start_time": workout.start_time.isoformat(),
                        "end_time": workout.end_time.isoformat(),
                        "duration_minutes": workout.duration_minutes,
                        "source": workout.source,
                    }
                    for workout in workouts
                ],
                "entries": [
                    {
                        "date": entry.entry_date.isoformat(),
                        "calories": entry.calories,
                        "protein": entry.protein,
                        "water_oz": entry.water_oz,
                        "weight": entry.weight,
                        "body_fat": entry.body_fat,
                    }
                    for entry in daily_entries
                ],
            }
        )


@app.route("/api/mobile/workouts", methods=["GET"])
def mobile_workouts():
    with SessionLocal() as db:
        user = get_mobile_api_user(db)
        if not user:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        profile = db.query(UserProfile).filter_by(user_id=user.id).first()
        plan = build_plan_for_user(profile)
        today_workout = get_today_workout(plan)
        weekly_schedule = ((plan or {}).get("exercise") or {}).get("schedule") or []

        normalized_schedule = [
            {
                "day": workout.get("day"),
                "title": workout.get("title"),
                "strength": workout.get("strength") or [],
                "core": workout.get("core") or [],
                "finisher": workout.get("finisher") or [],
                "is_rest_day": False,
            }
            for workout in weekly_schedule
        ]

        return jsonify(
            {
                "ok": True,
                "today_workout": {
                    "day": today_workout.get("day"),
                    "title": today_workout.get("title"),
                    "strength": today_workout.get("strength") or [],
                    "core": today_workout.get("core") or [],
                    "finisher": today_workout.get("finisher") or [],
                    "is_rest_day": bool(today_workout.get("is_rest_day")),
                },
                "weekly_schedule": normalized_schedule,
            }
        )


@app.route("/notifications/subscribe", methods=["POST"])
def subscribe_notifications():
    if not is_authenticated():
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    subscription = data.get("subscription")
    if not subscription:
        return jsonify({"ok": False}), 400
    with SessionLocal() as db:
        user = get_current_user(db)
        if not user:
            return jsonify({"ok": False}), 401
        existing = (
            db.query(NotificationSubscription)
            .filter_by(endpoint=subscription.get("endpoint"))
            .first()
        )
        if not existing:
            existing = NotificationSubscription(user_id=user.id)
        existing.user_id = user.id
        keys = subscription.get("keys", {})
        existing.endpoint = subscription.get("endpoint", "")
        existing.p256dh = keys.get("p256dh", "")
        existing.auth = keys.get("auth", "")
        db.add(existing)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/healthkit", methods=["POST"])
def healthkit_webhook():
    token = request.headers.get("X-Health-Token")
    if not app.config["HEALTHKIT_TOKEN"] or token != app.config["HEALTHKIT_TOKEN"]:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    # Accept data from JSON, form-encoded, or query params for flexibility with clients like Tasker.
    payload_json = request.get_json(silent=True) or {}
    payload = {}
    payload.update(request.args or {})
    payload.update(request.form or {})
    payload.update(payload_json)

    # As a fallback, if we still have nothing, try parsing the raw body text.
    # This helps when Tasker sends slightly malformed JSON that Flask skips.
    if not payload:
        raw_body = (request.get_data(as_text=True) or "").strip()
        if raw_body:
            try:
                payload = json.loads(raw_body)
            except Exception:
                app.logger.warning("Healthkit raw body could not be parsed: %s", raw_body)
    app.logger.info("Healthkit payload received: %s", payload)
    print(f"HK payload: {payload}", flush=True)

    # Accept a few flexible date formats to make Tasker simpler
    date_str = payload.get("date")
    if not date_str:
        date_str = date.today().isoformat()  # fallback to today
    def parse_date_flexible(val):
        # try ISO first
        try:
            return date.fromisoformat(val)
        except Exception:
            pass
        # common Tasker-style short dates: MM-DD-YY or MM/DD/YY
        for fmt in ("%m-%d-%y", "%m/%d/%y", "%m-%d-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(val, fmt).date()
            except Exception:
                continue
        return None
    payload_date = parse_date_flexible(date_str)
    if not payload_date:
        return jsonify({"ok": False, "error": "Invalid date format"}), 400

    username = payload.get("user") or payload.get("username")

    def parse_float(val):
        if val in (None, "", "null"):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def parse_int(val):
        if val in (None, "", "null"):
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def extract_weight(payload_obj):
        """
        Accept plain numbers or nested weight structures from Health Connect.
        Examples:
          payload['weight'] = 94.3
          payload['weight'] = {"kilograms":94.3,"pounds":207.8, ...}
        """
        w = payload_obj.get("weight")
        if isinstance(w, (int, float, str)):
            return parse_float(w)
        if isinstance(w, dict):
            # prefer kilograms if present, otherwise pounds
            if "kilograms" in w:
                return parse_float(w.get("kilograms"))
            if "pounds" in w:
                pounds = parse_float(w.get("pounds"))
                return None if pounds is None else pounds  # stored as given
            # fallback to any numeric in the dict
            for key in ("value", "weightKg", "weight_kg"):
                if key in w:
                    return parse_float(w.get(key))
        return None

    with SessionLocal() as db:
        user = None
        if username:
            user = (
                db.query(User)
                .filter(func.lower(User.username) == username.lower())
                .first()
            )
        if not user:
            user = db.query(User).filter_by(username="Tayleur").first()
        if not user:
            return jsonify({"ok": False, "error": "User not found"}), 404
        entry = get_or_create_entry(db, payload_date, user.id)
        updated = False
        weight_val = extract_weight(payload)
        if weight_val is not None:
            entry.weight = weight_val
            updated = True

        body_fat_val = parse_float(payload.get("body_fat") or payload.get("bodyFat"))
        if body_fat_val is not None:
            entry.body_fat = body_fat_val
            updated = True

        # calories, with optional mealType mapping (1=breakfast, 2=lunch, 3=dinner, 4=snack)
        calories_val = parse_int(payload.get("calories"))
        meal_type = parse_int(payload.get("mealType"))
        if calories_val is not None:
            if meal_type in {1, 2, 3, 4}:
                if meal_type == 1:
                    entry.cal_breakfast = (entry.cal_breakfast or 0) + calories_val
                elif meal_type == 2:
                    entry.cal_lunch = (entry.cal_lunch or 0) + calories_val
                elif meal_type == 3:
                    entry.cal_dinner = (entry.cal_dinner or 0) + calories_val
                elif meal_type == 4:
                    entry.cal_snack = (entry.cal_snack or 0) + calories_val
                entry.calories = (entry.calories or 0) + calories_val
            else:
                entry.calories = calories_val
            updated = True

        # explicit per-meal calories (idempotent: only set if not already set)
        meal_fields = {
            "cal_breakfast": "cal_breakfast",
            "cal_lunch": "cal_lunch",
            "cal_dinner": "cal_dinner",
            "cal_snack": "cal_snack",
        }
        meal_mapping = {
            "cal_breakfast": 1,
            "cal_lunch": 2,
            "cal_dinner": 3,
            "cal_snack": 4,
        }
        for key, attr in meal_fields.items():
            val = parse_int(payload.get(key))
            if val is None or val <= 0:
                continue
            current = getattr(entry, attr, None) or 0
            if current > 0:
                # already have a value for this meal; skip to avoid double counting
                continue
            # set meal calories and add to daily total
            setattr(entry, attr, val)
            entry.calories = (entry.calories or 0) + val
            updated = True

        protein_val = parse_int(payload.get("protein"))
        if protein_val is not None:
            entry.protein = protein_val
            updated = True

        water_val = parse_int(payload.get("water_oz") or payload.get("water"))
        if water_val is not None:
            entry.water_oz = water_val
            updated = True

        if updated:
            db.add(entry)
            db.commit()
        return jsonify({"ok": True, "updated": updated})


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if not is_authenticated():
        return redirect(url_for("login"))
    errors: List[str] = []
    with SessionLocal() as db:
        user = get_current_user(db)
        if not user:
            return redirect(url_for("logout"))
        profile = get_or_create_profile(db, user.id)
        action = request.form.get("action", "update_profile") if request.method == "POST" else None
        if request.method == "POST":
            if action == "update_profile":
                numeric_fields = [
                    ("start_weight", "start_weight", float, "Start weight"),
                    ("goal_weight", "goal_weight", float, "Goal weight"),
                    ("maintenance_calories", "maintenance_calories", int, "Maintenance calories"),
                    ("calorie_target", "calorie_target", int, "Calorie target"),
                    ("protein_target", "protein_target", int, "Protein target"),
                    ("water_target", "water_target", int, "Water goal"),
                ]
                for field, attr, caster, label in numeric_fields:
                    raw = (request.form.get(field, "") or "").strip()
                    if raw == "":
                        setattr(profile, attr, None)
                        continue
                    try:
                        setattr(profile, attr, caster(raw))
                    except ValueError:
                        errors.append(f"{label} must be a number.")
                profile.game_plan = request.form.get("game_plan", "").strip()
                if not errors:
                    db.add(profile)
                    db.commit()
                    flash("Settings updated.")
                    return redirect(url_for("settings"))
            elif action == "request_connection":
                errors.extend(handle_connection_request(db, user, profile))
            elif action == "respond_request":
                errors.extend(handle_connection_response(db, user, profile))
            elif action == "disconnect_partner":
                disconnect_partner(db, profile)
                db.commit()
                flash("Connection removed.")
                return redirect(url_for("settings"))
            elif action == "delete_account":
                confirm = (request.form.get("confirm_text") or "").strip()
                if confirm not in {"DELETE", user.username}:
                    errors.append("Type DELETE or your username to confirm.")
                else:
                    remove_user_account(db, user)
                    db.commit()
                    session.clear()
                    flash("Account deleted.")
                    return redirect(url_for("signup"))
        incoming_requests = (
            db.query(ConnectionRequest)
            .filter_by(target_id=user.id, status="pending")
            .all()
        )
        outgoing_requests = (
            db.query(ConnectionRequest)
            .filter_by(requester_id=user.id, status="pending")
            .all()
        )
        partner_user = get_partner_user(db, profile)
        return render_template(
            "settings.html",
            profile=profile,
            errors=errors,
            incoming_requests=incoming_requests,
            outgoing_requests=outgoing_requests,
            partner_user=partner_user,
            current_year=date.today().year,
        )


@app.route("/sw.js")
def service_worker():
    return send_from_directory(
        app.static_folder,
        "js/sw.js",
        mimetype="application/javascript",
    )


@app.teardown_appcontext
def shutdown_session(exception=None):
    pass

if not scheduler.running:
    scheduler.add_job(
        send_scheduled_notifications, "interval", minutes=1, id="notification-loop", replace_existing=True
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5917, debug=True)
