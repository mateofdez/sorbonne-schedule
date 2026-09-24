"""
Horario Sorbonne -> horario.ics (Google Calendar) + email semanal.

  Fuente                                  Asignaturas              .ics   email
  --------------------------------------  -----------------------  -----  -----
  CalDAV Máster (cal.ufr-info-p6)         DLP (Cours + TD1/TME1)    sí     sí
  planning.upmc.fr (M1.INFO.STL)          DLP (respaldo, martes)    sí     sí
  planning.upmc.fr (M1.MATH)              Géométrie Différentielle  sí     sí
  fijo en el código                       Français                  sí     sí
  edt-licence-info.hosted.lip6.fr         Réseaux, Systèmes, PF     no*    sí

  * Licence ya está en tu Google Calendar con la suscripción de LIP6,
    así que no la duplicamos en horario.ics.
"""

import os
import re
import sys
import time
import uuid
import smtplib
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as html_escape
from zoneinfo import ZoneInfo

import requests
import recurring_ical_events
from bs4 import BeautifulSoup
from icalendar import Calendar

# ============================================================
# CONFIGURACIÓN
# ============================================================

TZ = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")

CURSO_INICIO = date(2026, 8, 31)     # lunes de la semana 1 del curso
CURSO_FIN = date(2027, 1, 31)

# ---- DLP desde CalDAV (calendario M1_STL) ----
CALDAV_URL = "https://cal.ufr-info-p6.jussieu.fr:443/caldav.php/STL/M1_STL/"
# Cuenta de invitado de solo lectura, publicada en el config.js de la web.
CALDAV_USER = os.environ.get("CALDAV_USER", "student.master")
CALDAV_PASS = os.environ.get("CALDAV_PASS", "guest")
DLP_CODIGO = "UM4IN501"
DLP_GRUPOS = {"TD1", "TME1"}          # tus grupos de TD/TME (el Cours va siempre)

# ---- planning.upmc.fr: qué asignaturas buscar en cada público ----
MASTER = {
    "M1.INFO.STL": {"UM4IN501"},   # DLP (complementa a CalDAV: martes)
    "M1.MATH": {"UM4MA322"},       # Géométrie Différentielle
}
SEMANAS_A_LEER = 16

# ---- Licence (solo para el email) ----
LICENCE_URL = "https://edt-licence-info.hosted.lip6.fr/calendrier?sem={sem}"
SESIONES_LICENCE = {
    "UL3IN033": {"CM1", "TD5", "TP5"},   # Réseaux
    "UL3IN010": {"CM1", "TD3", "TP3"},   # Systèmes
    "UL3IN019": {"CM1", "TD1", "TP1"},   # Programmation fonctionnelle
}

# ---- Français: martes 18:00-20:00, del 15/09 al 15/12 ----
FRANCES_INICIO = date(2026, 9, 15)
FRANCES_FIN = date(2026, 12, 15)
FRANCES_SIN_CLASE = {date(2026, 10, 27)}   # vacaciones 24/10-01/11
FRANCES_SALA = "43.53.127"

# ---- Nombres y colores (email) ----
NOMBRES = {
    "UM4IN501": "DLP",
    "UM4MA322": "Géométrie Différentielle",
    "FRANCES": "Français",
    "UL3IN033": "Réseaux",
    "UL3IN010": "Systèmes",
    "UL3IN019": "Programmation fonctionnelle",
}
COLORES = {
    "UM4IN501": "#4f46e5",
    "UM4MA322": "#0d9488",
    "FRANCES": "#db2777",
    "UL3IN033": "#ea580c",
    "UL3IN010": "#2563eb",
    "UL3IN019": "#16a34a",
}
NIVEL = {"UM4IN501": "M1", "UM4MA322": "M1", "FRANCES": "", 
         "UL3IN033": "L3", "UL3IN010": "L3", "UL3IN019": "L3"}

URL_MASTER = "https://planning.upmc.fr/oldEmpSelect/jussieu/{public}/{n}/"

DIAS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES_ES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

# Colores de la leyenda del planning UPMC -> tipo de sesión.
TIPOS = {"#ffcc99": "Cours", "#ccccff": "TD", "#ffff99": "TP"}

COL_08H00 = 2      # índice de columna que corresponde a las 08:00
MIN_POR_COL = 15

FECHA_SEMANA_RE = re.compile(r"Planning du (\d{2})/(\d{2})/(\d{4})")
RESERVA_RE = re.compile(r"du (\d{2})/(\d{2}) au (\d{2})/(\d{2})")
CODIGO_RE = re.compile(r"(UM\w+)\s*\(([^)]*)\)")


def limpiar_sala(sala):
    """'Salle 14.15.406 ' -> '14.15.406'"""
    return re.sub(r"^\s*salle\s+", "", sala or "", flags=re.IGNORECASE).strip()


# ============================================================
# PLANNING UPMC (Géométrie y respaldo de DLP)
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
                sala = limpiar_sala(m_cod.group(2))
                m_res = RESERVA_RE.search(celda.get("title", ""))
                clases.append({
                    "en_ics": True,
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
    eventos, grupos_vistos, titulos_vistos = {}, set(), set()

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

            titulos_vistos.add(resumen)
            m = GRUPO_RE.search(resumen)
            tipo = TIPO_CALDAV.get(m.group(1).upper(), m.group(1).upper()) if m else ""
            grupo = (m.group(1).upper() + m.group(2)) if m else resumen
            grupos_vistos.add(grupo)
            # Solo filtramos grupos de TD/TME/TP; el curso magistral se queda siempre.
            if (DLP_GRUPOS is not None and tipo in ("TD", "TME", "TP")
                    and grupo not in DLP_GRUPOS):
                continue

            sala = limpiar_sala(RESERVA_LUGAR_RE.sub("", str(ev.get("LOCATION", ""))))
            estado = str(ev.get("STATUS", "")).upper()
            anulada = estado == "CANCELLED" or "ANNUL" in (resumen + sala).upper()
            etiqueta = grupo if m and m.group(2) else tipo

            c = {
                "ue": DLP_CODIGO, "fecha": ini.date(), "en_ics": True,
                "ini": ini.hour * 60 + ini.minute, "fin": fin.hour * 60 + fin.minute,
                "sala": sala or "(sin sala)", "tipo": etiqueta, "anulada": anulada,
            }
            eventos[(c["fecha"], c["ini"], etiqueta)] = c

    print(f"[+] CalDAV DLP: {len(eventos)} sesiones. Grupos existentes: {sorted(grupos_vistos)}")
    print(f"[i] Títulos DLP vistos: {sorted(titulos_vistos)}")
    return list(eventos.values())


def obtener_frances():
    eventos = []
    f = FRANCES_INICIO
    while f <= FRANCES_FIN:
        if f not in FRANCES_SIN_CLASE:
            eventos.append({
                "ue": "FRANCES", "fecha": f, "ini": 18 * 60, "fin": 20 * 60, "en_ics": True,
                "sala": FRANCES_SALA, "tipo": "", "anulada": False,
            })
        f += timedelta(weeks=1)
    return eventos



# ============================================================
# LICENCE (solo email)
# ============================================================

DIA_FECHA_RE = re.compile(
    r"\b(Lundi|Mardi|Mercredi|Jeudi|Vendredi|Samedi|Dimanche)\s+(\d{2})/(\d{2})\b")
SESION_LIC_RE = re.compile(
    r"(\d{2}):(\d{2})\s*[–—-]\s*(\d{2}):(\d{2})\s+"
    r"(UL\d[A-Z]{2}\d{3})\s+((?:CM|TD|TP)\d*)"
    r"(?:\s*📍\s*(\S+))?")


def parsear_licence(html, referencia):
    # Todo el texto visible en una sola línea: así da igual cómo
    # estén anidadas las etiquetas HTML.
    texto = " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())
    dias = list(DIA_FECHA_RE.finditer(texto))
    clases = []
    for i, d in enumerate(dias):
        fin_trozo = dias[i + 1].start() if i + 1 < len(dias) else len(texto)
        fecha = fecha_curso(int(d.group(2)), int(d.group(3)), referencia)
        for m in SESION_LIC_RE.finditer(texto, d.end(), fin_trozo):
            h1, m1, h2, m2, ue, sesion, sala = m.groups()
            if sesion not in SESIONES_LICENCE.get(ue, set()):
                continue
            clases.append({
                "ue": ue, "fecha": fecha, "en_ics": False,
                "ini": int(h1) * 60 + int(m1), "fin": int(h2) * 60 + int(m2),
                "sala": sala or "(sin sala)", "tipo": sesion, "anulada": False,
            })
    return clases


def obtener_licence(lunes_semanas):
    clases = []
    for lunes in lunes_semanas:
        sem = (lunes - CURSO_INICIO).days // 7 + 1
        try:
            r = requests.get(LICENCE_URL.format(sem=sem), timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[!] Licence semana {sem}: {e}")
            continue
        nuevas = [c for c in parsear_licence(r.text, lunes)
                  if lunes <= c["fecha"] < lunes + timedelta(days=7)]
        print(f"[+] Licence semana {sem} ({lunes:%d/%m}): {len(nuevas)} clases")
        clases.extend(nuevas)
    return clases

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
    eventos = [e for e in eventos if e.get("en_ics")]
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

def semanas_email():
    """Lunes de las dos semanas a mostrar (el domingo, empieza por la siguiente)."""
    hoy = datetime.now(TZ).date()
    lunes = hoy - timedelta(days=hoy.weekday())
    if hoy.weekday() == 6:
        lunes += timedelta(weeks=1)
    return [lunes, lunes + timedelta(weeks=1)]


def hhmm(minutos):
    return f"{minutos // 60:02d}:{minutos % 60:02d}"


def clases_de_semana(eventos, lunes):
    return sorted((e for e in eventos if lunes <= e["fecha"] < lunes + timedelta(days=7)),
                  key=lambda e: (e["fecha"], e["ini"], e["fin"]))


def rango_semana(lunes):
    viernes = lunes + timedelta(days=4)
    if lunes.month == viernes.month:
        return f"{lunes.day} – {viernes.day} de {MESES_ES[viernes.month]}"
    return f"{lunes.day} de {MESES_ES[lunes.month]} – {viernes.day} de {MESES_ES[viernes.month]}"


def email_texto(eventos, semanas):
    """Versión en texto plano (por si el cliente no muestra HTML)."""
    bloques = []
    for lunes in semanas:
        lineas = [f"SEMANA {rango_semana(lunes).upper()}", "=" * 60]
        dia_prev = None
        for c in clases_de_semana(eventos, lunes):
            if c["fecha"] != dia_prev:
                lineas.append(f"\n{DIAS_ES[c['fecha'].weekday()]} {c['fecha']:%d/%m}")
                dia_prev = c["fecha"]
            nombre = NOMBRES.get(c["ue"], c["ue"])
            tipo = ("ANULADA " if c["anulada"] else "") + (c["tipo"] or "")
            lineas.append(f"  {hhmm(c['ini'])}-{hhmm(c['fin'])} | {nombre} {tipo}".rstrip()
                          + f" | {c['sala']}")
        bloques.append("\n".join(lineas))
    return "\n\n\n".join(bloques)


def email_html(eventos, semanas):
    fuente = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    partes = [f'<div style="{fuente}max-width:680px;margin:0 auto;color:#1f2937;">']
    partes.append('<h1 style="font-size:22px;margin:8px 0 4px;">📅 Tu horario</h1>'
                  '<p style="margin:0 0 20px;color:#6b7280;font-size:13px;">'
                  'Sorbonne Université · Licence L3 + Máster M1 + Français</p>')

    for lunes in semanas:
        clases = clases_de_semana(eventos, lunes)
        partes.append(
            f'<h2 style="font-size:16px;margin:24px 0 8px;padding:8px 12px;'
            f'background:#111827;color:#fff;border-radius:6px;">'
            f'Semana del {html_escape(rango_semana(lunes))}</h2>')
        if not clases:
            partes.append('<p style="color:#6b7280;">Sin clases esta semana 🎉</p>')
            continue

        partes.append(
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="width:100%;border-collapse:collapse;font-size:14px;">'
            '<tr style="color:#6b7280;font-size:11px;text-transform:uppercase;'
            'letter-spacing:.04em;">'
            '<th align="left" style="padding:6px 8px;width:95px;">Hora</th>'
            '<th align="left" style="padding:6px 8px;">Asignatura</th>'
            '<th align="left" style="padding:6px 8px;width:70px;">Tipo</th>'
            '<th align="left" style="padding:6px 8px;width:95px;">Aula</th></tr>')

        dia_prev = None
        for c in clases:
            if c["fecha"] != dia_prev:
                dia_prev = c["fecha"]
                partes.append(
                    f'<tr><td colspan="4" style="padding:14px 8px 6px;font-weight:700;'
                    f'border-bottom:2px solid #e5e7eb;">'
                    f'{DIAS_ES[c["fecha"].weekday()]} '
                    f'<span style="color:#9ca3af;font-weight:400;">'
                    f'{c["fecha"]:%d/%m}</span></td></tr>')

            color = COLORES.get(c["ue"], "#6b7280")
            nombre = html_escape(NOMBRES.get(c["ue"], c["ue"]))
            nivel = NIVEL.get(c["ue"], "")
            etiqueta_nivel = (f' <span style="font-size:10px;color:#9ca3af;">{nivel}</span>'
                              if nivel else "")
            tachado = "text-decoration:line-through;color:#9ca3af;" if c["anulada"] else ""
            aviso = (' <span style="color:#dc2626;font-weight:700;">ANULADA</span>'
                     if c["anulada"] else "")
            partes.append(
                f'<tr style="border-bottom:1px solid #f3f4f6;{tachado}">'
                f'<td style="padding:8px;white-space:nowrap;font-variant-numeric:tabular-nums;">'
                f'{hhmm(c["ini"])}–{hhmm(c["fin"])}</td>'
                f'<td style="padding:8px;border-left:4px solid {color};">'
                f'<b>{nombre}</b>{etiqueta_nivel}{aviso}</td>'
                f'<td style="padding:8px;">{html_escape(c["tipo"] or "")}</td>'
                f'<td style="padding:8px;white-space:nowrap;">{html_escape(c["sala"])}</td>'
                f'</tr>')
        partes.append("</table>")

    partes.append('<p style="margin-top:28px;color:#9ca3af;font-size:11px;">'
                  'Generado automáticamente desde los calendarios oficiales. '
                  'Si algo no cuadra, comprueba la web de la asignatura.</p></div>')
    return "".join(partes)


def enviar_email(asunto, texto, html):
    remitente = os.environ["EMAIL_ORIGEN"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = os.environ["EMAIL_DESTINO"]
    msg.attach(MIMEText(texto, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(remitente, os.environ["EMAIL_PASSWORD"])
        server.send_message(msg)
    print("[+] Email enviado")


# ============================================================
# PRINCIPAL
# ============================================================

def combinar(*listas):
    """Une varias fuentes sin duplicar (misma asignatura, día y hora de inicio)."""
    vistos, resultado = set(), []
    for lista in listas:
        for c in lista:
            clave = (c["ue"], c["fecha"], c["ini"])
            if clave not in vistos:
                vistos.add(clave)
                resultado.append(c)
    return resultado


if __name__ == "__main__":
    # CalDAV primero: sus datos (Cours + TME1) mandan sobre el planning UPMC.
    master = combinar(obtener_dlp(), obtener_master(MASTER))

    # Si la web falla del todo, NO sobrescribimos el calendario.
    if not master:
        print("[!] No se ha leído ninguna clase de Máster. No toco horario.ics.")
        sys.exit(1)

    eventos = master + obtener_frances()
    generar_ics(eventos)

    semanas = semanas_email()
    todos = eventos + obtener_licence(semanas)
    texto = email_texto(todos, semanas)
    print("\n" + texto)

    if os.environ.get("ENVIAR_EMAIL", "").lower() == "true":
        asunto = f"📅 Tu horario · semana del {semanas[0]:%d/%m}"
        enviar_email(asunto, texto, email_html(todos, semanas))
    else:
        print("\n[i] Hoy no toca email (solo domingos o ejecución manual).")
