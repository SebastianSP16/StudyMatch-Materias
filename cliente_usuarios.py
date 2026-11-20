import os
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
import httpx
import database


class UsuarioNoEncontrado(Exception):
    pass


class ServicioUsuariosNoDisponible(Exception):
    pass


class ServicioUsuariosTimeout(Exception):
    pass


USUARIOS_SERVICE_URL = os.getenv("USUARIOS_SERVICE_URL", "").rstrip("/")
USUARIOS_TIMEOUT_SEG = float(os.getenv("USUARIOS_TIMEOUT_SEG", "3.0"))


def obtener_usuario_bd(usuario_id: int) -> dict | None:
    sql = text("""
        SELECT usuario_id, nombre, apellido, correo, carrera, semestre, foto, estado
        FROM usuarios
        WHERE usuario_id = :uid
    """)
    with database.SessionLocal() as db:
        row = db.execute(sql, {"uid": usuario_id}).mappings().fetchone()
        return dict(row) if row else None


def listar_estudiantes_bd() -> list[dict]:
    sql = text("""
        SELECT usuario_id, nombre, apellido, correo, carrera, semestre, estado
        FROM usuarios
        ORDER BY usuario_id
    """)
    with database.SessionLocal() as db:
        return [dict(r._mapping) for r in db.execute(sql).fetchall()]


async def consultar_usuario(usuario_id: int) -> dict:
    """Consulta un usuario por ID.
    Si USUARIOS_SERVICE_URL está configurado, consume GET /api/v1/usuarios/{usuario_id};
    de lo contrario consulta directamente la tabla `usuarios` en PostgreSQL.
    """
    if USUARIOS_SERVICE_URL:
        url = f"{USUARIOS_SERVICE_URL}/api/v1/usuarios/{usuario_id}"
        try:
            async with httpx.AsyncClient(timeout=USUARIOS_TIMEOUT_SEG) as client:
                resp = await client.get(url)
            if resp.status_code == 404:
                raise UsuarioNoEncontrado(f"Usuario {usuario_id} no existe en Servicio Usuarios")
            if resp.status_code >= 500:
                raise ServicioUsuariosNoDisponible(
                    f"Servicio Usuarios respondió con estado {resp.status_code}"
                )
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException as exc:
            raise ServicioUsuariosTimeout(
                "Timeout al consultar Servicio Usuarios"
            ) from exc
        except httpx.RequestError as exc:
            raise ServicioUsuariosNoDisponible(
                f"No se pudo conectar con Servicio Usuarios: {exc}"
            ) from exc

    try:
        usuario = obtener_usuario_bd(usuario_id)
        if not usuario:
            raise UsuarioNoEncontrado(f"Usuario {usuario_id} no existe en la base de datos")
        return usuario
    except UsuarioNoEncontrado:
        raise
    except SQLAlchemyError as exc:
        raise ServicioUsuariosNoDisponible(
            f"Error de base de datos al consultar usuarios: {exc}"
        ) from exc