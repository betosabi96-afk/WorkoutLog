from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
import sqlite3
import json
import uuid
import os

import psycopg
from psycopg.rows import dict_row


# ==========================================
# CONFIGURACIÓN
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
DB_PATH = BASE_DIR / "data" / "workoutlog.sqlite3"

DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", "3000"))

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ==========================================
# BASE DE DATOS
# ==========================================

def is_postgres():
    return bool(DATABASE_URL)


def connect():
    if is_postgres():
        return psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row
        )

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def sql_placeholder():
    return "%s" if is_postgres() else "?"


def init_db():
    con = connect()

    try:
        if is_postgres():
            with con.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        activity TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        total_duration_minutes INTEGER NOT NULL DEFAULT 0,
                        intensity TEXT NOT NULL DEFAULT 'Moderada',
                        notes TEXT NOT NULL DEFAULT ''
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS exercises (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        sets INTEGER NOT NULL DEFAULT 0,
                        reps INTEGER NOT NULL DEFAULT 0,
                        weight_kg REAL NOT NULL DEFAULT 0,
                        duration_minutes INTEGER NOT NULL DEFAULT 0,

                        FOREIGN KEY (session_id)
                        REFERENCES sessions(id)
                        ON DELETE CASCADE
                    )
                """)
        else:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    activity TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    total_duration_minutes INTEGER NOT NULL DEFAULT 0,
                    intensity TEXT NOT NULL DEFAULT 'Moderada',
                    notes TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS exercises (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    sets INTEGER NOT NULL DEFAULT 0,
                    reps INTEGER NOT NULL DEFAULT 0,
                    weight_kg REAL NOT NULL DEFAULT 0,
                    duration_minutes INTEGER NOT NULL DEFAULT 0,

                    FOREIGN KEY (session_id)
                    REFERENCES sessions(id)
                    ON DELETE CASCADE
                );
            """)

        con.commit()

    finally:
        con.close()


# ==========================================
# UTILIDADES
# ==========================================

def session_to_dict(session_row, exercise_rows):
    return {
        "id": session_row["id"],
        "activity": session_row["activity"],
        "startedAt": session_row["started_at"],
        "totalDurationMinutes": session_row["total_duration_minutes"],
        "intensity": session_row["intensity"],
        "notes": session_row["notes"],
        "exercises": [
            {
                "id": row["id"],
                "name": row["name"],
                "sets": row["sets"],
                "reps": row["reps"],
                "weightKg": row["weight_kg"],
                "durationMinutes": row["duration_minutes"]
            }
            for row in exercise_rows
        ]
    }


def safe_int(value, default=0):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def safe_float(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


# ==========================================
# SERVIDOR
# ==========================================

class Handler(SimpleHTTPRequestHandler):

    def translate_path(self, path):
        clean = urlparse(path).path.lstrip("/") or "index.html"
        return str(PUBLIC_DIR / clean)

    def send_json(self, status, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2
        ).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(len(body))
        )
        self.send_header(
            "Cache-Control",
            "no-store"
        )
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(
            self.headers.get(
                "Content-Length",
                "0"
            )
        )

        raw = self.rfile.read(length)

        return json.loads(
            raw.decode("utf-8")
        )

    # ======================================
    # GET
    # ======================================

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/health":
            return self.send_json(
                200,
                {
                    "ok": True,
                    "message": "WorkoutLog funcionando",
                    "database": (
                        "postgresql"
                        if is_postgres()
                        else "sqlite"
                    )
                }
            )

        if path == "/api/sessions":
            return self.get_sessions()

        if path == "/api/stats":
            return self.get_stats()

        if path == "/":
            self.path = "/index.html"

        return super().do_GET()

    # ======================================
    # POST
    # ======================================

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/sessions":
            return self.create_session()

        if path == "/api/shortcut-workout":
            return self.create_shortcut_workout()

        return self.send_json(
            404,
            {
                "ok": False,
                "error": "Ruta no encontrada"
            }
        )

    # ======================================
    # OBTENER SESIONES
    # ======================================

    def get_sessions(self):
        con = connect()

        try:
            sessions = con.execute("""
                SELECT *
                FROM sessions
                ORDER BY started_at DESC
            """).fetchall()

            result = []
            placeholder = sql_placeholder()

            for session in sessions:
                exercises = con.execute(
                    f"""
                    SELECT *
                    FROM exercises
                    WHERE session_id = {placeholder}
                    """,
                    (session["id"],)
                ).fetchall()

                result.append(
                    session_to_dict(
                        session,
                        exercises
                    )
                )

            return self.send_json(
                200,
                result
            )

        finally:
            con.close()

    # ======================================
    # CREAR SESIÓN DESDE WEB
    # ======================================

    def create_session(self):
        try:
            body = self.read_json_body()

        except Exception as exc:
            return self.send_json(
                400,
                {
                    "ok": False,
                    "error": "JSON inválido",
                    "detail": str(exc)
                }
            )

        activity = str(
            body.get(
                "activity",
                ""
            )
        ).strip()

        intensity = str(
            body.get(
                "intensity",
                "Moderada"
            )
        ).strip() or "Moderada"

        notes = str(
            body.get(
                "notes",
                ""
            )
        ).strip()

        exercises = body.get(
            "exercises",
            []
        )

        total_duration = safe_int(
            body.get(
                "totalDurationMinutes",
                0
            )
        )

        if not activity:
            return self.send_json(
                400,
                {
                    "ok": False,
                    "error": "Falta activity"
                }
            )

        if not isinstance(
            exercises,
            list
        ) or not exercises:
            return self.send_json(
                400,
                {
                    "ok": False,
                    "error": "Faltan ejercicios"
                }
            )

        return self.save_workout(
            activity=activity,
            exercises=exercises,
            duration=total_duration,
            intensity=intensity,
            notes=notes
        )

    # ======================================
    # ATAJO DE IPHONE
    # ======================================

    def create_shortcut_workout(self):
        try:
            body = self.read_json_body()
            print(
                "BODY RECIBIDO:",
                body
            )

        except Exception as exc:
            return self.send_json(
                400,
                {
                    "ok": False,
                    "message": "No pude leer los datos del iPhone.",
                    "detail": str(exc)
                }
            )

        # Formato esperado:
        #
        # {
        #   "muscleGroup": "Espalda",
        #   "exercises": [
        #       {
        #           "name": "Remo",
        #           "sets": "5",
        #           "reps": "4",
        #           "weightKg": "4"
        #       }
        #   ]
        # }

        muscle_group = str(
            body.get(
                "muscleGroup",
                body.get(
                    "activity",
                    ""
                )
            )
        ).strip()

        exercises = body.get(
            "exercises",
            []
        )

        intensity = str(
            body.get(
                "intensity",
                "Moderada"
            )
        ).strip() or "Moderada"

        notes = str(
            body.get(
                "notes",
                ""
            )
        ).strip()

        duration = safe_int(
            body.get(
                "durationMinutes",
                body.get(
                    "totalDurationMinutes",
                    0
                )
            )
        )

        if not muscle_group:
            return self.send_json(
                400,
                {
                    "ok": False,
                    "message": "Falta muscleGroup."
                }
            )

        if not isinstance(
            exercises,
            list
        ):
            return self.send_json(
                400,
                {
                    "ok": False,
                    "message": "exercises debe ser una lista."
                }
            )

        if not exercises:
            return self.send_json(
                400,
                {
                    "ok": False,
                    "message": "No hay ejercicios."
                }
            )

        return self.save_workout(
            activity=muscle_group,
            exercises=exercises,
            duration=duration,
            intensity=intensity,
            notes=notes
        )

    # ======================================
    # GUARDAR ENTRENAMIENTO
    # ======================================

    def save_workout(
        self,
        activity,
        exercises,
        duration,
        intensity,
        notes
    ):
        session_id = str(
            uuid.uuid4()
        )

        started_at = (
            datetime
            .now(timezone.utc)
            .isoformat()
            .replace(
                "+00:00",
                "Z"
            )
        )

        con = connect()
        placeholder = sql_placeholder()

        try:
            con.execute(
                f"""
                INSERT INTO sessions
                (
                    id,
                    activity,
                    started_at,
                    total_duration_minutes,
                    intensity,
                    notes
                )
                VALUES (
                    {placeholder},
                    {placeholder},
                    {placeholder},
                    {placeholder},
                    {placeholder},
                    {placeholder}
                )
                """,
                (
                    session_id,
                    activity,
                    started_at,
                    max(
                        0,
                        duration
                    ),
                    intensity,
                    notes
                )
            )

            saved_count = 0

            for exercise in exercises:
                if not isinstance(
                    exercise,
                    dict
                ):
                    continue

                name = str(
                    exercise.get(
                        "name",
                        ""
                    )
                ).strip()

                if not name:
                    continue

                sets = max(
                    0,
                    safe_int(
                        exercise.get(
                            "sets",
                            0
                        )
                    )
                )

                reps = max(
                    0,
                    safe_int(
                        exercise.get(
                            "reps",
                            0
                        )
                    )
                )

                weight = max(
                    0.0,
                    safe_float(
                        exercise.get(
                            "weightKg",
                            0
                        )
                    )
                )

                exercise_duration = max(
                    0,
                    safe_int(
                        exercise.get(
                            "durationMinutes",
                            0
                        )
                    )
                )

                con.execute(
                    f"""
                    INSERT INTO exercises
                    (
                        id,
                        session_id,
                        name,
                        sets,
                        reps,
                        weight_kg,
                        duration_minutes
                    )
                    VALUES (
                        {placeholder},
                        {placeholder},
                        {placeholder},
                        {placeholder},
                        {placeholder},
                        {placeholder},
                        {placeholder}
                    )
                    """,
                    (
                        str(
                            uuid.uuid4()
                        ),
                        session_id,
                        name,
                        sets,
                        reps,
                        weight,
                        exercise_duration
                    )
                )

                saved_count += 1

            if saved_count == 0:
                con.rollback()

                return self.send_json(
                    400,
                    {
                        "ok": False,
                        "message": "No encontré ejercicios válidos."
                    }
                )

            con.commit()

            session = con.execute(
                f"""
                SELECT *
                FROM sessions
                WHERE id = {placeholder}
                """,
                (
                    session_id,
                )
            ).fetchone()

            exercise_rows = con.execute(
                f"""
                SELECT *
                FROM exercises
                WHERE session_id = {placeholder}
                """,
                (
                    session_id,
                )
            ).fetchall()

            result = session_to_dict(
                session,
                exercise_rows
            )

            return self.send_json(
                201,
                {
                    "ok": True,
                    "message": "Entrenamiento guardado.",
                    "session": result
                }
            )

        except Exception as exc:
            con.rollback()

            print(
                "ERROR AL GUARDAR:",
                repr(exc)
            )

            return self.send_json(
                500,
                {
                    "ok": False,
                    "message": "Error al guardar el entrenamiento.",
                    "detail": str(exc)
                }
            )

        finally:
            con.close()

    # ======================================
    # ESTADÍSTICAS
    # ======================================

    def get_stats(self):
        con = connect()

        try:
            rows = con.execute("""
                SELECT
                    activity,
                    started_at,
                    total_duration_minutes
                FROM sessions
            """).fetchall()

        finally:
            con.close()

        now = datetime.now(
            timezone.utc
        )

        week_start = (
            now
            - timedelta(
                days=now.weekday()
            )
        ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        month_start = now.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        week_sessions = 0
        week_minutes = 0

        month_sessions = 0
        month_minutes = 0

        counts = {}

        for row in rows:
            activity = row["activity"]

            counts[activity] = (
                counts.get(
                    activity,
                    0
                )
                + 1
            )

            try:
                dt = datetime.fromisoformat(
                    row["started_at"]
                    .replace(
                        "Z",
                        "+00:00"
                    )
                )

            except Exception:
                continue

            if dt >= week_start:
                week_sessions += 1
                week_minutes += (
                    row[
                        "total_duration_minutes"
                    ]
                )

            if dt >= month_start:
                month_sessions += 1
                month_minutes += (
                    row[
                        "total_duration_minutes"
                    ]
                )

        top_activity = (
            max(
                counts,
                key=counts.get
            )
            if counts
            else None
        )

        return self.send_json(
            200,
            {
                "totalSessions": len(rows),

                "totalMinutes": sum(
                    row[
                        "total_duration_minutes"
                    ]
                    for row in rows
                ),

                "thisWeekSessions":
                    week_sessions,

                "thisWeekMinutes":
                    week_minutes,

                "thisMonthSessions":
                    month_sessions,

                "thisMonthMinutes":
                    month_minutes,

                "topActivity":
                    top_activity
            }
        )


# ==========================================
# INICIAR
# ==========================================

if __name__ == "__main__":
    init_db()

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        Handler
    )

    print("")
    print("==============================")
    print(" WorkoutLog funcionando")
    print("==============================")
    print(
        f"http://localhost:{PORT}"
    )
    print("==============================")
    print("")

    server.serve_forever()
