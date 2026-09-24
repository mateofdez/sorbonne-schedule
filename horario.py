"""
Horario Sorbonne (Máster + Français) -> horario.ics + email semanal.

Fuente DLP: servidor CalDAV del Máster de Informática
  (el mismo que usa https://cal.ufr-info-p6.jussieu.fr/master/ al marcar M1_STL).
  Incluye cursos, TD y TME (prácticas del viernes).

Fuente Géométrie: https://planning.upmc.fr/oldEmpSelect/jussieu/<PUBLIC>/<N>/
  - N = 1 es la semana actual, N = 2 la siguiente, etc.
  - Cada página indica su semana: "Planning du 21/09/2026 au 26/09/2026".
  - Rejilla: columna 0 = etiqueta de grupo, columna 2 = 08:00,
    y cada columna (sumando colspan) = 15 minutos.
  - Cada clase lleva title="réservation 695176 du 15/09 au 20/10",
    que usamos para rellenar las semanas que no podemos leer directamente.
"""

import os
import re
import sys
import time
import uuid
import smtplib
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import xml.etree.ElementTree as ET

import requests
import recurring_ical_events
from bs4 import BeautifulSoup
from icalendar import Calendar

# ============================================================
# CONFIGURACIÓN
# ============================================================

TZ = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")

# ---- DLP desde CalDAV (calendario M1_STL) ----
CALDAV_URL = "https://cal.ufr-info-p6.jussieu.fr:443/caldav.php/STL/M1_STL/"
# Cuenta de invitado de solo lectura, publicada en el config.js de la web.
CALDAV_USER = os.environ.get("CALDAV_USER", "student.master")
CALDAV_PASS = os.environ.get("CALDAV_PASS", "guest")
DLP_CODIGO = "UM4IN501"
# Grupos de DLP que te interesan, p. ej. {"TD1", "TME1"}.
# None = todos (el log imprime la lista de grupos que existen).
DLP_GRUPOS = {"TD1", "TME1"}
CURSO_INICIO = date(2026, 8, 31)
CURSO_FIN = date(2027, 1, 31)

# ---- Planning UPMC: qué asignaturas buscar en cada público ----
MASTER = {
    "M1.MATH": {"UM4MA322"},       # Géométrie Différentielle
}
# Si CalDAV falla, DLP se lee de aquí como plan B (solo trae los martes).
PLAN_B_DLP = {"M1.INFO.STL": {"UM4IN501"}}

NOMBRES = {
    "UM4IN501": "DLP",
    "UM4MA322": "Géométrie Différentielle",
    "FRANCES": "Français",
}

# Cuántas semanas hacia delante intentamos leer (N = 1..SEMANAS_A_LEER).
SEMANAS_A_LEER = 16

# Français: martes 18:00-20:00, del 15/09 al 15/12
FRANCES_INICIO = date(2026, 9, 15)
FRANCES_FIN = date(2026, 12, 15)
FRANCES_SIN_CLASE = {date(2026, 10, 27)}   # vacaciones 24/10-01/11
FRANCES_SALA = "43.53.127"

URL_MASTER = "https://planning.upmc.fr/oldEmpSelect/jussieu/{public}/{n}/"

DIAS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

# Colores de la leyenda de la web -> tipo de sesión.
TIPOS = {"#ffcc99": "Cours", "#ccccff": "TD", "#ffff99": "TP"}

COL_08H00 = 2      # índice de columna que corresponde a las 08:00
MIN_POR_COL = 15

FECHA_SEMANA_RE = re.compile(r"Planning du (\d{2})/(\d{2})/(\d{4})")
RESERVA_RE = re.compile(r"du (\d{2})/(\d{2}) au (\d{2})/(\d{2})")
CODIGO_RE = re.compile(r"(UM\w+)\s*\(([^)]*)\)")


# ============================================================
# LECTURA DEL PLANNING DE MÁSTER
# ============================================================

def descargar(public, n):
    url = URL_MASTER.format(public=public, n=n)
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")


def fecha_curso(dd, mm, referencia):
    """dd/mm sin año -> fecha del curso académico de 'referencia'."""
    anio_inicio = referencia.year if referencia.month >= 8 else referencia.year - 1
    anio = anio_inicio if mm >= 8 else anio_inicio + 1
    return date(anio, mm, dd)


def parsear_pagina(html, codigos):
    """Devuelve (lunes_de_la_semana, [clases]) de una página del planning."""
    m = FECHA_SEMANA_RE.search(html)
    if not m:
        return None, []
    d, mth, y = map(int, m.groups())
    primer_dia = date(y, mth, d)
    lunes = primer_dia - timedelta(days=primer_dia.weekday())

    soup = BeautifulSoup(html, "html.parser")
    tabla = soup.find("table", class_="gplanning")
    if tabla is None:
        return lunes, []

    clases = []
    dia = None
    for tr in tabla.find_all("tr", recursive=False):
        celdas = tr.find_all("td", recursive=False)

        # Fila de día: una sola celda con class="jour"
        if len(celdas) == 1 and "jour" in (celdas[0].get("class") or []):
            texto = celdas[0].get_text(strip=True)
            dia = texto if texto in DIAS else None
            continue
        if dia is None:
            continue

        col = 0
        for celda in celdas:
            span = int(celda.get("colspan", 1))
            texto = " ".join(celda.get_text(" ", strip=True).split())
            m_cod = CODIGO_RE.search(texto)
            if m_cod and m_cod.group(1) in codigos:
                ini = 8 * 60 + (col - COL_08H00) * MIN_POR_COL
                fin = ini + span * MIN_POR_COL
                sala = m_cod.group(2).strip()
                m_res = RESERVA_RE.search(celda.get("title", ""))
                clases.append({
                    "ue": m_cod.group(1),
                    "fecha": lunes + timedelta(days=DIAS.index(dia)),
                    "ini": ini,
                    "fin": fin,
                    "sala": sala,
                    "tipo": TIPOS.get((celda.get("bgcolor") or "").lower(), ""),
                    "anulada": "ANNUL" in sala.upper(),
                    "rango": m_res.groups() if m_res else None,
                })
            col += span
    return lunes, clases


def obtener_master(publicos):
    """
    Lee las semanas reales disponibles y, para las que no se pueden leer
    (semanas pasadas o si la web ignora el número), extrapola con el rango
    'du .. au ..' de cada reserva.
    """
    leidas = {}        # lunes -> [clases reales]
    patrones = []      # clases con su rango, para extrapolar

    for public, codigos in publicos.items():
        vistos = set()
        for n in range(1, SEMANAS_A_LEER + 1):
            try:
                html = descargar(public, n)
            except requests.RequestException as e:
                print(f"[!] {public} N={n}: {e}")
                break
            lunes, clases = parsear_pagina(html, codigos)
            if lunes is None:
                print(f"[!] {public} N={n}: no encuentro la fecha de la semana")
                break
            if lunes in vistos:
                print(f"[i] {public} N={n}: repite la semana {lunes}, paro aquí")
                break
            vistos.add(lunes)
            leidas.setdefault(lunes, []).extend(clases)
            patrones.extend(clases)
            print(f"[+] {public} N={n}: semana del {lunes:%d/%m} -> {len(clases)} clases")
            time.sleep(1)

    # Semanas leídas de verdad: mandan ellas.
    eventos = {}
    for clases in leidas.values():
        for c in clases:
            eventos[(c["fecha"], c["ini"], c["ue"])] = c

    # Resto de semanas: repetición semanal dentro del rango de la reserva.
    for c in patrones:
        if not c["rango"] or c["anulada"]:
            continue
        d1, m1, d2, m2 = map(int, c["rango"])
        inicio = fecha_curso(d1, m1, c["fecha"])
        fin = fecha_curso(d2, m2, c["fecha"])
        f = inicio
        while f <= fin:
            lunes = f - timedelta(days=f.weekday())
            if lunes not in leidas:
                clave = (f, c["ini"], c["ue"])
                if clave not in eventos:
                    eventos[clave] = {**c, "fecha": f, "anulada": False}
            f += timedelta(weeks=1)

    return list(eventos.values())



# ============================================================
# DLP DESDE CALDAV
# ============================================================

REPORT_XML = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="{ini}" end="{fin}"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""

TIPO_CALDAV = {"CS": "Cours", "CM": "Cours", "TD": "TD", "TME": "TME", "TP": "TP"}
GRUPO_RE = re.compile(r"-(CS|CM|CTD|TD|TME|TP|EX|EXAM)(\d*)\s*$", re.IGNORECASE)
RESERVA_LUGAR_RE = re.compile(r"\s*\(\s*r[ée]servation[^)]*\)", re.IGNORECASE)


def descargar_caldav(ini, fin):
    cuerpo = REPORT_XML.format(ini=f"{ini:%Y%m%d}T000000Z", fin=f"{fin:%Y%m%d}T000000Z")
    r = requests.request(
        "REPORT", CALDAV_URL,
        auth=(CALDAV_USER, CALDAV_PASS),
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=cuerpo.encode("utf-8"),
        timeout=60,
    )
    r.raise_for_status()
    raiz = ET.fromstring(r.content)
    return [n.text for n in raiz.iter("{urn:ietf:params:xml:ns:caldav}calendar-data") if n.text]


def obtener_dlp():
    try:
        bloques = descargar_caldav(CURSO_INICIO, CURSO_FIN)
    except (requests.RequestException, ET.ParseError) as e:
        print(f"[!] CalDAV no disponible: {e}")
        return []

    ini_rango = datetime.combine(CURSO_INICIO, datetime.min.time(), TZ)
    fin_rango = datetime.combine(CURSO_FIN, datetime.min.time(), TZ)
    eventos, grupos_vistos = {}, set()

    for bloque in bloques:
        cal = Calendar.from_ical(bloque)
        for ev in recurring_ical_events.of(cal).between(ini_rango, fin_rango):
            resumen = str(ev.get("SUMMARY", "")).strip()
            if DLP_CODIGO not in resumen.upper() and "DLP" not in resumen.upper():
                continue
            ini = ev.get("DTSTART").dt
            if not isinstance(ini, datetime):
                continue                      # eventos de día entero
            fin = ev.get("DTEND").dt if ev.get("DTEND") else ini
            ini, fin = ini.astimezone(TZ), fin.astimezone(TZ)

            m = GRUPO_RE.search(resumen)
            tipo = TIPO_CALDAV.get(m.group(1).upper(), m.group(1).upper()) if m else ""
            grupo = (m.group(1).upper() + m.group(2)) if m else resumen
            grupos_vistos.add(grupo)
            if DLP_GRUPOS is not None and tipo != "Cours" and grupo not in DLP_GRUPOS:
                continue

            sala = RESERVA_LUGAR_RE.sub("", str(ev.get("LOCATION", ""))).strip()
            estado = str(ev.get("STATUS", "")).upper()
            anulada = estado == "CANCELLED" or "ANNUL" in (resumen + sala).upper()
            etiqueta = grupo if m and m.group(2) else tipo

            c = {
                "ue": DLP_CODIGO, "fecha": ini.date(),
                "ini": ini.hour * 60 + ini.minute, "fin": fin.hour * 60 + fin.minute,
                "sala": sala or "(sin sala)", "tipo": etiqueta, "anulada": anulada,
            }
            eventos[(c["fecha"], c["ini"], etiqueta)] = c

    print(f"[+] CalDAV DLP: {len(eventos)} sesiones. Grupos existentes: {sorted(grupos_vistos)}")
    return list(eventos.values())


def obtener_frances():
    eventos = []
    f = FRANCES_INICIO
    while f <= FRANCES_FIN:
        if f not in FRANCES_SIN_CLASE:
            eventos.append({
                "ue": "FRANCES", "fecha": f, "ini": 18 * 60, "fin": 20 * 60,
                "sala": FRANCES_SALA, "tipo": "", "anulada": False,
            })
        f += timedelta(weeks=1)
    return eventos


# ============================================================
# CALENDARIO .ICS
# ============================================================

def a_datetime(fecha, minutos):
    return datetime(fecha.year, fecha.month, fecha.day,
                    minutos // 60, minutos % 60, tzinfo=TZ)


def titulo(c):
    t = NOMBRES.get(c["ue"], c["ue"])
    if c["tipo"]:
        t += f" ({c['tipo']})"
    if c["anulada"]:
        t = "❌ ANULADA - " + t
    return t


def escapar(texto):
    return (texto.replace("\\", "\\\\").replace(";", "\\;")
                 .replace(",", "\\,").replace("\n", "\\n"))


def generar_ics(eventos, ruta="horario.ics"):
    ahora = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lineas = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Mateo//Horario Sorbonne Master//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Sorbonne - Máster y Français",
        "X-WR-TIMEZONE:Europe/Paris",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]
    for c in sorted(eventos, key=lambda e: (e["fecha"], e["ini"])):
        ini = a_datetime(c["fecha"], c["ini"]).astimezone(UTC)
        fin = a_datetime(c["fecha"], c["fin"]).astimezone(UTC)
        uid = uuid.uuid5(uuid.NAMESPACE_URL,
                         f"{c['fecha']}|{c['ini']}|{c['ue']}") 
        lineas += [
            "BEGIN:VEVENT",
            f"UID:{uid}@sorbonne-schedule",
            f"DTSTAMP:{ahora}",
            f"DTSTART:{ini:%Y%m%dT%H%M%SZ}",
            f"DTEND:{fin:%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{escapar(titulo(c))}",
            f"LOCATION:{escapar(c['sala'])}",
            f"DESCRIPTION:{escapar(c['ue'])}",
            "END:VEVENT",
        ]
    lineas.append("END:VCALENDAR")
    with open(ruta, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lineas) + "\r\n")
    print(f"[+] {ruta}: {len(eventos)} eventos")


# ============================================================
# EMAIL
# ============================================================

def texto_email(eventos):
    hoy = datetime.now(TZ).date()
    lunes = hoy - timedelta(days=hoy.weekday())
    if hoy.weekday() == 6:          # domingo -> empezamos por la semana que viene
        lunes += timedelta(weeks=1)

    bloques = []
    for s in range(2):
        l = lunes + timedelta(weeks=s)
        semana = sorted((e for e in eventos if l <= e["fecha"] < l + timedelta(days=7)),
                        key=lambda e: (e["fecha"], e["ini"]))
        lineas = [f"═══ SEMANA DEL {l:%d/%m} ═══"]
        dia_prev = None
        for c in semana:
            if c["fecha"] != dia_prev:
                lineas.append(f"\n{DIAS_ES[c['fecha'].weekday()].upper()} {c['fecha']:%d/%m}")
                dia_prev = c["fecha"]
            h = f"{c['ini']//60:02d}:{c['ini']%60:02d}-{c['fin']//60:02d}:{c['fin']%60:02d}"
            lineas.append(f"  {h}  {titulo(c):<36} → {c['sala']}")
        if not semana:
            lineas.append("\n  (sin clases de Máster ni Français)")
        bloques.append("\n".join(lineas))
    return "\n\n".join(bloques)


def enviar_email(cuerpo):
    remitente = os.environ["EMAIL_ORIGEN"]
    msg = MIMEText(cuerpo, _charset="utf-8")
    msg["Subject"] = "Tu horario de Máster y Français"
    msg["From"] = remitente
    msg["To"] = os.environ["EMAIL_DESTINO"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(remitente, os.environ["EMAIL_PASSWORD"])
        server.send_message(msg)
    print("[+] Email enviado")


# ============================================================
# PRINCIPAL
# ============================================================

if __name__ == "__main__":
    dlp = obtener_dlp()
    if not dlp:
        print("[!] Uso el plan B para DLP (planning UPMC, solo martes).")
        dlp = obtener_master(PLAN_B_DLP)
    master = dlp + obtener_master(MASTER)

    # Si la web falla del todo, NO sobrescribimos el calendario:
    # mejor dejar el anterior que vaciarlo.
    if not master:
        print("[!] No se ha leído ninguna clase de Máster. No toco horario.ics.")
        sys.exit(1)

    eventos = master + obtener_frances()
    generar_ics(eventos)

    cuerpo = texto_email(eventos)
    print("\n" + cuerpo)

    if os.environ.get("ENVIAR_EMAIL", "").lower() == "true":
        enviar_email(cuerpo)
    else:
        print("\n[i] Hoy no toca email (solo domingos o ejecución manual).")
