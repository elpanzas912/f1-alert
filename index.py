import logging
import os
from datetime import datetime, timedelta

import requests
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Cargar variables de entorno desde el archivo .env ---
# Es crucial para la configuración en cualquier entorno (local o Railway)
load_dotenv()

# --- Configuración de Logging ---
# Un logging claro es vital para depurar en producción
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Variables de Configuración (leídas desde el entorno) ---
# Mover todas las configuraciones a .env hace el código más limpio y adaptable.
try:
    TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
    TELEGRAM_CHANNEL_ID = os.environ['TELEGRAM_CHANNEL_ID']
    DATABASE_URL = os.environ['DATABASE_URL']  # Para persistencia de trabajos en PostgreSQL
    WEBHOOK_URL = os.environ['WEBHOOK_URL']    # URL pública de la app en Railway
    PORT = int(os.getenv('PORT', '8080'))      # Puerto que Railway asigna

    # Configuraciones de la lógica del bot
    API_URL = os.getenv('API_URL', "https://backend-vuelta-rapida-production.up.railway.app/api/races")
    API_DAYS_AHEAD = int(os.getenv('API_DAYS_AHEAD', 90))
    NOTIFICATION_LEAD_HOURS = int(os.getenv('NOTIFICATION_LEAD_HOURS', 8))
    CHECK_INTERVAL_HOURS = int(os.getenv('CHECK_INTERVAL_HOURS', 4))
    F1_CATEGORY_ID = os.getenv('F1_CATEGORY_ID', 'f1')

except KeyError as e:
    logger.error(f"Error: La variable de entorno {e} no está definida. El bot no puede iniciar.")
    exit() # Salir si falta una variable crítica

# --- Lógica de la API ---

def obtener_carreras_f1():
    """Obtiene las carreras de F1 desde la API para los próximos días configurados."""
    fecha_inicio = datetime.now()
    fecha_fin = fecha_inicio + timedelta(days=API_DAYS_AHEAD)
    
    params = {
        'minDate': int(fecha_inicio.timestamp() * 1000),
        'maxDate': int(fecha_fin.timestamp() * 1000)
    }

    try:
        response = requests.get(API_URL, params=params, timeout=15)
        response.raise_for_status()
        datos = response.json()
        carreras = datos.get('races', [])
        # Filtrar por la categoría definida en las variables de entorno
        return [c for c in carreras if c.get('categoryId') == F1_CATEGORY_ID]
    except requests.RequestException as e:
        logger.error(f"Error al contactar la API: {e}")
        return []

# --- Funciones del Bot de Telegram ---

async def enviar_notificacion(context: ContextTypes.DEFAULT_TYPE):
    """Función genérica que envía un mensaje de notificación al canal."""
    job = context.job
    mensaje = job.data['mensaje']
    try:
        await context.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=mensaje, parse_mode='Markdown')
        logger.info(f"Notificación enviada al canal {TELEGRAM_CHANNEL_ID} (Job: {job.name})")
    except Exception as e:
        logger.error(f"Error al enviar notificación para el job {job.name}: {e}")

async def programar_avisos_para_sesion(context: ContextTypes.DEFAULT_TYPE, sesion: dict, nombre_evento: str):
    """
    Programa los dos avisos (X horas antes y al inicio) para una sesión.
    Usa el ID de la sesión para evitar duplicados, ya que `replace_existing=True` se encarga de eso.
    """
    scheduler = context.application.job_queue.scheduler
    
    sesion_id = sesion.get('id')
    nombre_sesion = sesion.get('name', 'Sesión')
    timestamp_sesion = sesion.get('startAt', 0) / 1000
    fecha_hora_inicio = datetime.fromtimestamp(timestamp_sesion)
    
    # 1. Programar aviso de X horas antes
    fecha_aviso_previo = fecha_hora_inicio - timedelta(hours=NOTIFICATION_LEAD_HOURS)
    if fecha_aviso_previo > datetime.now():
        mensaje_previo = (
            f"🏎️ *¡Atención!* La sesión **{nombre_sesion}** de **{nombre_evento}** "
            f"comienza en {NOTIFICATION_LEAD_HOURS} horas (a las {fecha_hora_inicio.strftime('%H:%M hs del %d/%m')})."
        )
        scheduler.add_job(
            enviar_notificacion,
            'date',
            run_date=fecha_aviso_previo,
            data={'mensaje': mensaje_previo},
            id=f"{sesion_id}_channel_{NOTIFICATION_LEAD_HOURS}hr",
            name=f"Notificación {NOTIFICATION_LEAD_HOURS}h antes para {nombre_sesion}",
            replace_existing=True # Evita duplicados si el job ya existe
        )

    # 2. Programar aviso de inicio
    if fecha_hora_inicio > datetime.now():
        mensaje_inicio = f"🟢 *¡Arrancó!* La sesión **{nombre_sesion}** de **{nombre_evento}** ha comenzado."
        scheduler.add_job(
            enviar_notificacion,
            'date',
            run_date=fecha_hora_inicio,
            data={'mensaje': mensaje_inicio},
            id=f"{sesion_id}_channel_start",
            name=f"Notificación de inicio para {nombre_sesion}",
            replace_existing=True # Evita duplicados
        )

async def check_for_races(context: ContextTypes.DEFAULT_TYPE):
    """
    Revisa periódicamente si hay nuevas carreras y programa los avisos.
    Ya no necesita gestionar un archivo JSON, apscheduler se encarga de la persistencia.
    """
    logger.info("Revisando si hay nuevas carreras para programar...")
    carreras_f1 = obtener_carreras_f1()
    
    if not carreras_f1:
        logger.info("No se encontraron carreras en la API en esta revisión.")
        return

    nuevas_sesiones_programadas = 0
    for carrera in carreras_f1:
        nombre_evento = carrera.get('completeName', 'Evento F1')
        for sesion in carrera.get('schedules', []):
            sesion_id = sesion.get('id')
            timestamp_sesion = sesion.get('startAt', 0) / 1000
            
            # Solo programar si la sesión es en el futuro
            if sesion_id and datetime.fromtimestamp(timestamp_sesion) > datetime.now():
                # Comprobar si ya existe un job para esta sesión para no reprogramar innecesariamente
                job_existente = context.application.job_queue.scheduler.get_job(f"{sesion_id}_channel_start")
                if not job_existente:
                    logger.info(f"Nueva sesión encontrada: {sesion.get('name')} de {nombre_evento}. Programando avisos.")
                    await programar_avisos_para_sesion(context, sesion, nombre_evento)
                    nuevas_sesiones_programadas += 1

    if nuevas_sesiones_programadas > 0:
        logger.info(f"Se programaron avisos para {nuevas_sesiones_programadas} nuevas sesiones.")
    else:
        logger.info("No hay nuevas sesiones para programar en esta revisión.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para verificar que el bot está funcionando y forzar una revisión de carreras."""
    user_name = update.effective_user.first_name
    await update.message.reply_text(f"¡Hola, {user_name}! Soy el bot de notificaciones de F1. Estoy activo y publicaré los avisos en el canal configurado.")
    logger.info(f"Comando /start recibido de {user_name}. Forzando revisión de carreras.")
    # Forzar una revisión de carreras al iniciar, para que no espere al intervalo
    context.job_queue.run_once(check_for_races, 5) # Ejecutar en 5 segundos

# --- Función Principal ---

def main():
    """Inicia el bot, el planificador y los manejadores de comandos usando webhooks."""
    
    # --- Configuración del planificador (Scheduler) con persistencia en PostgreSQL ---
    # Esto es clave para que los trabajos programados no se pierdan en Railway
    jobstores = {
        'default': SQLAlchemyJobStore(url=DATABASE_URL)
    }
    scheduler = AsyncIOScheduler(jobstores=jobstores)
    
    # --- Creación de la aplicación de Telegram ---
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # --- Integración del planificador con la aplicación ---
    # La JobQueue de la aplicación usará nuestro scheduler persistente
    application.job_queue.scheduler = scheduler

    # --- Añadir manejadores de comandos ---
    application.add_handler(CommandHandler("start", start_command))

    # --- Programar la revisión periódica de carreras ---
    # El primer chequeo se hace al recibir el comando /start
    application.job_queue.run_repeating(
        check_for_races, 
        interval=timedelta(hours=CHECK_INTERVAL_HOURS),
        name="Revisión periódica de carreras"
    )
    
    # --- Iniciar el planificador ---
    scheduler.start()
    logger.info("Planificador de tareas iniciado con persistencia.")

    # --- Iniciar el bot usando Webhooks (ideal para Railway) ---
    logger.info(f"Iniciando bot con webhook en el puerto {PORT}")
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_TOKEN, # Parte de la URL que Telegram usará para llamar al bot
        webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}"
    )
    logger.info("El bot se ha detenido.")


if __name__ == '__main__':
    main()
