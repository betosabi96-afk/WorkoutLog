from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import sqlite3
import json
import uuid
import os
import math
import secrets
import hashlib
import hmac
import re
import contextvars
import threading
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError


# ==========================================
# CONFIGURACIÓN
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
PUBLIC_DIR = BASE_DIR / "public"
DB_PATH = BASE_DIR / "data" / "workoutlog.sqlite3"

DATABASE_URL = os.environ.get("DATABASE_URL")
APP_DATABASE_URL = os.environ.get("WORKOUTLOG_APP_DATABASE_URL", "").strip()
AUTH_DATABASE_URL = os.environ.get("WORKOUTLOG_AUTH_DATABASE_URL", "").strip()
APP_DATABASE_PASSWORD = os.environ.get("WORKOUTLOG_APP_PASSWORD", "")
AUTH_DATABASE_PASSWORD = os.environ.get("WORKOUTLOG_AUTH_PASSWORD", "")
# Al detectar una conexión técnica, exigir modo multiusuario para no caer en SQLite.
# WORKOUTLOG_MULTIUSER=1 también permite activarlo explícitamente.
MULTIUSER = (os.environ.get("WORKOUTLOG_MULTIUSER") == "1"
             or bool(APP_DATABASE_URL) or bool(AUTH_DATABASE_URL))
# HTTP sin cookie Secure solo para pruebas locales. En internet debe usarse HTTPS.
COOKIE_SECURE = os.environ.get("WORKOUTLOG_COOKIE_SECURE", "1") != "0"
ACTIVE_USER = contextvars.ContextVar("workoutlog_user", default=None)
PASSWORD_HASHER = PasswordHasher()
LOGIN_FAILURES = {}
LOGIN_LOCK = threading.Lock()

PORT = int(os.environ.get("PORT", "3000"))

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ==========================================
# BASE DE DATOS
# ==========================================

def is_postgres():
    return MULTIUSER or bool(DATABASE_URL)


def connect():
    if MULTIUSER:
        user_id = ACTIVE_USER.get()
        if not user_id:
            raise PermissionError("Sesión de usuario requerida")
        if not APP_DATABASE_URL:
            raise RuntimeError("Falta WORKOUTLOG_APP_DATABASE_URL")
        if not APP_DATABASE_PASSWORD:
            raise RuntimeError("Falta WORKOUTLOG_APP_PASSWORD")
        con = psycopg.connect(APP_DATABASE_URL, password=APP_DATABASE_PASSWORD,
                              row_factory=dict_row)
        try:
            # Contexto por transacción. Nunca se acepta user_id desde la petición.
            con.execute("SELECT set_config('app.current_user_id', %s, true)", (str(user_id),))
        except Exception:
            con.close()
            raise
        return con
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
    return "%s" if (MULTIUSER or is_postgres()) else "?"


def init_db():
    if MULTIUSER:
        # Nunca aplicar DDL con la cuenta restringida sobre una BD restaurada.
        return
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
                        distance_km REAL NOT NULL DEFAULT 0,
                        muscle_group TEXT NOT NULL DEFAULT '',

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
                    distance_km REAL NOT NULL DEFAULT 0,
                    muscle_group TEXT NOT NULL DEFAULT '',

                    FOREIGN KEY (session_id)
                    REFERENCES sessions(id)
                    ON DELETE CASCADE
                );
            """)

        if is_postgres():
            with con.cursor() as cur:
                cur.execute("""
                    ALTER TABLE exercises
                    ADD COLUMN IF NOT EXISTS muscle_group TEXT NOT NULL DEFAULT ''
                """)

                cur.execute("""
                    ALTER TABLE exercises
                    ADD COLUMN IF NOT EXISTS distance_km REAL NOT NULL DEFAULT 0
                """)
        else:
            columns = con.execute("PRAGMA table_info(exercises)").fetchall()
            column_names = [row["name"] for row in columns]

            if "muscle_group" not in column_names:
                con.execute("""
                    ALTER TABLE exercises
                    ADD COLUMN muscle_group TEXT NOT NULL DEFAULT ''
                """)

            if "distance_km" not in column_names:
                con.execute("""
                    ALTER TABLE exercises
                    ADD COLUMN distance_km REAL NOT NULL DEFAULT 0
                """)

        con.commit()

    finally:
        con.close()


# ==========================================
# UTILIDADES
# ==========================================

def session_to_dict(session_row, exercise_rows):
    return {
        "id": str(session_row["id"]),
        "activity": session_row["activity"],
        "startedAt": str(session_row["started_at"]),
        "totalDurationMinutes": session_row["total_duration_minutes"],
        "intensity": session_row["intensity"],
        "notes": session_row["notes"],
        "exercises": [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "sets": row["sets"],
                "reps": row["reps"],
                "weightKg": row["weight_kg"],
                "durationMinutes": row["duration_minutes"],
                "distanceKm": row["distance_km"],
                "muscleGroup": row["muscle_group"]
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

def auth_connect():
    if not AUTH_DATABASE_URL:
        raise RuntimeError("Falta WORKOUTLOG_AUTH_DATABASE_URL")
    if not AUTH_DATABASE_PASSWORD:
        raise RuntimeError("Falta WORKOUTLOG_AUTH_PASSWORD")
    return psycopg.connect(AUTH_DATABASE_URL, password=AUTH_DATABASE_PASSWORD,
                           row_factory=dict_row)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_email(email):
    return isinstance(email, str) and len(email) <= 254 and bool(
        re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
    )


def user_from_token(token):
    if not token or len(token) > 256:
        return None
    try:
        with auth_connect() as con:
            row = con.execute("""
                SELECT user_id FROM public.auth_sessions
                WHERE token_hash = %s
                  AND revoked_at IS NULL AND expires_at > NOW()
            """, (hash_token(token),)).fetchone()
            return str(row["user_id"]) if row else None
    except psycopg.Error as exc:
        print("ERROR en autenticación:", type(exc).__name__)
        return None


class Handler(SimpleHTTPRequestHandler):

    def auth_token(self):
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return authorization[7:].strip(), "bearer"
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            item = cookies.get("workoutlog_session")
            return (item.value, "cookie") if item else (None, None)
        except Exception:
            return None, None

    def require_user(self, mutation=False):
        if not MULTIUSER:
            return True
        token, source = self.auth_token()
        if mutation and source == "cookie":
            # La protección SameSite no sustituye verificación de origen.
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            if not origin or not host:
                self.send_json(403, {"ok": False, "error": "Origen requerido"})
                return False
            parsed = urlsplit(origin)
            if parsed.netloc.lower() != host.lower() or parsed.scheme not in ("https", "http"):
                self.send_json(403, {"ok": False, "error": "Origen no autorizado"})
                return False
            if COOKIE_SECURE and parsed.scheme != "https":
                self.send_json(403, {"ok": False, "error": "HTTPS requerido"})
                return False
        user_id = user_from_token(token)
        if not user_id:
            self.send_json(401, {"ok": False, "error": "Inicio de sesión requerido"})
            return False
        ACTIVE_USER.set(user_id)
        return True

    def require_shortcut_user(self):
        """Autoriza únicamente tokens dedicados de Atajos, nunca cookies web."""
        if not MULTIUSER:
            return True  # Compatibilidad con la versión antigua de un solo usuario.
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer wlsc_"):
            self.send_json(401, {"ok": False, "error": "Token de Atajo requerido"})
            return False
        token = authorization[7:].strip()
        if len(token) > 128 or len(token) < 35:
            self.send_json(401, {"ok": False, "error": "Token de Atajo inválido"})
            return False
        try:
            with auth_connect() as con:
                row = con.execute("""
                    SELECT user_id FROM public.shortcut_tokens
                    WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > NOW()
                """, (hash_token(token),)).fetchone()
                if row:
                    con.execute("""
                        UPDATE public.shortcut_tokens SET last_used_at = NOW()
                        WHERE token_hash = %s
                    """, (hash_token(token),))
            if not row:
                self.send_json(401, {"ok": False, "error": "Token de Atajo inválido o vencido"})
                return False
            ACTIVE_USER.set(str(row["user_id"]))
            return True
        except psycopg.Error as exc:
            print("ERROR validando token Atajo:", type(exc).__name__)
            self.send_json(503, {"ok": False, "error": "Autenticación de Atajos no disponible"})
            return False

    def shortcut_tokens_list(self):
        try:
            with auth_connect() as con:
                rows = con.execute("""
                    SELECT id, label, created_at, expires_at, last_used_at, revoked_at
                    FROM public.shortcut_tokens WHERE user_id = %s
                    ORDER BY created_at DESC LIMIT 30
                """, (ACTIVE_USER.get(),)).fetchall()
            return self.send_json(200, {"ok": True, "tokens": [
                {k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if v is not None else None)
                 for k, v in row.items()} for row in rows
            ]})
        except psycopg.Error as exc:
            print("ERROR listando tokens Atajo:", type(exc).__name__)
            return self.send_json(503, {"ok": False, "error": "Servicio de Atajos no disponible"})

    def shortcut_token_issue(self):
        if self.auth_token()[1] != "cookie":
            return self.send_json(403, {"ok": False, "error": "Inicia sesión en el navegador"})
        try:
            body = self.request_json()
            label = str(body.get("label", "Mi iPhone")).strip()
            if not label or len(label) > 60:
                return self.send_json(400, {"ok": False, "error": "Nombre de dispositivo inválido"})
            token = "wlsc_" + secrets.token_urlsafe(32)
            with auth_connect() as con:
                count = con.execute("""
                    SELECT COUNT(*) AS total FROM public.shortcut_tokens
                    WHERE user_id = %s AND revoked_at IS NULL AND expires_at > NOW()
                """, (ACTIVE_USER.get(),)).fetchone()["total"]
                if count >= 3:
                    return self.send_json(409, {"ok": False, "error": "Máximo de 3 dispositivos activos"})
                row = con.execute("""
                    INSERT INTO public.shortcut_tokens (user_id, label, token_hash, expires_at)
                    VALUES (%s, %s, %s, NOW() + INTERVAL '180 days')
                    RETURNING id, expires_at
                """, (ACTIVE_USER.get(), label, hash_token(token))).fetchone()
            return self.send_auth_json(201, {
                "ok": True, "id": str(row["id"]), "token": token,
                "expiresAt": row["expires_at"].isoformat(),
                "message": "Guarda el token: solo se muestra una vez"
            })
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"ok": False, "error": str(exc)})
        except psycopg.Error as exc:
            print("ERROR creando token Atajo:", type(exc).__name__)
            return self.send_json(503, {"ok": False, "error": "Servicio de Atajos no disponible"})

    def shortcut_token_revoke(self):
        if self.auth_token()[1] != "cookie":
            return self.send_json(403, {"ok": False, "error": "Inicia sesión en el navegador"})
        try:
            token_id = str(self.request_json().get("id", ""))
            uuid.UUID(token_id)
            with auth_connect() as con:
                row = con.execute("""
                    UPDATE public.shortcut_tokens SET revoked_at = NOW()
                    WHERE id = %s AND user_id = %s AND revoked_at IS NULL
                    RETURNING id
                """, (token_id, ACTIVE_USER.get())).fetchone()
            if row is None:
                return self.send_json(404, {"ok": False, "error": "Dispositivo no encontrado"})
            return self.send_json(200, {"ok": True, "message": "Acceso revocado"})
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return self.send_json(400, {"ok": False, "error": "Identificador inválido"})
        except psycopg.Error as exc:
            print("ERROR revocando token Atajo:", type(exc).__name__)
            return self.send_json(503, {"ok": False, "error": "Servicio de Atajos no disponible"})

    def send_auth_json(self, status, payload, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def set_login_cookie(self, token, max_age=604800):
        security = "; Secure" if COOKIE_SECURE else ""
        return (f"workoutlog_session={token}; HttpOnly; SameSite=Strict; "
                f"Path=/; Max-Age={max_age}{security}")

    def request_json(self):
        size = self.headers.get("Content-Length", "0")
        try:
            length = int(size)
        except ValueError:
            raise ValueError("Content-Length inválido")
        if not 0 < length <= 16384:
            raise ValueError("Tamaño de petición inválido")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Se esperaba un objeto JSON")
        return value

    def issue_session(self, user_id):
        token = secrets.token_urlsafe(32)
        with auth_connect() as con:
            con.execute("""
                INSERT INTO public.auth_sessions (user_id, token_hash, expires_at)
                VALUES (%s, %s, NOW() + INTERVAL '7 days')
            """, (user_id, hash_token(token)))
        return token

    def register_user(self):
        try:
            data = self.request_json()
            email = str(data.get("email", "")).strip().lower()
            password = data.get("password", "")
            if not valid_email(email) or not isinstance(password, str) or not 12 <= len(password) <= 128:
                return self.send_json(400, {"ok": False, "error": "Correo inválido o contraseña fuera de 12 a 128 caracteres"})
            password_hash = PASSWORD_HASHER.hash(password)
            with auth_connect() as con:
                row = con.execute("""
                    INSERT INTO public.users (email, password_hash)
                    VALUES (%s, %s) RETURNING id
                """, (email, password_hash)).fetchone()
            # No iniciar sesión automáticamente hasta validar la configuración.
            return self.send_json(201, {"ok": True, "message": "Cuenta registrada", "id": str(row["id"])})
        except psycopg.errors.UniqueViolation:
            return self.send_json(409, {"ok": False, "error": "No se pudo registrar ese correo"})
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            print("ERROR registro:", type(exc).__name__)
            return self.send_json(500, {"ok": False, "error": "No se pudo completar el registro"})

    def login_user(self):
        try:
            data = self.request_json()
            email = str(data.get("email", "")).strip().lower()
            password = data.get("password")
            if not valid_email(email) or not isinstance(password, str):
                return self.send_json(400, {"ok": False, "error": "Datos inválidos"})
            key = (self.client_address[0], email)
            now = datetime.now(timezone.utc).timestamp()
            with LOGIN_LOCK:
                count, until = LOGIN_FAILURES.get(key, (0, 0))
                if count >= 5 and now < until:
                    return self.send_json(429, {"ok": False, "error": "Demasiados intentos; inténtalo después"})
            with auth_connect() as con:
                row = con.execute("""
                    SELECT id, password_hash FROM public.users WHERE email = %s
                """, (email,)).fetchone()
            verified = False
            if row and row.get("password_hash"):
                try:
                    verified = PASSWORD_HASHER.verify(row["password_hash"], password)
                except (VerifyMismatchError, VerificationError, ValueError):
                    pass
            if not verified:
                with LOGIN_LOCK:
                    count, _ = LOGIN_FAILURES.get(key, (0, 0))
                    LOGIN_FAILURES[key] = (count + 1, now + 900)
                return self.send_json(401, {"ok": False, "error": "Credenciales incorrectas"})
            with LOGIN_LOCK:
                LOGIN_FAILURES.pop(key, None)
            token = self.issue_session(row["id"])
            # Bearer permite probar los endpoints sin interfaz; NO compartir el token.
            return self.send_auth_json(200, {"ok": True, "message": "Sesión iniciada", "token": token},
                                       cookie=self.set_login_cookie(token))
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            print("ERROR login:", type(exc).__name__)
            return self.send_json(500, {"ok": False, "error": "No fue posible iniciar sesión"})

    def logout_user(self):
        token, source = self.auth_token()
        if not token:
            return self.send_json(401, {"ok": False, "error": "Sesión requerida"})
        if source == "cookie":
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            parsed = urlsplit(origin)
            if not origin or parsed.netloc.lower() != host.lower() or (COOKIE_SECURE and parsed.scheme != "https"):
                return self.send_json(403, {"ok": False, "error": "Origen inválido"})
        try:
            with auth_connect() as con:
                con.execute("""
                    UPDATE public.auth_sessions SET revoked_at = NOW()
                    WHERE token_hash = %s AND revoked_at IS NULL
                """, (hash_token(token),))
            return self.send_auth_json(200, {"ok": True}, cookie=self.set_login_cookie("", 0))
        except Exception as exc:
            print("ERROR logout:", type(exc).__name__)
            return self.send_json(500, {"ok": False, "error": "No se pudo cerrar sesión"})

    def translate_path(self, path):
        clean = urlparse(path).path.lstrip("/") or "index.html"
        candidate = (PUBLIC_DIR / clean).resolve()
        root = PUBLIC_DIR.resolve()
        if not candidate.is_relative_to(root):
            return str(root / "__not_found__")
        return str(candidate)

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
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Longitud inválida") from exc
        if length < 1 or length > 1024 * 1024:
            raise ValueError("La petición excede el tamaño permitido")
        raw = self.rfile.read(length)

        return json.loads(
            raw.decode("utf-8")
        )

    def do_GET(self):
        ACTIVE_USER.set(None)
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

        if MULTIUSER and path == "/api/auth/me":
            if not self.require_user():
                return
            return self.send_json(200, {"ok": True, "userId": ACTIVE_USER.get()})

        if MULTIUSER and path == "/api/shortcut-tokens":
            if not self.require_user():
                return
            return self.shortcut_tokens_list()

        if path == "/api/sessions":
            if not self.require_user():
                return
            return self.get_sessions()

        if path == "/api/stats":
            if not self.require_user():
                return
            return self.get_stats()

        if path == "/":
            self.path = "/index.html"

        return super().do_GET()

    def do_POST(self):
        ACTIVE_USER.set(None)
        path = urlparse(self.path).path

        if MULTIUSER and path == "/api/auth/register":
            return self.register_user()
        if MULTIUSER and path == "/api/auth/login":
            return self.login_user()
        if MULTIUSER and path == "/api/auth/logout":
            return self.logout_user()

        if MULTIUSER and path == "/api/shortcut-tokens":
            if not self.require_user(mutation=True):
                return
            return self.shortcut_token_issue()

        if MULTIUSER and path == "/api/shortcut-tokens/revoke":
            if not self.require_user(mutation=True):
                return
            return self.shortcut_token_revoke()

        if path == "/api/sessions":
            if not self.require_user(mutation=True):
                return
            return self.create_session()

        if path == "/api/shortcut-workout":
            if not self.require_shortcut_user():
                return
            return self.create_shortcut_workout()

        return self.send_json(
            404,
            {
                "ok": False,
                "error": "Ruta no encontrada"
            }
        )

    def do_PUT(self):
        ACTIVE_USER.set(None)
        path = urlparse(self.path).path
        if path.startswith("/api/exercises/"):
            if not self.require_user(mutation=True):
                return
            exercise_id = path[len("/api/exercises/"):]
            if not exercise_id or "/" in exercise_id:
                return self.send_json(404, {"ok": False, "message": "Ejercicio no encontrado."})
            return self.update_exercise(exercise_id)
        return self.send_json(404, {"ok": False, "message": "Ruta no encontrada."})

    def update_exercise(self, exercise_id):
        try:
            uuid.UUID(exercise_id)
            body = self.read_json_body()
            if not isinstance(body, dict):
                raise ValueError("El cuerpo debe ser un objeto.")
            name = str(body.get("name", "")).strip()
            group = str(body.get("muscleGroup", "")).strip()
            if not name or len(name) > 150 or not group or len(group) > 60:
                raise ValueError("Nombre o grupo muscular inválido.")
            cardio = group.casefold() == "cardio"
            keys = ("durationMinutes", "distanceKm") if cardio else ("sets", "reps", "weightKg")
            values = {}
            for key in keys:
                raw = body.get(key, 0)
                val = float(raw)
                if not (0 <= val <= 1000000) or not math.isfinite(val):
                    raise ValueError("Las métricas deben ser números válidos no negativos.")
                if key in ("sets", "reps", "durationMinutes") and not val.is_integer():
                    raise ValueError("Series, repeticiones y duración deben ser enteros.")
                values[key] = int(val) if key in ("sets", "reps", "durationMinutes") else val
            if cardio:
                if values["durationMinutes"] <= 0 and values["distanceKm"] <= 0:
                    raise ValueError("Cardio necesita duración o distancia.")
            elif values["sets"] <= 0 or values["reps"] <= 0:
                raise ValueError("Fuerza necesita series y repeticiones mayores que cero.")
        except (ValueError, TypeError, OverflowError) as exc:
            return self.send_json(400, {"ok": False, "message": str(exc)})

        con = connect()
        p = sql_placeholder()
        try:
            original = con.execute(
                f"SELECT session_id FROM exercises WHERE id = {p}", (exercise_id,)
            ).fetchone()
            if original is None:
                return self.send_json(404, {"ok": False, "message": "Ejercicio no encontrado."})
            session_id = original["session_id"]
            fields = {
                "name": name, "muscle_group": group,
                "sets": 0 if cardio else values["sets"],
                "reps": 0 if cardio else values["reps"],
                "weight_kg": 0 if cardio else values["weightKg"],
                "duration_minutes": values["durationMinutes"] if cardio else 0,
                "distance_km": values["distanceKm"] if cardio else 0,
            }
            assignments = ", ".join(f"{col} = {p}" for col in fields)
            con.execute(
                f"UPDATE exercises SET {assignments} WHERE id = {p} AND session_id = {p}",
                tuple(fields.values()) + (exercise_id, session_id)
            )
            siblings = con.execute(
                f"SELECT muscle_group, duration_minutes FROM exercises WHERE session_id = {p}",
                (session_id,)
            ).fetchall()
            groups = {str(row["muscle_group"]).strip() for row in siblings if row["muscle_group"]}
            activity = next(iter(groups)) if len(groups) == 1 else "Mixto" if groups else "Entrenamiento"
            duration = sum(max(0, int(row["duration_minutes"] or 0)) for row in siblings)
            con.execute(
                f"UPDATE sessions SET activity = {p}, total_duration_minutes = {p} WHERE id = {p}",
                (activity, duration, session_id)
            )
            con.commit()
            return self.send_json(200, {"ok": True, "message": "Ejercicio actualizado.", "id": exercise_id})
        except Exception as exc:
            con.rollback()
            print("ERROR AL EDITAR:", repr(exc))
            return self.send_json(500, {"ok": False, "message": "No se pudieron guardar los cambios."})
        finally:
            con.close()

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

    def create_shortcut_workout(self):
        try:
            body = self.read_json_body()


        except Exception as exc:
            return self.send_json(
                400,
                {
                    "ok": False,
                    "message": "No pude leer los datos del iPhone.",
                    "detail": str(exc)
                }
            )

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

        groups = []
        for exercise in exercises:
            if isinstance(exercise, dict):
                group = str(exercise.get("muscleGroup", "")).strip()
                if group and group not in groups:
                    groups.append(group)

        if len(groups) == 1:
            activity = groups[0]
        elif len(groups) > 1:
            activity = "Mixto"
        else:
            activity = str(body.get("activity", "Entrenamiento")).strip() or "Entrenamiento"

        if duration == 0:
            duration = sum(
                max(
                    0,
                    safe_int(
                        exercise.get(
                            "durationMinutes",
                            0
                        )
                    )
                )
                for exercise in exercises
                if isinstance(exercise, dict)
            )

        return self.save_workout(
            activity=activity,
            exercises=exercises,
            duration=duration,
            intensity=intensity,
            notes=notes
        )

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
                    {", user_id" if MULTIUSER else ""}
                )
                VALUES (
                    {placeholder},
                    {placeholder},
                    {placeholder},
                    {placeholder},
                    {placeholder},
                    {placeholder}
                    {", " + placeholder if MULTIUSER else ""}
                )
                """,
                (
                    session_id,
                    activity,
                    started_at,
                    max(0, duration),
                    intensity,
                    notes
                ) + ((ACTIVE_USER.get(),) if MULTIUSER else ())
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

                exercise_distance = max(
                    0.0,
                    safe_float(
                        exercise.get(
                            "distanceKm",
                            0
                        )
                    )
                )

                muscle_group = str(
                    exercise.get(
                        "muscleGroup",
                        activity
                    )
                ).strip() or activity

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
                        duration_minutes,
                        distance_km,
                        muscle_group
                    )
                    VALUES (
                        {placeholder},
                        {placeholder},
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
                        exercise_duration,
                        exercise_distance,
                        muscle_group
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

            # Leer antes del commit: SET LOCAL de RLS vive solo durante la transacción.
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
            con.commit()

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
                    "message": "Error al guardar el entrenamiento."
                }
            )

        finally:
            con.close()

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
                "thisWeekSessions": week_sessions,
                "thisWeekMinutes": week_minutes,
                "thisMonthSessions": month_sessions,
                "thisMonthMinutes": month_minutes,
                "topActivity": top_activity
            }
        )


if __name__ == "__main__":
    if MULTIUSER:
        if not APP_DATABASE_URL or not AUTH_DATABASE_URL:
            raise SystemExit("Faltan URLs de los dos roles técnicos")
        if not APP_DATABASE_PASSWORD or not AUTH_DATABASE_PASSWORD:
            raise SystemExit("Faltan contraseñas de los dos roles técnicos")
        if not COOKIE_SECURE and os.environ.get("WORKOUTLOG_LOCAL_ONLY") != "1":
            raise SystemExit("Cookie HTTP insegura solo permitida en desarrollo local")
    init_db()

    server = ThreadingHTTPServer(
        (
            "127.0.0.1" if os.environ.get("WORKOUTLOG_LOCAL_ONLY") == "1" else "0.0.0.0",
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
