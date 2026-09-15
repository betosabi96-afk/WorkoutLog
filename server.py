from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
import sqlite3
import json
import uuid
import os


# ==========================================
# CONFIGURACIÓN
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
DB_PATH = BASE_DIR / "data" / "workoutlog.sqlite3"

PORT = int(os.environ.get("PORT", "3000"))

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ==========================================
# BASE DE DATOS
# ==========================================

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    con = connect()

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

        FOREIGN KEY(session_id)
        REFERENCES sessions(id)
        ON DELETE CASCADE
    );
    """)

    con.commit()
    con.close()


# ==========================================
# CONVERTIR SESIÓN A JSON
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
                "id": r["id"],
                "name": r["name"],
                "sets": r["sets"],
                "reps": r["reps"],
                "weightKg": r["weight_kg"],
                "durationMinutes": r["duration_minutes"]
            }
            for r in exercise_rows
        ]
    }


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


    # ======================================
    # GET
    # ======================================

    def do_GET(self):

        path = urlparse(self.path).path

        if path == "/api/health":
            return self.send_json(200, {
                "ok": True,
                "message": "WorkoutLog funcionando"
            })

        if path == "/api/sessions":
            return self.get_sessions()

        if path == "/api/stats":
            return self.get_stats()

        if path == "/":
            self.path = "/index.html"
            return super().do_GET()

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

        return self.send_json(404, {
            "ok": False,
            "error": "Ruta no encontrada"
        })


    # ======================================
    # OBTENER SESIONES
    # ======================================

    def get_sessions(self):

        con = connect()

        sessions = con.execute(
            """
            SELECT *
            FROM sessions
            ORDER BY started_at DESC
            """
        ).fetchall()

        result = []

        for session in sessions:

            exercises = con.execute(
                """
                SELECT *
                FROM exercises
                WHERE session_id = ?
                ORDER BY rowid
                """,
                (session["id"],)
            ).fetchall()

            result.append(
                session_to_dict(
                    session,
                    exercises
                )
            )

        con.close()

        return self.send_json(
            200,
            result
        )


    # ======================================
    # CREAR SESIÓN DESDE WEB
    # ======================================

    def create_session(self):

        try:

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            body = json.loads(
                self.rfile
                .read(length)
                .decode("utf-8")
            )

        except Exception:

            return self.send_json(
                400,
                {
                    "ok": False,
                    "error": "JSON inválido"
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
        ).strip()

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

        try:

            total_duration = int(
                float(
                    body.get(
                        "totalDurationMinutes",
                        0
                    ) or 0
                )
            )

        except Exception:

            total_duration = 0


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
            activity,
            exercises,
            total_duration,
            intensity,
            notes
        )


    # ======================================
    # ATAJO IPHONE
    # ======================================

    def create_shortcut_workout(self):

        try:

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            raw = self.rfile.read(length)

            body = json.loads(
                raw.decode("utf-8")
            )

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


        # El Atajo manda:
        #
        # {
        #   "muscleGroup": "Espalda",
        #   "exercises": [...]
        # }

        muscle_group = str(
            body.get(
                "muscleGroup",
                ""
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
        ).strip()


        notes = str(
            body.get(
                "notes",
                ""
            )
        ).strip()


        try:

            duration = int(
                float(
                    body.get(
                        "durationMinutes",
                        0
                    ) or 0
                )
            )

        except Exception:

            duration = 0


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


        if len(exercises) == 0:

            return self.send_json(
                400,
                {
                    "ok": False,
                    "message": "No hay ejercicios."
                }
            )


        return self.save_workout(
            muscle_group,
            exercises,
            duration,
            intensity,
            notes
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


        try:

            con.execute(
                """
                INSERT INTO sessions
                (
                    id,
                    activity,
                    started_at,
                    total_duration_minutes,
                    intensity,
                    notes
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    activity,
                    started_at,
                    max(0, duration),
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


                try:

                    sets = int(
                        float(
                            exercise.get(
                                "sets",
                                0
                            ) or 0
                        )
                    )

                except Exception:

                    sets = 0


                try:

                    reps = int(
                        float(
                            exercise.get(
                                "reps",
                                0
                            ) or 0
                        )
                    )

                except Exception:

                    reps = 0


                try:

                    weight = float(
                        exercise.get(
                            "weightKg",
                            0
                        ) or 0
                    )

                except Exception:

                    weight = 0


                try:

                    exercise_duration = int(
                        float(
                            exercise.get(
                                "durationMinutes",
                                0
                            ) or 0
                        )
                    )

                except Exception:

                    exercise_duration = 0


                con.execute(
                    """
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
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        session_id,
                        name,
                        max(0, sets),
                        max(0, reps),
                        max(0, weight),
                        max(
                            0,
                            exercise_duration
                        )
                    )
                )


                saved_count += 1


            if saved_count == 0:

                con.rollback()
                con.close()

                return self.send_json(
                    400,
                    {
                        "ok": False,
                        "message": "No encontré ejercicios válidos."
                    }
                )


            con.commit()


            session = con.execute(
                """
                SELECT *
                FROM sessions
                WHERE id = ?
                """,
                (session_id,)
            ).fetchone()


            exercise_rows = con.execute(
                """
                SELECT *
                FROM exercises
                WHERE session_id = ?
                ORDER BY rowid
                """,
                (session_id,)
            ).fetchall()


            result = session_to_dict(
                session,
                exercise_rows
            )


            con.close()


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
            con.close()

            return self.send_json(
                500,
                {
                    "ok": False,
                    "message": "Error al guardar el entrenamiento.",
                    "detail": str(exc)
                }
            )


    # ======================================
    # ESTADÍSTICAS
    # ======================================

    def get_stats(self):

        con = connect()

        rows = con.execute(
            """
            SELECT
                activity,
                started_at,
                total_duration_minutes
            FROM sessions
            """
        ).fetchall()

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
                    r[
                        "total_duration_minutes"
                    ]
                    for r in rows
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