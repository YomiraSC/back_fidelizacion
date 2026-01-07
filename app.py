import os
import json
from datetime import datetime
import pytz
from typing import Optional, Dict, Any
from flask import Flask, request, Response, g
from psycopg_pool import ConnectionPool
from uuid import uuid4
# LangChain / OpenAI / Elasticsearch
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, messages_to_dict, BaseMessage
from langchain_elasticsearch import ElasticsearchStore
from langchain.tools import tool
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.prebuilt import create_react_agent
#para pasar el num de telefono
#from langchain_core.tools import ToolInvocation
#from langgraph.prebuilt import create_react_agent_executor
# Google Firestore
#from google.cloud import firestore
#from google.cloud.firestore_v1 import SERVER_TIMESTAMP
from langchain.tools import Tool
from google.cloud import firestore as _gc_firestore
from google.cloud.firestore_v1 import SERVER_TIMESTAMP
# Twilio
from twilio.rest import Client
import requests
#Para enviar a Vercel
import threading, time, uuid

VERCEL_WEBHOOK_URL = "https://crm-fidelizacion.vercel.app/api/webhook/whatsapp"

CHECKPOINT_NS = "campaign_seed"
#PROGRAMA CON COMENTARIOS EN LAS HERRAMIENTAS
import traceback
# ------------------------------------------------------------------------------
# 1) VARIABLES DE ENTORNO (solo para PRUEBAS: texto plano)
# ------------------------------------------------------------------------------
# Clave de OpenAI (pruebas locales)
os.environ["OPENAI_API_KEY"] = ""

# Cadena de conexión a PostgreSQL (solo para pruebas)
os.environ["DB_URI"] = ""

# Credenciales de Elasticsearch RAG (solo para pruebas)
os.environ["ELASTIC_URL"]      = ""
os.environ["ELASTIC_USER"]     = "elastic"
os.environ["ELASTIC_PASSWORD"] = "P=IK-doIv668orND5FmG"
os.environ["ELASTIC_INDEX"]    = "fidelizacion-v1"

# Twilio API Keys (pruebas locales)
# Reemplaza con tus valores para probar localmente
TWILIO_ACCOUNT_SID = ""
TWILIO_AUTH_TOKEN  = ""
CURRENT_SENDER = None


# --- WhatsApp Cloud API (Meta) ---
WHATSAPP_TOKEN = ""
PHONE_NUMBER_ID = ""
VERIFY_TOKEN = "token_fide"  # usa el mismo que pusiste en Meta

# ------------------------------------------------------------------------------
# 2) COMPONENTES: Firestore, Twilio y Postgre
# ------------------------------------------------------------------------------

#META
# --- WhatsApp Cloud API (Meta) ---
API_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json",
}

def send_whatsapp(to_number: str, message_body: str) -> str:
    to = (to_number or "").replace("whatsapp:", "").replace("+", "").strip()
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message_body},
    }
    try:
        r = requests.post(API_URL, headers=HEADERS, json=payload, timeout=15)
    except Exception as e:
        print(f"[META SEND] request error: {e}", flush=True)
        return ""

    print(f"[META SEND] status={r.status_code} to={to}", flush=True)
    print(f"[META SEND] resp={r.text}", flush=True)

    # ⬇️ NUEVO: guardar el id del mensaje saliente (mensaje_out)
    if 200 <= r.status_code < 300:
        try:
            data = r.json()
            mid = (data.get("messages") or [{}])[0].get("id")
            if mid:
                print(f"[META SEND] message_id={mid}", flush=True)
                try:
                    postgresql.registrar_mensaje_out(
                        id_msg=mid,
                        phone_to=to_number,        # el manager normaliza
                        template_name="bot_reply", # si fuera plantilla real, cámbialo
                        template_lang="es",
                        campanha_id=None
                    )
                except Exception as e2:
                    print(f"[META SEND] warn: no pude registrar mensaje_out ({e2})", flush=True)
        except Exception:
            pass

    return r.text


#FIN META

class DataBaseFirestoreManager:
    def __init__(self):
        self.db = self._connect()
        self.tz = pytz.timezone("America/Lima")
    
    def _connect(self):
        """Establece la conexión a Firestore."""
        try:
            db = _gc_firestore.Client()
            print("✅ Conexión exitosa a Firestore", flush=True)
            return db
        except Exception as e:
            print(f"❌ ERROR al conectar con Firestore: {e}", flush=True)
            return None

    def _reconnect_if_needed(self):
        """Verifica si la conexión sigue activa y la restablece si es necesario."""
        try:
            _ = self.db.collection("fidelizacion").document("connection_test").get()
        except Exception as e:
            print(f"⚠ Conexión perdida, intentando reconectar... {e}", flush=True)
            self.db = self._connect()

    def crear_documento(self, celular, id_cliente, id_bot, mensaje, sender):
        """Crea un documento en la colección 'fidelizacion'."""
        self._reconnect_if_needed()
        data = {
            "celular": celular,
            "fecha": SERVER_TIMESTAMP,
            "id_cliente": id_cliente,
            "id_bot": id_bot,
            "mensaje": mensaje,
            "sender": sender
        }
        try:
            doc_ref = self.db.collection("fidelizacion").document()
            doc_ref.set(data)
            print("✅ Documento creado exitosamente en Firestore.", flush=True)
        except Exception as e:
            print(f"❌ Error al crear el documento en Firestore: {e}", flush=True)




class DataBasePostgreSQLManager:
    def __init__(self):
        self.pool = ConnectionPool(conninfo=os.environ["DB_URI"])

    def registrar_motivo_estado(self, telefono: str, motivo: str, estado: str):

        # — Normalizar teléfono a formato "+<digits>" —
        tel_original = "" if telefono is None else str(telefono).strip()
        telefono = "+" + tel_original.lstrip("+").replace(" ", "")
        if telefono != tel_original:
            print(f"    → Normalizado teléfono: {tel_original!r} → {telefono!r}", flush=True)
        # fin normalizar

        print("🟢 [DEBUG] Iniciando registrar_motivo_estado", flush=True)
        print(f"    → Parámetros recibidos: telefono={telefono}, motivo={motivo}, estado={estado}", flush=True)
        with self.pool.connection() as conn:
            print("    → Conexión obtenida del pool", flush=True)
            with conn.cursor() as cur:
                # 1) Apuntamos al schema correcto
                cur.execute("SET search_path TO fidelizacion;")
                # 2) Obtenemos el cliente_id a partir del celular
                cur.execute(
                    "SELECT cliente_id FROM cliente WHERE celular = %s;",
                    (telefono,)
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Cliente con celular {telefono!r} no existe")
                cliente_id = row[0]

                # 3) Si ya hay un contrato previo, guardamos su estado y motivo en historico_estado
                cur.execute(
                    "SELECT contrato_id, estado, motivo FROM contrato WHERE cliente_id = %s;",
                    (cliente_id,)
                )
                contrato_row = cur.fetchone()
                if contrato_row:
                    contrato_id_prev, estado_prev, motivo_prev = contrato_row
                    print(f"    → Registro previo encontrado: contrato_id={contrato_id_prev}, estado={estado_prev}, motivo={motivo_prev}", flush=True)
                    # ➕ Guardar histórico con fecha_estado = NOW()
                    cur.execute(
                        """
                        INSERT INTO historico_estado (contrato_id, estado, motivo, fecha_estado)
                        VALUES (%s, %s, %s, NOW());
                        """,
                        (contrato_id_prev, estado_prev, motivo_prev)
                    )
                    print(f"✅ Histórico guardado (con fecha_estado=NOW()): contrato_id={contrato_id_prev}", flush=True)
                else:
                    print("    → No existía contrato previo, no se agregó al histórico", flush=True)

                # 4) Upsert en contrato usando ON CONFLICT sobre cliente_id
                print("    → Ejecutando INSERT ... ON CONFLICT (cliente_id) DO UPDATE", flush=True)
                # ➕ fecha_pago = NOW() tanto en insert como en update
                cur.execute(
                    """
                    INSERT INTO contrato (cliente_id, motivo, estado, fecha_pago)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (cliente_id)
                    DO UPDATE SET
                        motivo     = EXCLUDED.motivo,
                        estado     = EXCLUDED.estado,
                        fecha_pago = NOW();
                    """,
                    (cliente_id, motivo, estado)
                )
                print(f"✅ Contrato upserted para cliente_id={cliente_id} (fecha_pago=NOW())", flush=True)

                # 5) Actualizar la tabla cliente
                print("    → Actualizando estado y detalle en tabla cliente", flush=True)
                cur.execute(
                    """
                    UPDATE cliente
                    SET estado = %s,
                        detalle = %s,
                        fecha_ultima_interaccion_bot = NOW()
                    WHERE cliente_id = %s;
                    """,
                    (estado, motivo, cliente_id)
                )
                print(f"✅ Cliente actualizado con estado={estado} y detalle={motivo} ", flush=True)

        print("🟢 [DEBUG] registrar_motivo_estado completado\n", flush=True)


    # NUEVO: busca cliente_id por celular (normalizando igual que en registrar_motivo_estado)
    def obtener_cliente_id_por_celular(self, telefono: str) -> int:
        tel_original = "" if telefono is None else str(telefono).strip()
        telefono = "+" + tel_original.lstrip("+").replace(" ", "")
        if telefono != tel_original:
            print(f"    → Normalizado teléfono: {tel_original!r} → {telefono!r}", flush=True)

        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO fidelizacion;")
                cur.execute(
                    "SELECT cliente_id FROM cliente WHERE celular = %s;",
                    (telefono,)
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"No existe cliente con celular {telefono!r}")
                return row[0]

    # NUEVO: inserta una promesa en la tabla pago
    def insertar_pago_promesa(self, cliente_id: int, fecha_pago_dt) -> int:
        """
        Inserta un registro en pago con monto=0.
        fecha_pago_dt debe ser datetime (naive, sin tz) para 'timestamp without time zone'.
        Retorna el pago_id generado.
        """
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO fidelizacion;")
                cur.execute(
                    """
                    INSERT INTO pago (cliente_id, fecha_pago, monto)
                    VALUES (%s, %s, %s)
                    RETURNING pago_id;
                    """,
                    (cliente_id, fecha_pago_dt, 0)
                )
                pago_id = cur.fetchone()[0]
                print(f"✅ Pago/promesa insertado: pago_id={pago_id}, cliente_id={cliente_id}, fecha_pago={fecha_pago_dt}", flush=True)
                return pago_id


    # ---------- contactabilidad: envíos ----------
    def registrar_mensaje_out(
        self,
        id_msg: str,
        phone_to: str,
        template_name: str,
        template_lang: str,
        campanha_id: Optional[int] = None,
    ) -> bool:
        """
        Crea (o ignora si existe) el envío saliente asociado al id_msg de Meta.
        Usa idempotencia con ON CONFLICT DO NOTHING para no duplicar.
        """
        if not id_msg:
            print("❌ [PG][registrar_mensaje_out] id_msg vacío", flush=True)
            return False

        # normalizar teléfono a +<digits> (igual estilo que registrar_motivo_estado)
        tel_original = "" if phone_to is None else str(phone_to).strip()
        phone_norm = "+" + tel_original.lstrip("+").replace(" ", "")

        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    # esquema correcto
                    cur.execute("SET search_path TO fidelizacion;")
                    cur.execute(
                        """
                        INSERT INTO mensaje_out (id_msg, phone_to, template_name, template_lang, campanha_id)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (id_msg) DO NOTHING;
                        """,
                        (id_msg, phone_norm, template_name, template_lang, campanha_id)
                    )
                    print(f"📨 [PG] mensaje_out upsert OK id_msg={id_msg}", flush=True)
                    return True
        except Exception as e:
            print(f"❌ [PG][registrar_mensaje_out] Error: {e}", flush=True)
            return False


    # ---------- contactabilidad: estados ----------
    def registrar_status_event(
        self,
        id_msg: str,
        estado: str,                 # 'sent' | 'delivered' | 'read' | 'failed'
        ts_unix: Optional[int] = None,
        recipient_id: Optional[str] = None,
        pricing_json: Optional[str] = None,
        conversation_json: Optional[str] = None,
        errors_json: Optional[str] = None,
    ) -> bool:
        """
        Inserta un evento de estado para el id_msg. No actualiza nada más;
        la vista v_mensaje_estado_actual te da el último estado por mensaje.
        """
        if not id_msg or not estado:
            print("❌ [PG][registrar_status_event] id_msg/estado vacío", flush=True)
            return False

        # normalizar recipient a +<digits> si viene
        if recipient_id:
            tel_original = str(recipient_id).strip()
            recipient_norm = "+" + tel_original.lstrip("+").replace(" ", "")
        else:
            recipient_norm = None

        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    # esquema correcto
                    cur.execute("SET search_path TO fidelizacion;")
                    cur.execute(
                        """
                        INSERT INTO mensaje_status_event
                            (id_msg, estado, ts_unix, recipient_id, pricing_json, conversation_json, errors_json)
                        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb);
                        """,
                        (
                            id_msg,
                            estado,
                            ts_unix,
                            recipient_norm,
                            pricing_json,
                            conversation_json,
                            errors_json,
                        )
                    )
                    print(f"🧩 [PG] status_event insert OK id_msg={id_msg} estado={estado}", flush=True)
                    return True
        except Exception as e:
            print(f"❌ [PG][registrar_status_event] Error: {e}", flush=True)
            return False


    def registrar_webhook_log(self, event_type: str, payload: Dict[str, Any]) -> Optional[int]:
        """
        Inserta el payload RAW del webhook de Meta en fidelizacion.webhook_logs.
        Retorna el id generado si todo OK; si no, retorna None.
        """
        try:
            # seguridad: event_type corto y payload dict
            et = (event_type or "unknown")[:100]

            # payload -> json string (para castear a jsonb en SQL)
            payload_json = json.dumps(payload or {}, ensure_ascii=False)

            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    # schema correcto
                    cur.execute("SET search_path TO fidelizacion;")
                    cur.execute(
                        """
                        INSERT INTO webhook_logs (event_type, payload)
                        VALUES (%s, %s::jsonb)
                        RETURNING id;
                        """,
                        (et, payload_json)
                    )
                    row = cur.fetchone()
                    new_id = row[0] if row else None

            print(f"✅ [PG][registrar_webhook_log] OK id={new_id} event_type={et!r}", flush=True)
            return new_id

        except Exception as e:
            print(f"❌ [PG][registrar_webhook_log] Error: {e}", flush=True)
            return None

# ------------------------------------------------------------------------------
# 3) HERRAMIENTAS DE LANGCHAIN (RAG + personalizadas)
# ------------------------------------------------------------------------------

# 3.1. Función auxiliar para normalizar y validar fechas
def normalizar_y_validar_fecha(fecha_str: str) -> (bool, str):
    partes = fecha_str.split("/")
    if len(partes) == 2:
        dia_mes = fecha_str
        anio_actual = datetime.now().year
        fecha_str = f"{dia_mes}/{anio_actual}"
    try:
        fecha_obj = datetime.strptime(fecha_str, "%d/%m/%Y")
    except ValueError:
        return False, f"Error: '{fecha_str}' no respeta el formato DD/MM/AAAA."
    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if fecha_obj <= hoy:
        try:
            fecha_obj = fecha_obj.replace(year=fecha_obj.year + 1)
        except ValueError:
            return False, f"Error: la fecha '{fecha_str}' no se pudo normalizar a futuro."
    fecha_legible = fecha_obj.strftime("%d de %B de %Y")
    return True, fecha_legible

# 3.2. Herramienta: 'registrar_promesa_pago'

@tool(
    "registrar_promesa_pago",
    description=(
        "Registra la promesa de pago de un cliente identificado por su número de teléfono. "
        "Si recibe 'fecha_promesa' vacío, pregunta la fecha. "
        "Si recibe DD/MM, asume año actual y normaliza. "
        "Si recibe DD/MM/AAAA, normaliza si ya pasó."
    ),
)
def registrar_promesa_pago(payload: str) -> str:
    print("🛠️ [TOOL] >>> Entró a herramienta: registrar_promesa_pago", flush=True)
    print("🛠️ [TOOL] >>> Payload raw:", repr(payload), flush=True)

    # --- Parseo del JSON ---
    try:
        data = json.loads(payload)
        print("🟢 [TOOL] JSON parseado OK:", data, flush=True)
    except Exception as e:
        print("❌ [TOOL] JSON inválido:", e, flush=True)
        return (
            "Error: JSON inválido. Ejemplo:\n"
            "{\"cliente_telefono\":\"+51912345678\",\"fecha_promesa\":\"DD/MM o DD/MM/AAAA\"}"
        )

    cliente_telefono = data.get("cliente_telefono")
    fecha_promesa = data.get("fecha_promesa")
    print(f"🟡 [TOOL] cliente_telefono={cliente_telefono!r} | fecha_promesa={fecha_promesa!r}", flush=True)

    if not cliente_telefono:
        return "Error: falta 'cliente_telefono'."
    if not fecha_promesa:
        return "¿Para qué fecha exacta te comprometes a pagar? (DD/MM o DD/MM/AAAA)"

    # --- Normalización a datetime naive ---
    try:
        partes = [int(x) for x in fecha_promesa.split("/")]
        if len(partes) == 2:
            d, m = partes
            y = datetime.now().year
        else:
            d, m, y = partes
        fecha_dt = datetime(y, m, d, 0, 0, 0)
        hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if fecha_dt <= hoy:
            fecha_dt = fecha_dt.replace(year=fecha_dt.year + 1)
        fecha_legible = fecha_dt.strftime("%d/%m/%Y")
        print(f"🟡 [TOOL] fecha_dt(normalizada)={fecha_dt} | legible={fecha_legible}", flush=True)
    except Exception as e:
        print("❌ [TOOL] No pude interpretar la fecha:", e, flush=True)
        return "No pude interpretar la fecha. Usa DD/MM o DD/MM/AAAA."

    # --- Inserción en pago + registro de estado ---
    try:
        telefono_bd = g.sender or cliente_telefono
        print(f"🟡 [TOOL] telefono_bd usado={telefono_bd!r}", flush=True)

        cliente_id = postgresql.obtener_cliente_id_por_celular(telefono_bd)
        print(f"🟢 [TOOL] cliente_id obtenido={cliente_id}", flush=True)

        pago_id = postgresql.insertar_pago_promesa(cliente_id, fecha_dt)
        print(f"🟢 [TOOL] pago/promesa insertado pago_id={pago_id}", flush=True)

        # NUEVO: guardar estado "Promesa de pago"
        motivo_estado = f"Se compromete a pagar el {fecha_legible}"
        try:
            postgresql.registrar_motivo_estado(telefono_bd, motivo_estado, "Promesa de pago")
            print("✅ [TOOL] Estado 'Promesa de pago' registrado en BD", flush=True)
        except Exception as e2:
            print(f"⚠️ [TOOL] Promesa insertada pero falló registrar estado: {e2}", flush=True)

    except Exception as e:
        print(f"❌ [TOOL] Error al insertar promesa en pago/obtener cliente: {e}", flush=True)
        return "Ocurrió un problema registrando tu promesa. Inténtalo más tarde."

    # --- Mensaje al usuario ---
    return (
        f"👍 Listo. Registré tu promesa de pago para el {fecha_legible}. "
        "Si necesitas modificarla, avísame."
    )


# 3.3. Función para clasificar intenciones económicas
def detectar_intencion_economica(mensaje_usuario: str) -> str:
    system_prompt = (
        "Eres un clasificador de intenciones. Recibes un texto de un cliente y solo debes responder "
        "con una de estas dos etiquetas (sin comillas ni explicaciones adicionales):\n"
        "– motivo_económico  (si el cliente habla algo sobre que no puede pagar por un problema económico)\n"
        "– otro_motivo       (si el cliente menciona el motivo como algo diferente a lo económico)\n"
    )
    print("entrando a detectar_intencion_economica", flush=True)
    clasificador_model = ChatOpenAI(model="gpt-4", temperature=0.0)
    respuesta = clasificador_model.generate(
        messages=[
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=mensaje_usuario),
            ]
        ]
    )
    etiqueta = respuesta.generations[0][0].text.strip().lower()
    print(f"etiqueta en detectar_intencion_economica: {etiqueta}", flush=True)
    if etiqueta not in ("motivo_económico", "otro_motivo"):
        raise ValueError(f"Etiqueta inesperada del modelo: '{etiqueta}'")
    return etiqueta

# 3.4. Herramienta: 'gestionar_cambio_contrato'

@tool("gestionar_cambio_contrato",
    description=(
        """
        OBJETIVO / ESTADO: Ofrecer alternativas de pago cuando el cliente QUIERE PAGAR pero NO PUEDE con el
        acuerdo actual (Estado = "Negociacion de pago"). El foco es en facilidades: fraccionamiento, plazos,
        redistribución, o cambio a producto de menor costo.

        CUÁNDO USAR:
        - El cliente expresa intención de pagar pero solicita facilidades, plazos, fraccionar o descuentos.

        NO USAR:
        - Si quiere hablar YA con un humano para pagar (usa derivar_contacto_humano).
        - Si es una duda que el bot/RAG puede resolver (usa busqueda_productos).
        - Si es una duda que no puedes resolver (usa aclarar_info_reclamo).
        - Si está en tono de salida/enojo sin intención de pagar (usa registrar_motivo_salida).

        EJEMPLOS DISPARADORES:
        - "¿Puedo pagar en dos partes este mes?"
        - "No puedo pagar todo, ¿me pueden dar un plazo?"
        - "¿Hay alguna forma de fraccionar el pago?"
        - "Puedo pagar la mitad hoy y la otra el próximo mes."

        RESPUESTA ESPERADA:
        - Ofrecer alternativas concretas (p.ej., fraccionamiento / cambio de producto / redistribución de cuotas)
          y pedir confirmación de la opción preferida.
        - Estilo breve (máx. 3 oraciones), claro y empático.

        PARÁMETROS:
        - JSON con: { "motivo": str }

        EFECTO ESPERADO (ya implementado en el código):
        - Registrar en BD con estado = "Negociacion de pago".
        - Clasificar si el motivo es económico para ajustar el mensaje.
        """
    ),
)
def gestionar_cambio_contrato(payload: str) -> str:
    print("🛠️ [TOOL] >>> Entró a gestionar_cambio_contrato", flush=True)
    print(f"🛠️ [TOOL] >>> Payload raw: {payload!r}", flush=True)

    # 1) Intentar JSON; si no, tratarlo como texto plano (motivo)
    data = {}
    motivo = None
    try:
        data = json.loads(payload)
        print(f"🟢 [TOOL] JSON OK: {data}", flush=True)
        motivo = data.get("motivo")
    except Exception as e:
        print(f"⚠️ [TOOL] No es JSON. Usaré el payload como motivo. Error: {e}", flush=True)
        motivo = (payload or "").strip()

    print(f"🟡 [TOOL] motivo={motivo!r}", flush=True)
    if not motivo:
        return "Error: no encontré el campo 'motivo'. Envía el motivo o reformula tu solicitud."

    # 2) Teléfono
    cliente_telefono = (data.get("cliente_telefono") if isinstance(data, dict) else None) or g.sender
    print(f"🟡 [TOOL] telefono={cliente_telefono!r}", flush=True)

    # 3) Clasificar (BUGFIX: usar motivo, no payload)
    try:
        tipo = detectar_intencion_economica(motivo)   # <-- CORREGIDO
        print(f"🟡 [TOOL] tipo_intencion={tipo}", flush=True)
    except Exception as e:
        print(f"❌ [TOOL] Error clasificando: {e}", flush=True)
        return "Error interno al clasificar el motivo."

    # 4) Respuesta + persistencia
    mensaje = ""
    try:
        if tipo == "motivo_económico":
            mensaje = (
                "Entendemos tu situación económica. Podemos ofrecerte:\n"
                "- Cambio a producto de menor costo.\n"
                "- Redistribución de cuotas o plazos.\n"
                "¿Cuál opción prefieres?"
            )
            if cliente_telefono:
                postgresql.registrar_motivo_estado(cliente_telefono, "motivo economicos", "Negociacion de pago")
                print("✅ [TOOL] BD: estado=Negociacion de pago / motivo=motivo economicos", flush=True)
        else:
            mensaje = (
                "Podemos evaluar congelar temporalmente tu contrato sin penalidad.\n"
                "¿Te parece si seguimos por ahí?"
            )
            if cliente_telefono:
                postgresql.registrar_motivo_estado(cliente_telefono, "otros motivos", "Gestion de contrato")
                print("✅ [TOOL] BD: estado=Negociacion de pago / motivo=otros motivos", flush=True)
    except Exception as e:
        print(f"❌ [TOOL] Error guardando en BD: {e}", flush=True)

    print(f"🟢 [TOOL] Mensaje final: {mensaje!r}", flush=True)
    return mensaje



#NUEVOOOO

def detectar_enfado(mensaje: str) -> str:
    print("🎯 [CLF] detectar_enfado() >> mensaje:", repr(mensaje), flush=True)
    system_prompt = (
        "Eres un clasificador. Recibes un texto de un cliente y solo debes responder "
        "con una de estas dos etiquetas (sin comillas ni explicaciones adicionales):\n"
        "– con_ira\n"
        "– sin_ira\n"
    )
    clasificador_model = ChatOpenAI(model="gpt-4", temperature=0.0)
    respuesta = clasificador_model.generate(
        messages=[[SystemMessage(content=system_prompt), HumanMessage(content=mensaje)]]
    )
    etiqueta = respuesta.generations[0][0].text.strip().lower()
    print("🎯 [CLF] etiqueta:", repr(etiqueta), flush=True)
    if etiqueta not in ("con_ira", "sin_ira"):
        raise ValueError(f"Etiqueta inesperada del modelo: '{etiqueta}'")
    return etiqueta

@tool(
    "aclarar_info_reclamo",
    description=(
        """
        OBJETIVO / ESTADO: Gestionar dudas o reclamos que el bot NO pueda resolver.
        - Si el tono es neutral/cordial → Estado = "Duda no resuelta".
        - Si el tono es agresivo/ofensivo → Estado = "Duda agresiva no resuelta".
        (La herramienta detecta el tono automáticamente.)

        CUÁNDO USAR:
        - Preguntas o requerimientos que el asistente NO puede resolver directamente.
        - ÚSALA DESPUÉS de considerar "busqueda_productos" (RAG). Si RAG no aplica o no resuelve, entonces deriva aquí.

        NO USAR:
        - Si la consulta es claramente resoluble por RAG (usa primero busqueda_productos).
        - Si hay intención de pago inmediata/llamada (usa derivar_contacto_humano).
        - Si el cliente pide facilidades (usa gestionar_cambio_contrato).
        - Si es una salida agresiva sin intención de pagar (usa registrar_motivo_salida).

        EJEMPLOS DISPARADORES (tono neutral: "Duda no resuelta"):
        - "¿Cuándo es mi próxima asamblea?"
        - "No encuentro mi recibo de pago."
        - "¿Me pueden enviar el detalle de mi deuda?"
        - "¿Dónde puedo ver el contrato que firmé?"

        EJEMPLOS DISPARADORES (tono agresivo: "Duda agresiva no resuelta"):
        - "Me voy a Indecopi si no solucionan mi caso en este momento."
        - "Por qué rayos no me han dado mi carro"
        - "No encuentro la forma de pagar, son unos idiotas"

        RESPUESTA ESPERADA:
        - Confirmar que se escalará/coordinará contacto para resolver la duda/reclamo.
        - Mantener máximo 3 oraciones, tono profesional. En caso agresivo, máxima contención y claridad.

        PARÁMETROS:
        - JSON con: { "cliente_telefono": str (opcional), "mensaje": str }

        EFECTO ESPERADO (ya implementado en el código):
        - Registrar en BD con estado = "Duda no resuelta" o "Duda agresiva no resuelta", según tono detectado.
        """
    ),
)
def aclarar_info_reclamo(payload: str) -> str:
    print("🛠️ [TOOL] >>> Entró a aclarar_info_reclamo", flush=True)
    print("🛠️ [TOOL] >>> Payload raw:", repr(payload), flush=True)

    # 1) Intentamos parsear JSON; si falla, tratamos payload como texto del mensaje.
    data = {}
    mensaje = None
    telefono = None
    try:
        data = json.loads(payload)
        print("🟢 [TOOL] JSON parseado OK:", data, flush=True)
        mensaje = data.get("mensaje")
        telefono = data.get("cliente_telefono")
    except Exception as e:
        print("⚠️ [TOOL] No es JSON. Usaré payload como 'mensaje'. Error:", e, flush=True)
        mensaje = (payload or "").strip()

    print("🟡 [TOOL] mensaje extraído:", repr(mensaje), flush=True)
    if not mensaje:
        print("🔴 [TOOL] mensaje vacío → no se puede clasificar", flush=True)
        return "Para ayudarte con tu reclamo, cuéntame en una frase qué necesitas."

    telefono = telefono or getattr(g, "sender", None)
    print("🟡 [TOOL] telefono detectado:", repr(telefono), flush=True)

    # 2) Clasificar tono
    try:
        tono = detectar_enfado(mensaje)
        print("🟡 [TOOL] tono clasificado:", tono, flush=True)
    except Exception as e:
        print("❌ [TOOL] Error en clasificador de tono:", e, flush=True)
        tono = "sin_ira"

    # 3) Preparar estado/motivo + respuesta
    if tono == "con_ira":
        motivo = "Reclamo con cliente enojado"
        estado = "Duda agresiva no resuelta"
        respuesta = (
            "Lamento mucho tu experiencia. He escalado tu reclamo a un asesor, "
            "quien te contactará en breve para resolverlo de forma prioritaria."
        )
    else:
        motivo = "Solicitó información sobre reclamos"
        estado = "Duda no resuelta"
        respuesta = "Te contactaremos para ayudarte con esta consulta y resolverla a la brevedad."

    # 4) Registrar en BD (si hay teléfono)
    if telefono:
        try:
            postgresql.registrar_motivo_estado(telefono, motivo=motivo, estado=estado)
            print(f"✅ [TOOL] BD OK → estado={estado} | motivo={motivo}", flush=True)
        except Exception as e:
            print("❌ [TOOL] Error guardando en BD:", e, flush=True)
    else:
        print("⚠️ [TOOL] Teléfono no disponible: no se registró en BD", flush=True)

    print("🟢 [TOOL] Respuesta final al cliente:", repr(respuesta), flush=True)
    return respuesta
# FIN NUEVOOO


# 3.6. Función para clasificar intenciones de contrato o duda
def detectar_intencion_contrato_o_duda(mensaje_usuario: str) -> str:
    system_prompt = (
        "Eres un clasificador de intenciones. Recibes un texto de un cliente y solo debes responder "
        "con una de estas dos etiquetas (sin comillas ni explicaciones adicionales):\n"
        "– gestion_contrato  (si el cliente solicita un cambio o modificación de su contrato)\n"
        "– duda              (si el cliente plantea una consulta que el asistente no puede resolver o es sobre reclamos)\n"
    )
    clasificador_model = ChatOpenAI(model="gpt-4", temperature=0.0)
    respuesta = clasificador_model.generate(
        messages=[
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=mensaje_usuario),
            ]
        ]
    )
    etiqueta = respuesta.generations[0][0].text.strip().lower()
    if etiqueta not in ("gestion_contrato", "duda"):
        raise ValueError(f"Etiqueta inesperada del modelo: '{etiqueta}'")
    return etiqueta

# 3.7. Herramienta: 'derivar_contacto_humano'

@tool(
    return_direct=True,
    description=(
        """
        OBJETIVO / ESTADO: Comunicar inmediatamente con un asesor humano (Estado = "Comunicacion inmediata")
        cuando el cliente quiere hablar rápido con alguien y muestra señales claras de INTENCIÓN DE PAGO.

        CUÁNDO USAR:
        - El cliente expresa que desea pagar ahora, confirmar monto para pagar, enviar comprobante o coordinar pago
          de forma inmediata con un humano.
        - Señales explícitas de intención de pago + urgencia por comunicarse.

        NO USAR:
        - Si pide facilidades, plazos o descuentos (usa gestionar_cambio_contrato).
        - Si es una duda que el bot/RAG puede resolver (usa busqueda_productos).
        - Si es una duda que no puedes resolver (usa aclarar_info_reclamo).
        - Si muestra intención de darse de baja o agresión fuerte sin intención de pago (usa registrar_motivo_salida).
        - Si solo expresa que ya realizó el pago y nada más.

        EJEMPLOS DISPARADORES:
        - "Quiero pagar ahora, pásenme el número de cuenta."
        - "Estoy por hacer el depósito, necesito confirmar el monto."
        - "Necesito que alguien me llame para coordinar mi pago."
        - "Ya hice el pago, ¿a quién le envío el comprobante?"

        RESPUESTA ESPERADA:
        - Confirma transferencia a un asesor humano y que lo contactarán de inmediato.
        - Estilo breve (máx. 3 oraciones), claro y profesional.

        PARÁMETROS:
        - motivo: str  (texto breve que explique la razón de la derivación)
        - (opcional) cliente_telefono: str

        EFECTO ESPERADO (ya implementado en el código):
        - Registrar en BD con estado = "Comunicacion inmediata".
        """
    ),
)
def derivar_contacto_humano(motivo: str, cliente_telefono: str = None) -> str:
    print("🛠️ [TOOL] Entró a herramienta: derivar_contacto_humano", flush=True)
    print(f"🛠️ [TOOL] motivo={motivo!r}, cliente_telefono={cliente_telefono!r}", flush=True)

    #tipo = detectar_intencion_contrato_o_duda(motivo or "")
    #print(f"🛠️ [TOOL] intención clasificada={tipo}", flush=True)

    # número desde payload o desde g.sender
    cliente_telefono = cliente_telefono or g.sender
    print(f"🛠️ [TOOL] usaremos telefono={cliente_telefono!r}", flush=True)

    # Guardar motivo y estado en la base de datos
    if cliente_telefono:
        try:
            postgresql.registrar_motivo_estado(cliente_telefono, motivo, "Comunicacion inmediata")
        except Exception as e:
            print(f"❌ Error al registrar en BD: {e}", flush=True)

    mensaje = "En breve te llamará nuestro Contact Center al número asociado para aclararte tus pedidos"
    return mensaje




# 3.8. Herramienta: 'registrar_motivo_salida'
@tool(
    "registrar_motivo_salida",
    description=(
        """
        OBJETIVO / ESTADO: Registrar que el cliente quiere darse de baja / no continuar, mostrando enojo
        o frustración significativa (Estado = "Enojado"). No hay señales de intención de pagar ni de renegociar.

        CUÁNDO USAR:
        - Cliente molesto o sarcástico con quejas directas, amenazas o insultos.
        - Rechaza pagar o expresa explícitamente que no continuará.

        NO USAR:
        - Si hay intención de pagar con urgencia y contacto humano (usa derivar_contacto_humano).
        - Si pide facilidades de pago (usa gestionar_cambio_contrato).
        - Si es una duda/consulta que podría resolverse (usa busqueda_productos o aclarar_info_reclamo).

        EJEMPLOS DISPARADORES:
        - "No me parece justo lo que están haciendo."
        - "Esto es una porquería, no sirven para nada."
        - "Ustedes son unos estafadores, voy a denunciarlos."
        - "Voy a ir a su oficina a romper todo."
        - "Son unos ladrones, no pienso pagar nada."

        RESPUESTA ESPERADA:
        - Confirmar registro del motivo de salida y cerrar con tono profesional y breve (máx. 3 oraciones).

        PARÁMETROS:
        - JSON con: { "cliente_telefono": str, "motivo_salida": str (opcional) }
          • Si falta "motivo_salida", pedirlo brevemente.

        EFECTO ESPERADO (ya implementado en el código):
        - Registrar en BD con estado = "Enojado".
        """
    ),
)
def registrar_motivo_salida(payload: str) -> str:
    print("🛠️ [TOOL] >>> Entró a registrar_motivo_salida", flush=True)
    print("🛠️ [TOOL] >>> Payload raw:", repr(payload), flush=True)

    data = {}
    motivo_salida = None
    telefono = None

    # 1) Parseo tolerante
    try:
        data = json.loads(payload)
        print("🟢 [TOOL] JSON parseado OK:", data, flush=True)
        if not isinstance(data, dict):
            print("⚠️ [TOOL] JSON no es objeto; usaré payload como motivo.", flush=True)
            motivo_salida = (payload or "").strip()
        else:
            motivo_salida = (data.get("motivo_salida") or "").strip()
            telefono = (data.get("cliente_telefono") or "").strip()
    except Exception as e:
        print("⚠️ [TOOL] No es JSON. Usaré payload como motivo_salida. Error:", e, flush=True)
        motivo_salida = (payload or "").strip()

    print(f"🟡 [TOOL] motivo_salida={motivo_salida!r}", flush=True)
    if not motivo_salida:
        print("🔴 [TOOL] motivo_salida vacío → solicitar motivo", flush=True)
        return (
            "Veo que deseas darte de baja, pero no conozco el motivo. "
            "¿Podrías indicarme brevemente por qué deseas salir de la empresa?"
        )

    # 2) Teléfono (payload → fallback g.sender)
    telefono = telefono or getattr(g, "sender", None)
    print(f"🟡 [TOOL] telefono detectado={telefono!r}", flush=True)

    # 3) Registrar en BD
    if telefono:
        try:
            postgresql.registrar_motivo_estado(telefono, motivo_salida, "Enojado")
            print("✅ [TOOL] BD OK → estado=Enojado | motivo=", motivo_salida, flush=True)
        except Exception as e:
            print("❌ [TOOL] Error guardando en BD:", e, flush=True)
    else:
        print("⚠️ [TOOL] Teléfono no disponible: no se registró en BD", flush=True)

    # 4) Respuesta final
    respuesta = (
        f"👍 He registrado tu motivo de salida: “{motivo_salida}”.\n"
        "Lamentamos que nos dejes y agradecemos que hayas compartido la razón. "
        "Si en el futuro cambias de opinión o necesitas algún trámite, aquí estaremos."
    )
    print("🟢 [TOOL] Respuesta final al cliente:", repr(respuesta), flush=True)
    return respuesta


# 3.9 registrar_no_interado
@tool(
    "registrar_no_interado",
    description=(
        """
        OBJETIVO / ESTADO: Registrar que el cliente NO está interesado (Estado = "No interesado").
        Caso recuperable: rechazo o molestia leve/moderada **sin insultos/amenazas**.
        No hay señales de intención de pagar ni de renegociar.

        CUÁNDO USAR:
        - Mensajes como "no estoy interesado", "por ahora no", "no quiero continuar",
        "no deseo seguir", "no me interesa", "me parece caro" (sin insultos).

        NO USAR:
        - Si hay insultos/amenazas o hostilidad alta → usar registrar_motivo_salida.
        - Si pide facilidades/alternativas → usar gestionar_cambio_contrato.
        - Si quiere pagar ya con un humano → usar derivar_contacto_humano.
        - Si es una duda factual → usar busqueda_productos o aclarar_info_reclamo.
        """
    ),
)
def registrar_no_interado(payload: str) -> str:
    print("🛠️ [TOOL] >>> Entró a registrar_no_interado", flush=True)
    print("🛠️ [TOOL] >>> Payload raw:", repr(payload), flush=True)

    data = {}
    motivo = None
    telefono = None

    # 1) Parseo tolerante
    try:
        data = json.loads(payload)
        print("🟢 [TOOL] JSON parseado OK:", data, flush=True)
        if not isinstance(data, dict):
            print("⚠️ [TOOL] JSON no es objeto; usaré payload como motivo.", flush=True)
            motivo = (payload or "").strip()
        else:
            # Acepta varias claves posibles para compatibilidad
            motivo = (
                data.get("motivo")
                or data.get("motivo_no_interes")
                or data.get("motivo_salida")
                or ""
            ).strip()
            telefono = (data.get("cliente_telefono") or "").strip()
    except Exception as e:
        print("⚠️ [TOOL] No es JSON. Usaré payload como motivo. Error:", e, flush=True)
        motivo = (payload or "").strip()

    print(f"🟡 [TOOL] motivo={motivo!r}", flush=True)
    if not motivo:
        print("🔴 [TOOL] motivo vacío → solicitar motivo", flush=True)
        return "Entiendo. ¿Podrías indicarme brevemente por qué no estás interesado?"

    # 2) Teléfono (payload → fallback g.sender)
    telefono = telefono or getattr(g, "sender", None)
    print(f"🟡 [TOOL] telefono detectado={telefono!r}", flush=True)

    # 3) Registrar en BD
    estado = "No interesado"
    if telefono:
        try:
            postgresql.registrar_motivo_estado(telefono, motivo, estado)
            print(f"✅ [TOOL] BD OK → estado={estado} | motivo={motivo}", flush=True)
        except Exception as e:
            print("❌ [TOOL] Error guardando en BD:", e, flush=True)
    else:
        print("⚠️ [TOOL] Teléfono no disponible: no se registró en BD", flush=True)

    # 4) Respuesta final
    respuesta = (
        "He registrado que no estás interesado en continuar. "
        "Si cambias de opinión más adelante, escríbenos y te ayudamos."
    )
    print("🟢 [TOOL] Respuesta final al cliente:", repr(respuesta), flush=True)
    return respuesta




# --------------------------------------------------------------------------
# 4) CALLBACK PARA ENRIQUECER EL PAYLOAD CON thread_id
# --------------------------------------------------------------------------

def enrich_payload_with_thread_id(tool, config):
    """Inserta el número de teléfono en el payload si no viene explícito."""
    try:
        payload = json.loads(tool.tool_input)
        if "cliente_telefono" not in payload:
            payload["cliente_telefono"] = g.sender
            tool.tool_input = json.dumps(payload)
    except Exception as e:
        print(f"⚠️ No se pudo enriquecer payload: {e}")
    return tool
# ------------------------------------------------------------------------------
# 5) INICIALIZACIÓN GLOBAL DEL AGENTE
# ------------------------------------------------------------------------------

# a) Instanciar managers externos
#twilio     = TwilioManager()
postgresql = DataBasePostgreSQLManager()
firestore  = DataBaseFirestoreManager()

# b) Configuración de Postgres para checkpoint en memoria del agente
DB_URI = os.environ["DB_URI"]
connection_kwargs = {"autocommit": True, "prepare_threshold": 0}
pool = ConnectionPool(conninfo=DB_URI, max_size=20, kwargs=connection_kwargs)
checkpointer = PostgresSaver(pool)

# c) Configuración de RAG (Elasticsearch + embeddings)
ELASTIC_URL      = os.environ["ELASTIC_URL"]
ELASTIC_USER     = os.environ["ELASTIC_USER"]
ELASTIC_PASSWORD = os.environ["ELASTIC_PASSWORD"]
ELASTIC_INDEX    = os.environ["ELASTIC_INDEX"]

db_query = ElasticsearchStore(
    es_url=ELASTIC_URL,
    es_user=ELASTIC_USER,
    es_password=ELASTIC_PASSWORD,
    index_name=ELASTIC_INDEX,
    embedding=OpenAIEmbeddings()
)
retriever = db_query.as_retriever()
raw_rag_tool  = retriever.as_tool(
    name="busqueda_productos",
    description=(
        "Usa esta herramienta para buscar información detallada en documentos oficiales, manuales, reglamentos "
        "o sobre el fondo colectivo. Utilízala siempre que el cliente tenga dudas específicas sobre procesos, "
        "normativas, condiciones o requiera una explicación técnica sobre su contrato o productos."
    ),
)
# 2) Definimos un wrapper que primero registra en BD y luego llama a la herramienta RAG
def rag_con_historico(input_text: str) -> str:
    print("🛠️ [TOOL] Entró a herramienta: busqueda_productos (RAG)", flush=True)
    print(f"🛠️ [TOOL] Query: {input_text}", flush=True)
    cliente_telefono = g.sender
    motivo = "Tuvo una duda específica que se pudo contestar"
    estado = "Duda resuelta"
    print("🟡 [DEBUG] Guardando en histórico antes de RAG", flush=True)
    try:
        postgresql.registrar_motivo_estado(cliente_telefono, motivo, estado)
        print(f"✅ [DEBUG] Histórico: {estado} / {motivo}", flush=True)
    except Exception as e:
        print(f"❌ [DEBUG] Error al registrar histórico en BD: {e}", flush=True)

    # 3) Finalmente llamamos a la RAG original
    return raw_rag_tool.func(input_text)

# 4) Registramos la nueva herramienta en el toolkit
consultar_informacion_rag = Tool(
    name="busqueda_productos",
    func=rag_con_historico,
    description=raw_rag_tool.description,
)
# d) Inicializar modelo y agente de LangChain
model = ChatOpenAI(model="gpt-4.1-2025-04-14")

tolkit = [
    consultar_informacion_rag,
    registrar_promesa_pago,
    gestionar_cambio_contrato,
    aclarar_info_reclamo,
    derivar_contacto_humano,
    registrar_motivo_salida,
    registrar_no_interado,
]


prompt = ChatPromptTemplate.from_messages(
    [
        ("system",
         """
        Eres un asistente virtual para gestión de contratos y pagos en Maqui+. Debes:
        - Identificar la intención y rutear a UNA herramienta cuando aplique. No inventes datos.
        - Responder en **máximo 3 oraciones** con tono empático y profesional, estilo WhatsApp.
        - Confirmar toda acción registrada (p. ej., promesa de pago o derivación).

        ### Árbol de decisión (en orden de prioridad)

        1) **Quiere pagar pero NO puede con el acuerdo actual (facilidades/plazos/descuento/cambio de producto)** → **gestionar_cambio_contrato**
        - Disparadores: "cambiar de producto", "bajar plan", "menor costo", "¿puedo pagar en dos partes?", "no puedo todo, ¿me dan plazo?", "fraccionar", "mitad hoy y mitad el próximo mes", "renegociar", "descuento", "congelar".
        - Llama: `gestionar_cambio_contrato(payload='{{"motivo":"<texto>"}}')`.
        - Estado en BD: "Negociacion de pago".

        2) **Intención de pagar inmediata + urgencia de hablar con humano** → **derivar_contacto_humano**
        - REQUISITOS: el mensaje contiene **al menos una** señal de pago inmediato en: ["pagar ahora","depósito","transferencia","número de cuenta","CIP","comprobante","voucher","enviar pago","confirmar monto", "problemas con pagos"].
        - Si también menciona términos de negociación (punto 1), **prioriza el punto 1**.
        - Llama: `derivar_contacto_humano(motivo="<texto breve>")`. (No incluyas datos sensibles.)
        - Estado en BD: "Comunicacion inmediata".

        3) **Ofrece una fecha de compromiso de pago** → **registrar_promesa_pago**
        - Formatos aceptados: `DD/MM` o `DD/MM/AAAA`; si `DD/MM`, la herramienta normaliza el año; si ya pasó, salta al próximo año.
        - Llama: `registrar_promesa_pago(payload='{{"cliente_telefono":"<auto>","fecha_promesa":"<DD/MM o DD/MM/AAAA>"}}')`.
        - Estado en BD: "Promesa de pago".
        - Si el usuario menciona fecha sin año (DD/MM), pásala tal cual; **no completes el año** tú.

        4) **Duda/consulta informativa** → intenta **RAG** primero (**busqueda_productos**).
        - Si puedes resolver con documentos, usa RAG; confirma y responde en 1–3 oraciones (el wrapper ya registra "Duda resuelta").
        - Si NO puedes resolver o la pregunta excede RAG → **aclarar_info_reclamo**:
            - Llama: `aclarar_info_reclamo(payload='{{"mensaje":"<texto del usuario>"}}')`.
            - La herramienta clasifica el tono:
                • Neutral → "Duda no resuelta"
                • Agresivo/ofensivo → "Duda agresiva no resuelta"
        - Si el cliente dice que "le prometieron el carro este mes", "le mintieron", o "debería tener su carro ya" o " dice que no puede adjudicar desde tal mes":
            • Responde explicando brevemente que la adjudicación depende de un sorteo.
            • Si el cliente insiste o se muestra molesto, usa `aclarar_info_reclamo(payload='{{"mensaje":"<texto del usuario>"}}')`.
            • Estado en BD: "Duda no resuelta" (o "Duda agresiva no resuelta" si hay tono ofensivo).
        - Si el cliente pregunta o se queja sobre: cuándo le entregarán el auto,por qué le siguen llegando mensajes, o siente que “ya debería tener el auto”:
            • Explica que los mensajes son informativos y sirven para mostrarle su progreso.
            • Aclara que la entrega del auto depende del sorteo.
            • Indica que el tiempo puede variar y no es una fecha fija.
            • Reafirma que el sorteo es la única forma de adjudicación.
            • Mantén el tono claro, respetuoso y sin prometer plazos.
        - Si el cliente pregunta por el premio de la “Ruleta ganadora” o “premio del mes”:
            • Responde que los premios pueden ser **electrodomésticos**.
            • Aclara que los premios varían según el mes.
            • Si el cliente pide un ejemplo, menciona **freidora de aire** como ejemplo.
            • Mantén la respuesta breve y sin prometer un premio específico.
            • NO generes expectativa de adjudicación directa.

        5) **Salida/enojo fuerte sin intención de pagar (irrecuparable)** → **registrar_motivo_salida**
        - Disparadores (agresión/amenazas/insultos o hostilidad alta): "estafadores", "ladrones", "ratas", "los voy a denunciar", "Indecopi", "romper", "nunca más", mayúsculas agresivas, insultos directos.
        - Anti-disparadores: si pide facilidades, descuento o alternativa → usa 1) gestionar_cambio_contrato.
        - Llama: `registrar_motivo_salida(payload='{{"motivo_salida":"<texto>"}}')`.
        - Si falta motivo, llama igual para que lo solicite.
        - Estado en BD: "Enojado".
        - Regla de desempate: si el mensaje contiene insultos/amenazas **siempre** prioriza este punto sobre cualquier otra opción.

        6) **No está interesado en continuar (rechazo recuperable, SIN insultos)** → **registrar_no_interado**
        - Disparadores (rechazo educado o molestia leve/moderada): "no estoy interesado", "déjalo ahí", "no quiero continuar", "no deseo seguir", "no me interesa", "por ahora no", "me parece caro", "no veo beneficio", quejas sin insultos.
        - Anti-disparadores:
            • Si menciona facilidades o querer pagar con cambios → usa 1) gestionar_cambio_contrato.  
            • Si quiere pagar ahora o hablar con humano para pagar → usa 2) derivar_contacto_humano.  
            • Si es una duda factual → usa 4) busqueda_productos; si no resuelve, 4) aclarar_info_reclamo.
        - Llama: `registrar_no_interado(payload='{{"motivo":"<texto breve>"}}')`.
        - Estado en BD: "No interesado".

        7) **Si el mensaje no encaja en lo anterior**:
        - Si es una pregunta factual → RAG.
        - Si es ambiguo, pide una aclaración **en una sola oración**.
        - Si excede el alcance, deriva con **aclarar_info_reclamo**.

        ### Formato y privacidad
        - No menciones nombres de herramientas en la respuesta al cliente; solo confirma la acción (“Te contactará un asesor…”, etc.).
        - **Nunca incluyas etiquetas/estados internos** como “Duda resuelta”, “Negociacion de pago”, “Comunicacion inmediata”, “No interesado”, “Enojado”, etc. en el mensaje al cliente. Esos estados son solo para registro interno.
        - No compartas datos sensibles (DNI/correo/cuentas). No pidas comprobantes por este canal; deriva a humano si hace falta.
        - Mantén las respuestas en 1–3 oraciones, claras y específicas.
        - No intentes extender la conversación innecesariamente.
        - Si ya resolviste la duda, o ya confirmaste una acción registrada (promesa de pago, derivación, no interesado, salida, etc.), CIERRA el mensaje.
        - NO utilices palabras de cierre fijas o repetitivas como “Listo”, “Perfecto”, “Genial” de forma automática.
        - En esos casos, NO hagas preguntas de seguimiento ni pidas más información.
        - Cierra con una frase breve de disponibilidad, estilo WhatsApp, por ejemplo:
            “Si deseas, cuando gustes me escribes y te apoyo.”
            “Perfecto, quedo atento(a). Cuando gustes me escribes.”
        - Solo haz una pregunta si el mensaje del usuario fue ambiguo o falta un dato mínimo indispensable para rutear a una herramienta.

        ### Herramientas disponibles
        - busqueda_productos (RAG) → dudas técnicas/procesos/condiciones; si resuelve, “Duda resuelta”.
        - registrar_promesa_pago (payload JSON con `cliente_telefono`, `fecha_promesa`).
        - gestionar_cambio_contrato (payload JSON con `motivo`).
        - registrar_no_interado (payload JSON con `motivo`, opcional `cliente_telefono`).
        - registrar_motivo_salida (payload JSON con `motivo_salida`, opcional `cliente_telefono`).
        - derivar_contacto_humano (motivo: str, cliente_telefono opcional).
        - aclarar_info_reclamo (payload JSON con `mensaje`).

        Comienza con una pregunta breve y directa de ayuda solo si el usuario aún no ha planteado intención clara.

        ### Ejemplo
         """
        ),
        ("human", "{messages}"),
    ]
)




# Primero creas el “agent” normal
agent_executor = create_react_agent(
    model=model,
    tools=tolkit,
    checkpointer=checkpointer,
    prompt=prompt
)

# ------------------------------------------------------------------------------
# 5) FLASK APP Y ROUTE /hello (Webhook de Twilio)
# ------------------------------------------------------------------------------

app = Flask(__name__)

def _extract_messages(cp):
    return (((cp or {}).get("checkpoint") or {}).get("channel_values") or {}).get("messages") or []

def _ensure_message_ids(items):
    """Devuelve SOLO dicts y fuerza que cada uno tenga 'id'."""
    fixed = []
    for m in (items or []):
        if isinstance(m, dict):
            if not m.get("id"):
                m = {**m, "id": f"legacy-{uuid4().hex}"}
            fixed.append(m)
    return fixed

def _to_msg_dicts_any(prev):
    """
    Normaliza cualquier cosa (dicts, BaseMessage, strings) a lista de dicts
    con 'id','type','content'. Genera id si falta.
    """
    out = []
    for m in (prev or []):
        # 1) BaseMessage (LangChain)
        if isinstance(m, BaseMessage):
            d = messages_to_dict([m])[0]
            if not d.get("id"):
                d["id"] = f"legacy-{uuid4().hex}"
            out.append(d)
            continue

        # 2) dict (posible formato heterogéneo)
        if isinstance(m, dict):
            d = dict(m)
            # compatibilidad con claves comunes
            d["content"] = d.get("content") or d.get("text") or d.get("body") or ""
            d["type"]    = d.get("type") or d.get("role") or ("ai" if d.get("from_ai") else "human")
            d["id"]      = d.get("id") or f"legacy-{uuid4().hex}"
            out.append(d)
            continue

        # 3) string suelto → asumimos humano
        if isinstance(m, str):
            out.append({"type": "human", "content": m, "id": f"legacy-{uuid4().hex}"})
            continue

        # 4) cualquier otro tipo lo ignoramos silenciosamente
    return out

def migrate_campaign_seed_to_default(thread_id: str) -> int:
    # 1) leer seeds
    cfg_seed = {"configurable": {"thread_id": thread_id, "checkpoint_ns": CHECKPOINT_NS}}
    seed_state = checkpointer.get(cfg_seed)
    seed_msgs  = _ensure_message_ids(_extract_messages(seed_state))
    if not seed_msgs:
        return 0

    # 2) leer default
    cfg_def   = {"configurable": {"thread_id": thread_id}}
    def_state = checkpointer.get(cfg_def)
    def_cp    = (def_state or {}).get("checkpoint") or {}
    def_id    = def_cp.get("id") or f"cp-{uuid4().hex}"
    def_msgs  = _ensure_message_ids(_extract_messages(def_state))

    # 3) merge (append seeds al final, o al inicio si prefieres)
    merged = def_msgs + seed_msgs

    # 4) escribir en DEFAULT
    msg_ver = str(uuid4())
    checkpoint_new = {
        "v": 4,
        "id": def_id,
        "channel_values": {"messages": merged},
        "channel_versions": {"messages": msg_ver},
    }
    new_versions = {"channel_values": {"messages": msg_ver}}
    checkpointer.put(cfg_def, checkpoint_new, {"source": "migrated_campaign_seed"}, new_versions)

    # 5) (opcional) vaciar el namespace de seeds para no re-migrar
    empty_ver = str(uuid4())
    cp_empty  = {"v": 4, "id": (seed_state or {}).get("checkpoint", {}).get("id") or f"cp-{uuid4().hex}",
                 "channel_values": {"messages": []},
                 "channel_versions": {"messages": empty_ver}}
    checkpointer.put(cfg_seed, cp_empty, {"migrated": True}, {"channel_values": {"messages": empty_ver}})

    return len(seed_msgs)

@app.route("/seed-campaign-memory", methods=["POST"])
def seed_campaign_memory():
    """
    Recibe seeds del CRM y los guarda en el checkpointer del hilo f-<phone>.
    Body:
      {
        "mode": "append" | "replace",
        "entries": [
          { "phone": "51987654321", "text": "mensaje ya renderizado", "role": "ai" }
        ]
      }
    """
    if not request.is_json:
        print("[SEED] ❌ request no es JSON", flush=True)
        return Response("Bad Request", status=400)

    payload = request.get_json() or {}
    mode    = (payload.get("mode") or "append").lower()
    entries = payload.get("entries") or []

    print(f"[SEED] ▶️ inicio | mode={mode} | entries={len(entries)}", flush=True)
    if not entries:
        print("[SEED] ⚠️ entries vacío", flush=True)
        return Response("Empty entries", status=400)

    ok = fail = 0

    for idx, e in enumerate(entries, start=1):
        try:
            phone = str(e.get("phone") or "").strip().lstrip("+")
            text  = str(e.get("text") or "").strip()
            role  = (e.get("role") or "ai").lower()

            snippet = (text.replace("\n", " ")[:140] + ("…" if len(text) > 140 else ""))
            if not phone or not text:
                print(f"[SEED][{idx}] ⚠️ descartado | phone={phone!r} | vacío text/phone", flush=True)
                fail += 1
                continue

            thread_id = f"f-{phone}"
            config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": CHECKPOINT_NS,
                }
            }
            print(f"[SEED][{idx}] 📥 recibido | thread={thread_id} | role={role} | len(text)={len(text)} | text='{snippet}'", flush=True)

            # ---------- 1) Leer historial ----------
            before_count = 0
            merged_msg_dicts = []
            checkpoint_id = None
            try:
                # ---------- 1) Leer historial ----------
                existing = checkpointer.get(config)
                prev = None
                if existing and existing.get("checkpoint"):
                    cp = existing["checkpoint"]
                    # guarda el id existente si lo hay
                    checkpoint_id = cp.get("id")
                    # preferir channel_values si existe
                    prev = (cp.get("channel_values") or {}).get("messages") or cp.get("messages")

                prev_norm = _to_msg_dicts_any(prev)
                prev_norm = _ensure_message_ids(prev_norm)
                merged_msg_dicts = list(prev_norm)  # copia
                before_count = len(merged_msg_dicts)
                print(f"[SEED][{idx}] 🧾 historial | before={before_count}", flush=True)
            except Exception as ex_get:
                # print(f"[SEED][{idx}] ⚠️ no pude leer historial: {type(ex_get).__name__}: {ex_get!r}", flush=True)
                # print("[SEED] Traceback (get):\n" + traceback.format_exc(), flush=True)
                # fila vieja sin v → lo tratamos como historial vacío
                print(f"[SEED][{idx}] (info) historial vacío o legacy sin 'v' para {thread_id}: {type(ex_get).__name__}", flush=True)
                existing = None

            # ---------- 2) Agregar el nuevo mensaje (siempre con id) ----------
            msg_id = f"seed-{uuid4().hex}"
            msg_obj = HumanMessage(content=text, id=msg_id) if role == "human" else AIMessage(content=text, id=msg_id)
            new_msg = messages_to_dict([msg_obj])[0]
            if not new_msg.get("id"):
                new_msg["id"] = msg_id
            merged_msg_dicts.append(new_msg)

            # Sanitiza TODO antes de guardar (garantiza 'id' en todos)
            merged_msg_dicts = _ensure_message_ids(merged_msg_dicts)
            after_count = len(merged_msg_dicts)

            metadata = {"source": "campaign_seed", "role": role}
            
            try:
                if not checkpoint_id:
                    checkpoint_id = f"cp-{uuid4().hex}"
                checkpoint_new   = {"v": 4, "id": checkpoint_id, "channel_values": {"messages": merged_msg_dicts}}
                new_versions_new = {"channel_values": {"messages": str(uuid4())}}
                checkpointer.put(config, checkpoint_new, metadata, new_versions_new)
                print(
                    f"[SEED][{idx}] ✅ guardado (nuevo) | thread={thread_id} "
                    f"| before={before_count} -> after={after_count} | msg_id={msg_id}",
                    flush=True,
                )
                ok += 1
            except Exception as ex_new:
                print(
                    f"[SEED][{idx}] ❌ error final (nuevo) | "
                    f"type={type(ex_new).__name__} | msg={str(ex_new)!r} | repr={repr(ex_new)} | "
                    f"cause={repr(getattr(ex_new, '__cause__', None))} | "
                    f"context={repr(getattr(ex_new, '__context__', None))}",
                    flush=True,
                )
                print("[SEED] Traceback (nuevo):\n" + traceback.format_exc(), flush=True)
                fail += 1

        except Exception as ex:
            print(
                f"[SEED][{idx}] ❌ error inesperado | type={type(ex).__name__} | msg={str(ex)!r}",
                flush=True,
            )
            print("[SEED] Traceback (outer):\n" + traceback.format_exc(), flush=True)
            fail += 1

    print(f"[SEED] ⏹ fin | ok={ok} | fail={fail}", flush=True)
    return Response(json.dumps({"ok": ok, "fail": fail}), status=200, mimetype="application/json")

@app.route("/hello", methods=["GET", "POST"])
def main():
    # --- Verificación de webhook (Meta) ---
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            # Microajuste 1: devolver texto plano
            return Response(challenge, status=200, mimetype="text/plain")
        return "Error de verificación", 403

    # --- Eventos entrantes (Meta -> POST JSON) ---
    if request.is_json:
        data = request.get_json()

        # =====================================================
        # 🔴 LOG RAW DEL WEBHOOK (PRIMERA COSA) - FIDELIZACION
        # =====================================================
        try:
            change = (data.get("entry") or [{}])[0].get("changes", [{}])[0]
            value = (change.get("value", {}) or {})

            event_type = "unknown"
            if value.get("messages"):
                event_type = "message"
            elif value.get("statuses"):
                event_type = "status"

            # guarda payload RAW
            postgresql.registrar_webhook_log(
                event_type=event_type,
                payload=data
            )
        except Exception as e:
            print(f"⚠️ [WEBHOOK_LOG] error guardando payload: {e}", flush=True)
        # =====================================================
        
        try:
            change = data["entry"][0]["changes"][0]
            value = change.get("value", {})

            # --- NUEVO: captura de estados de entrega/lectura/falla ---
            statuses = value.get("statuses", [])
            if statuses:
                for st in statuses:
                    id_msg       = st.get("id")
                    status       = st.get("status")          # sent | delivered | read | failed
                    ts_unix      = st.get("timestamp")       # string (segundos)
                    recipient_id = st.get("recipient_id")
                    pricing      = st.get("pricing")
                    conversation = st.get("conversation")
                    errors       = st.get("errors")

                    # 1) upsert del envío (por si lo mandó el CRM y no este bot)
                    try:
                        postgresql.registrar_mensaje_out(
                            id_msg=id_msg,
                            phone_to=recipient_id or "",
                            template_name="desconocida",   # status no trae nombre de plantilla
                            template_lang="",
                            campanha_id=None
                        )
                    except Exception as e:
                        print(f"[STATUS] warn upsert mensaje_out: {e}", flush=True)

                    # 2) insertar evento de estado
                    try:
                        postgresql.registrar_status_event(
                            id_msg=id_msg,
                            estado=status,
                            ts_unix=int(ts_unix) if ts_unix else None,
                            recipient_id=recipient_id,
                            pricing_json=json.dumps(pricing) if pricing else None,
                            conversation_json=json.dumps(conversation) if conversation else None,
                            errors_json=json.dumps(errors) if errors else None
                        )
                    except Exception as e:
                        print(f"[STATUS] error insert status_event: {e}", flush=True)

                # Importante: responder 200 para que Meta no reintente
                return Response("EVENT_RECEIVED", status=200)
            # --- FIN NUEVO ---

            metadata = value.get("metadata", {})
            print("[META] event phone_number_id=", metadata.get("phone_number_id"),
      " | code PHONE_NUMBER_ID=", PHONE_NUMBER_ID, flush=True)
            messages = value.get("messages", [])
            if not messages:
                return Response("EVENT_RECEIVED", status=200)  # statuses u otros eventos
            msg = messages[0]
            incoming_msg = (msg.get("text") or {}).get("body", "")  # solo textos
            sender = msg.get("from", "")  # número del cliente
        except Exception as e:
            print(f"❌ No pude parsear JSON de Meta: {e} | data={data}", flush=True)
            # Microajuste 2: responder 200 para que Meta no reintente en bucle
            return Response("EVENT_RECEIVED", status=200)
    else:
        # (fallback por si aún llega algo de Twilio durante la transición)
        incoming_msg = request.form.get("Body", "").strip()
        sender       = request.form.get("From", "").replace("whatsapp:", "").strip()

    # guardar el sender para herramientas
    g.sender = sender
    print("🟢 [DEBUG] Mensaje recibido:", incoming_msg, flush=True)
    print("🟢 [DEBUG] Número de sender:", sender, flush=True)

    # Firestore (igual que antes)
    try:
        firestore.crear_documento(sender, None, "fidelizacion", incoming_msg, True)
    except Exception as e:
        print(f"❌ Error al escribir en Firestore: {e}")

    # Invocar agente (detalle: pasar " " si llega vacío)
    try:
        try:
            thread_id = f"f-{sender}"
            migradas = migrate_campaign_seed_to_default(thread_id)  # ← esta función la definiste antes
            print(f"[BOOT] seeds migradas al default: {migradas}", flush=True)
        except Exception as e:
            print(f"[BOOT] migración seeds falló: {e}", flush=True)
        config = {"configurable": {"thread_id": thread_id}}
        result = agent_executor.invoke(
            {"messages": [HumanMessage(content=(incoming_msg or " "))]},
            config=config
        )
        response_text = result["messages"][-1].content
    except Exception as e:
        print(f"⚠️ Error al invocar agente: {e}")
        response_text = "Lo siento, hubo un problema al procesar tu solicitud. Inténtalo más tarde."

    # Guardar respuesta y enviar por Cloud API (mismo objeto 'twilio')
    try:
        firestore.crear_documento(sender, None, "fidelizacion", response_text, False)
    except Exception as e:
        print(f"❌ Error guardando respuesta en Firestore: {e}", flush=True)

    #twilio.send_message(sender, response_text)
    send_whatsapp(sender, response_text)
    return Response("EVENT_RECEIVED", status=200)


# ------------------------------------------------------------------------------
# 6) ARRANQUE LOCAL: Flask escucha en 0.0.0.0:8080 para Cloud Run
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
