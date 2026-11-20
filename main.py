from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
import asyncio
import json

import database
import schemas
import cliente_usuarios as cu
from cliente_usuarios import (
    UsuarioNoEncontrado,
    ServicioUsuariosNoDisponible,
    ServicioUsuariosTimeout,
)

app = FastAPI(title="Servicio Materias - StudyMatch", version="1.5")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ============ CONFIGURACIÓN DE PERIODOS ============
PERIODOS_HABILITADOS = ["2026-2"]
TODOS_LOS_PERIODOS = ["2025-2", "2026-1", "2026-2", "2027-1"]
PERIODO_ACTIVO = "2026-2"


# ============ MANEJO DE ERRORES (CONTRATO) ============
def err(status: int, code: str, msg: str):
    raise HTTPException(status_code=status, detail={"error": code, "mensaje": msg})


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    payload = {"codigo": exc.status_code}
    if isinstance(exc.detail, dict):
        payload.update(exc.detail)
    else:
        payload.update({"error": "ERROR", "mensaje": str(exc.detail)})
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(RequestValidationError)
async def validation_exc_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={
            "codigo": 400,
            "error": "DATOS_INVALIDOS",
            "mensaje": "La solicitud contiene datos inválidos o incompletos",
        },
    )


def _validar_usuario(usuario_id: int) -> dict:
    return asyncio.run(cu.consultar_usuario(usuario_id))


def _traducir_fallo_usuarios(e: Exception):
    if isinstance(e, UsuarioNoEncontrado):
        err(404, "USUARIO_NO_ENCONTRADO", str(e))
    if isinstance(e, ServicioUsuariosTimeout):
        err(503, "USUARIOS_TIMEOUT", str(e))
    if isinstance(e, ServicioUsuariosNoDisponible):
        err(503, "USUARIOS_NO_DISPONIBLE", str(e))
    raise e


def _usuario_activo_id(request: Request) -> int:
    # Si hay sesión/cookie o cabecera de usuario, se puede resolver aquí; por defecto 1 en BD
    uid_header = request.headers.get("X-Usuario-Id")
    if uid_header and uid_header.isdigit() and int(uid_header) > 0:
        return int(uid_header)
    return 1


def _catalogo(db: Session) -> list[dict]:
    rows = db.execute(
        text("""
            SELECT grupo_id, materia_id, nombre, carrera, dia, hora, salon
            FROM materias
            ORDER BY carrera, materia_id, grupo_id
        """)
    ).fetchall()
    return [
        {
            "grupo_id": x[0],
            "materia_id": x[1],
            "nombre": x[2],
            "carrera": x[3] or "General",
            "dia": x[4],
            "hora": x[5],
            "salon": x[6],
        }
        for x in rows
    ]


# ============ API DEL CONTRATO (/api/v1) ============
@app.get("/api/v1/materias", tags=["Materias"])
def listar_materias(db: Session = Depends(database.get_db)):
    try:
        rows = db.execute(
            text("SELECT DISTINCT materia_id, nombre FROM materias ORDER BY materia_id")
        ).fetchall()
        return [{"materia_id": r[0], "nombre": r[1]} for r in rows]
    except SQLAlchemyError:
        err(503, "BD_NO_DISPONIBLE", "Error al consultar el catálogo de materias")


@app.get("/api/v1/materias/{materia_id}", tags=["Materias"])
def consultar_materia(materia_id: int, db: Session = Depends(database.get_db)):
    if materia_id <= 0:
        err(400, "ID_INVALIDO", "materia_id debe ser un entero positivo")
    try:
        r = db.execute(
            text("SELECT materia_id, nombre FROM materias WHERE materia_id = :id LIMIT 1"),
            {"id": materia_id},
        ).fetchone()
    except SQLAlchemyError:
        err(503, "BD_NO_DISPONIBLE", "Error al consultar la materia")
    if not r:
        err(404, "MATERIA_NO_ENCONTRADA", "La materia solicitada no existe")
    return {"materia_id": r[0], "nombre": r[1]}


@app.get("/api/v1/usuarios/{usuario_id}/materias", tags=["Usuario-Materia"])
def obtener_materias_usuario(usuario_id: int, db: Session = Depends(database.get_db)):
    if usuario_id <= 0:
        err(400, "ID_INVALIDO", "usuario_id debe ser un entero positivo")
    try:
        _validar_usuario(usuario_id)
    except Exception as e:
        _traducir_fallo_usuarios(e)

    try:
        rows = db.execute(
            text("""
                SELECT DISTINCT m.materia_id, m.nombre
                FROM usuariomaterias um
                JOIN materias m ON um.materia_id = m.grupo_id
                WHERE um.usuario_id = :uid
                ORDER BY m.materia_id
            """),
            {"uid": usuario_id},
        ).fetchall()
        return {
            "usuario_id": usuario_id,
            "materias": [{"materia_id": r[0], "nombre": r[1]} for r in rows],
        }
    except SQLAlchemyError:
        err(503, "BD_NO_DISPONIBLE", "Error al consultar las materias del usuario")


@app.post("/api/v1/usuarios/{usuario_id}/materias", status_code=201, tags=["Usuario-Materia"])
def asociar_materia(
    usuario_id: int,
    data: schemas.AsociarMateria,
    db: Session = Depends(database.get_db),
):
    if usuario_id <= 0 or data.materia_id <= 0:
        err(400, "ID_INVALIDO", "Los IDs deben ser enteros positivos")

    try:
        _validar_usuario(usuario_id)
    except Exception as e:
        _traducir_fallo_usuarios(e)

    try:
        m = db.execute(
            text("SELECT grupo_id, materia_id FROM materias WHERE grupo_id = :gid OR materia_id = :mid LIMIT 1"),
            {"gid": data.materia_id, "mid": data.materia_id},
        ).fetchone()
        if not m:
            err(404, "MATERIA_NO_ENCONTRADA", "La materia o grupo solicitado no existe")

        grupo_objetivo = m[0]
        db.execute(
            text("""
                INSERT INTO usuariomaterias (usuario_id, materia_id, periodo)
                VALUES (:u, :m, :p)
            """),
            {"u": usuario_id, "m": grupo_objetivo, "p": PERIODO_ACTIVO},
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        err(400, "ASOCIACION_DUPLICADA", "La materia ya está asociada a este usuario")
    except SQLAlchemyError:
        db.rollback()
        err(503, "BD_NO_DISPONIBLE", "No se pudo persistir la asociación en la base de datos")
    except Exception:
        db.rollback()
        err(500, "ERROR_INTERNO", "Error interno al asociar la materia")

    return {
        "usuario_id": usuario_id,
        "materia_id": data.materia_id,
        "mensaje": "Materia asociada correctamente",
    }


@app.delete("/api/v1/usuarios/{usuario_id}/materias/{materia_id}", tags=["Usuario-Materia"])
def retirar_asociacion(
    usuario_id: int,
    materia_id: int,
    db: Session = Depends(database.get_db),
):
    if usuario_id <= 0 or materia_id <= 0:
        err(400, "ID_INVALIDO", "Los IDs deben ser enteros positivos")
    try:
        res = db.execute(
            text("""
                DELETE FROM usuariomaterias
                WHERE usuario_id = :u AND materia_id = :m
            """),
            {"u": usuario_id, "m": materia_id},
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        err(503, "BD_NO_DISPONIBLE", "Error al eliminar la asociación")

    if res.rowcount == 0:
        err(404, "ASOCIACION_NO_ENCONTRADA", "No existe esa asociación para eliminar")
    return {"mensaje": "Asociación eliminada correctamente"}


# ============ VISTAS WEB ============
@app.get("/", include_in_schema=False)
def raiz():
    return RedirectResponse("/tablero", status_code=303)


@app.get("/tablero", response_class=HTMLResponse, include_in_schema=False)
def tablero(request: Request, db: Session = Depends(database.get_db)):
    uid = _usuario_activo_id(request)
    usuario = cu.obtener_usuario_bd(uid) or {"usuario_id": uid, "nombre": "Usuario"}

    rows = db.execute(
        text("""
            SELECT m.grupo_id, m.materia_id, m.nombre, m.carrera, m.dia, m.hora, m.salon, um.periodo
            FROM usuariomaterias um
            JOIN materias m ON um.materia_id = m.grupo_id
            WHERE um.usuario_id = :u
            ORDER BY m.materia_id
        """),
        {"u": uid},
    ).fetchall()

    materias_inscritas = [
        {
            "grupo_id": x[0],
            "materia_id": x[1],
            "nombre": x[2],
            "carrera": x[3],
            "dia": x[4],
            "hora": x[5],
            "salon": x[6],
            "periodo": x[7],
        }
        for x in rows
    ]

    return templates.TemplateResponse(
        request=request,
        name="tablero.html",
        context={"usuario": usuario, "materias_inscritas": materias_inscritas},
    )


@app.get("/catalogo", response_class=HTMLResponse, include_in_schema=False)
def catalogo(request: Request, msg: str = None, ok: str = None, db: Session = Depends(database.get_db)):
    uid = _usuario_activo_id(request)
    usuario = cu.obtener_usuario_bd(uid) or {"usuario_id": uid, "nombre": "Usuario"}
    cat = _catalogo(db)

    agrupado: dict[str, list[dict]] = {}
    for m in cat:
        agrupado.setdefault(m["carrera"], []).append(m)

    return templates.TemplateResponse(
        request=request,
        name="catalogo.html",
        context={
            "usuario": usuario,
            "catalogo": agrupado,
            "periodo_activo": PERIODO_ACTIVO,
            "msg": msg,
            "ok": ok == "1" if ok else False,
        },
    )


@app.post("/web/agregar_materia", include_in_schema=False)
def web_agregar_materia(
    nombre: str = Form(...),
    carrera: str = Form(...),
    dia: str = Form(...),
    hora: str = Form(...),
    salon: str = Form(...),
    db: Session = Depends(database.get_db),
):
    conflicto = db.execute(
        text("""
            SELECT grupo_id, nombre, dia, hora, salon
            FROM materias
            WHERE dia = :d AND hora = :h AND salon = :s
            LIMIT 1
        """),
        {"d": dia, "h": hora, "s": salon},
    ).fetchone()

    if conflicto:
        return RedirectResponse(
            f"/catalogo?msg=Error:+conflicto+de+horario+en+grupo+G{conflicto[0]}+({conflicto[2]}+{conflicto[3]}+{conflicto[4]})&ok=0",
            status_code=303,
        )

    materia_existente = db.execute(
        text("SELECT materia_id FROM materias WHERE nombre = :n LIMIT 1"),
        {"n": nombre},
    ).fetchone()

    if materia_existente:
        nuevo_materia_id = materia_existente[0]
    else:
        max_id = db.execute(
            text("SELECT COALESCE(MAX(materia_id), 0) FROM materias")
        ).fetchone()[0]
        nuevo_materia_id = max_id + 1

    try:
        result = db.execute(
            text("""
                INSERT INTO materias (materia_id, nombre, carrera, dia, hora, salon)
                VALUES (:id, :n, :c, :d, :h, :s)
                RETURNING grupo_id
            """),
            {
                "id": nuevo_materia_id,
                "n": nombre,
                "c": carrera,
                "d": dia,
                "h": hora,
                "s": salon,
            },
        )
        nuevo_grupo_id = result.fetchone()[0]
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return RedirectResponse("/catalogo?msg=Error+al+guardar+en+base+de+datos&ok=0", status_code=303)

    return RedirectResponse(
        f"/catalogo?msg=Grupo+G{nuevo_grupo_id}+de+'{nombre}'+agregado+exitosamente&ok=1",
        status_code=303,
    )


@app.post("/web/actualizar_materia", include_in_schema=False)
def web_actualizar_materia(
    materia_id: int = Form(...),
    nombre: str = Form(...),
    carrera: str = Form(...),
    dia: str = Form(...),
    hora: str = Form(...),
    salon: str = Form(...),
    db: Session = Depends(database.get_db),
):
    try:
        db.execute(
            text("""
                UPDATE materias
                SET nombre = :n, carrera = :c, dia = :d, hora = :h, salon = :s
                WHERE materia_id = :id
            """),
            {"n": nombre, "c": carrera, "d": dia, "h": hora, "s": salon, "id": materia_id},
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return RedirectResponse("/catalogo?msg=Error+al+actualizar+en+BD&ok=0", status_code=303)

    return RedirectResponse(f"/catalogo?msg=Materia+actualizada+(ID+{materia_id})&ok=1", status_code=303)


@app.post("/web/eliminar_materia_catalogo", include_in_schema=False)
def web_eliminar_materia_catalogo(
    materia_id: int = Form(...),
    db: Session = Depends(database.get_db),
):
    grupo_id = materia_id
    inscritos = db.execute(
        text("SELECT COUNT(*) FROM usuariomaterias WHERE materia_id = :gid"),
        {"gid": grupo_id},
    ).fetchone()[0]

    if inscritos > 0:
        return RedirectResponse(
            f"/catalogo?msg=Error:+no+se+puede+eliminar+G{grupo_id}+con+{inscritos}+inscrito(s)&ok=0",
            status_code=303,
        )

    try:
        db.execute(text("DELETE FROM materias WHERE grupo_id = :gid"), {"gid": grupo_id})
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return RedirectResponse("/catalogo?msg=Error+al+eliminar+de+BD&ok=0", status_code=303)

    return RedirectResponse(f"/catalogo?msg=Grupo+G{grupo_id}+eliminado&ok=1", status_code=303)


@app.get("/inscripcion", response_class=HTMLResponse, include_in_schema=False)
def pagina_inscripcion(request: Request, db: Session = Depends(database.get_db)):
    uid = _usuario_activo_id(request)
    usuario = cu.obtener_usuario_bd(uid) or {"usuario_id": uid, "nombre": "Usuario", "carrera": "General"}
    cat = _catalogo(db)
    estudiantes = cu.listar_estudiantes_bd()

    return templates.TemplateResponse(
        request=request,
        name="inscripcion.html",
        context={
            "usuario": usuario,
            "periodos": PERIODOS_HABILITADOS,
            "periodo_activo": PERIODO_ACTIVO,
            "catalogo": cat,
            "catalogo_json": json.dumps(cat),
            "msg": None,
            "ok": False,
            "carrera_usuario": usuario.get("carrera") or "General",
            "estudiantes": estudiantes,
        },
    )


@app.post("/web/inscripcion", include_in_schema=False)
def web_inscripcion(
    request: Request,
    periodo: str = Form(...),
    grupo_id: int = Form(...),
    usuario_destino: int = Form(None),
    db: Session = Depends(database.get_db),
):
    uid_activo = _usuario_activo_id(request)
    uid = usuario_destino if usuario_destino else uid_activo
    usuario = cu.obtener_usuario_bd(uid)
    cat = _catalogo(db)
    estudiantes = cu.listar_estudiantes_bd()

    ctx = {
        "usuario": usuario or {"usuario_id": uid, "nombre": "Usuario"},
        "periodos": PERIODOS_HABILITADOS,
        "periodo_activo": PERIODO_ACTIVO,
        "catalogo": cat,
        "catalogo_json": json.dumps(cat),
        "carrera_usuario": (usuario.get("carrera") if usuario else None) or "General",
        "estudiantes": estudiantes,
    }

    def render(msg: str, ok: bool):
        ctx.update({"msg": msg, "ok": ok})
        return templates.TemplateResponse(request=request, name="inscripcion.html", context=ctx)

    if not usuario:
        return render("Error 404: el usuario no existe en la base de datos.", False)

    if periodo not in PERIODOS_HABILITADOS:
        return render(f"Error 400: El periodo '{periodo}' no está habilitado.", False)

    g = db.execute(
        text("SELECT grupo_id, materia_id, nombre, carrera, dia, hora, salon FROM materias WHERE grupo_id = :gid"),
        {"gid": grupo_id},
    ).fetchone()
    if not g:
        return render("Error 404: el grupo no existe en el catálogo.", False)

    ya_inscrito = db.execute(
        text("""
            SELECT COUNT(*)
            FROM usuariomaterias um
            JOIN materias m ON um.materia_id = m.grupo_id
            WHERE um.usuario_id = :u AND m.materia_id = :mid
        """),
        {"u": uid, "mid": g[1]},
    ).fetchone()[0]

    if ya_inscrito > 0:
        return render(f"Error 400: ya estás inscrito en '{g[2]}'.", False)

    try:
        db.execute(
            text("INSERT INTO usuariomaterias (usuario_id, materia_id, periodo) VALUES (:u, :m, :p)"),
            {"u": uid, "m": grupo_id, "p": periodo},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        return render("Error 400: la inscripción ya existe.", False)
    except SQLAlchemyError:
        db.rollback()
        return render("Error 503: error al registrar la inscripción en BD.", False)

    return render(
        f"Inscripción exitosa: {g[2]} ({g[4]} {g[5]} - {g[6]}) en el periodo {periodo}.",
        True,
    )


@app.get("/horario", response_class=HTMLResponse, include_in_schema=False)
def horario(
    request: Request,
    periodo: str = None,
    ok: str = None,
    error: str = None,
    db: Session = Depends(database.get_db),
):
    uid = _usuario_activo_id(request)
    usuario = cu.obtener_usuario_bd(uid) or {"usuario_id": uid, "nombre": "Usuario"}
    periodo_consulta = periodo or PERIODO_ACTIVO
    error_msg = error
    clases = []

    if periodo_consulta not in PERIODOS_HABILITADOS:
        error_msg = f"El periodo '{periodo_consulta}' no está habilitado. Activo: {PERIODO_ACTIVO}."
    else:
        rows = db.execute(
            text("""
                SELECT m.grupo_id, m.materia_id, m.nombre, m.carrera, m.dia, m.hora, m.salon, um.periodo, u.nombre
                FROM usuariomaterias um
                JOIN materias m ON um.materia_id = m.grupo_id
                LEFT JOIN usuarios u ON um.usuario_id = u.usuario_id
                WHERE um.usuario_id = :u AND um.periodo = :p
                ORDER BY m.materia_id
            """),
            {"u": uid, "p": periodo_consulta},
        ).fetchall()

        for x in rows:
            clases.append(
                {
                    "grupo_id": x[0],
                    "materia_id": x[1],
                    "nombre": x[2],
                    "carrera": x[3],
                    "dia": x[4],
                    "hora": x[5],
                    "salon": x[6],
                    "periodo": x[7],
                    "estudiante_nombre": x[8] or "",
                }
            )

    return templates.TemplateResponse(
        request=request,
        name="horario.html",
        context={
            "usuario": usuario,
            "periodo": periodo_consulta,
            "clases": clases,
            "ok": ok,
            "error": error_msg,
            "periodos_habilitados": PERIODOS_HABILITADOS,
            "todos_los_periodos": TODOS_LOS_PERIODOS,
        },
    )


@app.post("/web/eliminar_materia", include_in_schema=False)
def web_eliminar_materia(
    request: Request,
    materia_id: int = Form(...),
    db: Session = Depends(database.get_db),
):
    uid = _usuario_activo_id(request)
    if uid <= 0 or materia_id <= 0:
        return RedirectResponse("/horario?error=ID+invalido", status_code=303)
    try:
        res = db.execute(
            text("DELETE FROM usuariomaterias WHERE usuario_id = :u AND materia_id = :m"),
            {"u": uid, "m": materia_id},
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return RedirectResponse("/horario?error=Error+de+BD", status_code=303)

    if res.rowcount == 0:
        return RedirectResponse("/horario?error=No+encontrada", status_code=303)
    return RedirectResponse("/horario?ok=Eliminada", status_code=303)


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def panel_admin(request: Request, msg: str = None, ok: str = None, db: Session = Depends(database.get_db)):
    uid = _usuario_activo_id(request)
    usuario = cu.obtener_usuario_bd(uid) or {"usuario_id": uid, "nombre": "Admin"}
    materias = _catalogo(db)

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "usuario": usuario,
            "materias": materias,
            "periodos_habilitados": PERIODOS_HABILITADOS,
            "todos_los_periodos": TODOS_LOS_PERIODOS,
            "periodo_activo": PERIODO_ACTIVO,
            "msg": msg,
            "ok": ok == "1" if ok else False,
        },
    )


@app.post("/admin/agregar_periodo", include_in_schema=False)
def admin_agregar_periodo(nuevo_periodo: str = Form(...)):
    if nuevo_periodo not in TODOS_LOS_PERIODOS:
        TODOS_LOS_PERIODOS.append(nuevo_periodo)
        return RedirectResponse(f"/admin?msg=Periodo+{nuevo_periodo}+agregado&ok=1", status_code=303)
    return RedirectResponse("/admin?msg=Error:+el+periodo+ya+existe&ok=0", status_code=303)


@app.post("/admin/habilitar_periodo", include_in_schema=False)
def admin_habilitar_periodo(periodo: str = Form(...)):
    if periodo not in PERIODOS_HABILITADOS:
        PERIODOS_HABILITADOS.append(periodo)
    return RedirectResponse("/admin?msg=Periodo+habilitado&ok=1", status_code=303)


@app.post("/admin/inhabilitar_periodo", include_in_schema=False)
def admin_inhabilitar_periodo(periodo: str = Form(...)):
    if periodo in PERIODOS_HABILITADOS and periodo != PERIODO_ACTIVO:
        PERIODOS_HABILITADOS.remove(periodo)
    return RedirectResponse("/admin?msg=Periodo+inhabilitado&ok=1", status_code=303)


@app.post("/admin/actualizar_inscripcion", include_in_schema=False)
def admin_actualizar_inscripcion(
    usuario_id: int = Form(...),
    materia_id: int = Form(...),
    dia: str = Form(...),
    hora: str = Form(...),
    salon: str = Form(...),
    periodo: str = Form(...),
    db: Session = Depends(database.get_db),
):
    try:
        db.execute(
            text("UPDATE materias SET dia = :d, hora = :h, salon = :s WHERE materia_id = :m"),
            {"d": dia, "h": hora, "s": salon, "m": materia_id},
        )
        db.execute(
            text("UPDATE usuariomaterias SET periodo = :p WHERE usuario_id = :u AND materia_id = :m"),
            {"p": periodo, "u": usuario_id, "m": materia_id},
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return RedirectResponse("/admin?msg=Error+al+actualizar+inscripcion&ok=0", status_code=303)

    return RedirectResponse("/admin?msg=Inscripcion+actualizada&ok=1", status_code=303)


@app.post("/admin/eliminar_inscripcion", include_in_schema=False)
def admin_eliminar_inscripcion(
    usuario_id: int = Form(...),
    materia_id: int = Form(...),
    db: Session = Depends(database.get_db),
):
    try:
        db.execute(
            text("DELETE FROM usuariomaterias WHERE usuario_id = :u AND materia_id = :m"),
            {"u": usuario_id, "m": materia_id},
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return RedirectResponse("/admin?msg=Error+al+eliminar+inscripcion&ok=0", status_code=303)

    return RedirectResponse("/admin?msg=Inscripcion+eliminada&ok=1", status_code=303)