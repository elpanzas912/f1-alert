const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const schedule = require('node-schedule');
const { Pool } = require('pg');
require('dotenv').config();

// --- Configuración de Variables de Entorno ---
const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
const TELEGRAM_CHANNEL_ID = process.env.TELEGRAM_CHANNEL_ID;
const DATABASE_URL = process.env.DATABASE_URL;
const API_URL = process.env.API_URL || 'https://backend-vuelta-rapida-production.up.railway.app/api/races';
const API_DAYS_AHEAD = parseInt(process.env.API_DAYS_AHEAD, 10) || 90;
const NOTIFICATION_LEAD_HOURS = parseInt(process.env.NOTIFICATION_LEAD_HOURS, 10) || 8;
const CHECK_INTERVAL_HOURS = parseInt(process.env.CHECK_INTERVAL_HOURS, 10) || 4;
const F1_CATEGORY_ID = process.env.F1_CATEGORY_ID || 'f1';

// --- Validación de Variables Críticas ---
if (!TELEGRAM_TOKEN || !TELEGRAM_CHANNEL_ID || !DATABASE_URL) {
    console.error('Error: Faltan variables de entorno críticas (TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID, o DATABASE_URL).');
    process.exit(1);
}

// --- Configuración de la Base de Datos (PostgreSQL) ---
const pool = new Pool({
    connectionString: DATABASE_URL,
    ssl: {
        rejectUnauthorized: false // Necesario para conexiones a Railway
    }
});

// --- Configuración del Bot de Telegram ---
// Usamos 'polling' para desarrollo y pruebas. Railway usará el Procfile para ejecutarlo como un 'worker'.
const bot = new TelegramBot(TELEGRAM_TOKEN, { polling: true });

console.log('Bot iniciado. Conectando a la base de datos...');

// --- Lógica de la Base de Datos ---

/**
 * Crea la tabla para guardar los IDs de las sesiones programadas si no existe.
 */
async function setupDatabase() {
    const client = await pool.connect();
    try {
        await client.query(`
            CREATE TABLE IF NOT EXISTS scheduled_sessions (
                session_id VARCHAR(255) PRIMARY KEY,
                scheduled_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        `);
        console.log('Tabla de la base de datos verificada/creada con éxito.');
    } catch (err) {
        console.error('Error al configurar la base de datos:', err);
        process.exit(1); // Salir si no podemos configurar la DB
    } finally {
        client.release();
    }
}

/**
 * Verifica si un ID de sesión ya ha sido programado.
 * @param {string} sessionId El ID de la sesión.
 * @returns {Promise<boolean>} True si ya está programado, false en caso contrario.
 */
async function isSessionScheduled(sessionId) {
    const client = await pool.connect();
    try {
        const res = await client.query('SELECT 1 FROM scheduled_sessions WHERE session_id = $1', [sessionId]);
        return res.rowCount > 0;
    } finally {
        client.release();
    }
}

/**
 * Guarda un ID de sesión en la base de datos para marcarlo como programado.
 * @param {string} sessionId El ID de la sesión.
 */
async function markSessionAsScheduled(sessionId) {
    const client = await pool.connect();
    try {
        await client.query('INSERT INTO scheduled_sessions (session_id) VALUES ($1) ON CONFLICT (session_id) DO NOTHING', [sessionId]);
    } finally {
        client.release();
    }
}

// --- Lógica de la API de F1 ---

/**
 * Obtiene las carreras de F1 desde la API.
 * @returns {Promise<Array>} Una lista de carreras.
 */
async function obtenerCarrerasF1() {
    const fechaInicio = new Date();
    const fechaFin = new Date();
    fechaFin.setDate(fechaFin.getDate() + API_DAYS_AHEAD);

    const params = {
        minDate: fechaInicio.getTime(),
        maxDate: fechaFin.getTime(),
    };

    try {
        const response = await axios.get(API_URL, { params, timeout: 15000 });
        const carreras = response.data?.races || [];
        return carreras.filter(c => c.categoryId === F1_CATEGORY_ID);
    } catch (error) {
        console.error('Error al contactar la API de F1:', error.message);
        return [];
    }
}

// --- Lógica de Programación de Notificaciones ---

/**
 * Envía una notificación al canal de Telegram.
 * @param {string} mensaje El mensaje a enviar.
 */
function enviarNotificacion(mensaje) {
    bot.sendMessage(TELEGRAM_CHANNEL_ID, mensaje, { parse_mode: 'Markdown' })
        .then(() => console.log('Notificación enviada con éxito.'))
        .catch(err => console.error('Error al enviar notificación:', err.message));
}

/**
 * Programa los avisos para una sesión específica.
 * @param {object} sesion El objeto de la sesión.
 * @param {string} nombreEvento El nombre del evento de F1.
 */
function programarAvisosParaSesion(sesion, nombreEvento) {
    const { id: sesionId, name: nombreSesion, startAt } = sesion;
    const fechaHoraInicio = new Date(startAt);

    // 1. Programar aviso de X horas antes
    const fechaAvisoPrevio = new Date(fechaHoraInicio.getTime() - NOTIFICATION_LEAD_HOURS * 60 * 60 * 1000);
    if (fechaAvisoPrevio > new Date()) {
        const mensajePrevio = `🏎️ *¡Atención!* La sesión **${nombreSesion}** de **${nombreEvento}** comienza en ${NOTIFICATION_LEAD_HOURS} horas (a las ${fechaHoraInicio.toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit', timeZone: 'America/Argentina/Buenos_Aires' })} hs del ${fechaHoraInicio.toLocaleDateString('es-AR')}).`;
        schedule.scheduleJob(fechaAvisoPrevio, () => enviarNotificacion(mensajePrevio));
        console.log(`Aviso de ${NOTIFICATION_LEAD_HOURS}h programado para '${nombreSesion}' el ${fechaAvisoPrevio.toLocaleString()}`);
    }

    // 2. Programar aviso de inicio
    if (fechaHoraInicio > new Date()) {
        const mensajeInicio = `🟢 *¡Arrancó!* La sesión **${nombreSesion}** de **${nombreEvento}** ha comenzado.`;
        schedule.scheduleJob(fechaHoraInicio, () => enviarNotificacion(mensajeInicio));
        console.log(`Aviso de inicio programado para '${nombreSesion}' el ${fechaHoraInicio.toLocaleString()}`);
    }
}

/**
 * Revisa si hay nuevas carreras y programa los avisos.
 */
async function checkAndScheduleRaces() {
    console.log('Revisando si hay nuevas carreras para programar...');
    const carrerasF1 = await obtenerCarrerasF1();
    if (!carrerasF1.length) {
        console.log('No se encontraron carreras en la API en esta revisión.');
        return;
    }

    let nuevasSesionesProgramadas = 0;
    for (const carrera of carrerasF1) {
        const nombreEvento = carrera.completeName || 'Evento F1';
        for (const sesion of carrera.schedules) {
            if (sesion.id && new Date(sesion.startAt) > new Date()) {
                const yaProgramada = await isSessionScheduled(sesion.id);
                if (!yaProgramada) {
                    console.log(`Nueva sesión encontrada: ${sesion.name} de ${nombreEvento}. Programando avisos.`);
                    programarAvisosParaSesion(sesion, nombreEvento);
                    await markSessionAsScheduled(sesion.id);
                    nuevasSesionesProgramadas++;
                }
            }
        }
    }

    if (nuevasSesionesProgramadas > 0) {
        console.log(`Se programaron avisos para ${nuevasSesionesProgramadas} nuevas sesiones.`);
    } else {
        console.log('No hay nuevas sesiones para programar en esta revisión.');
    }
}

// --- Comandos del Bot ---

bot.onText(/\/start/, (msg) => {
    const chatId = msg.chat.id;
    const userName = msg.from.first_name;
    bot.sendMessage(chatId, `¡Hola, ${userName}! Soy el bot de notificaciones de F1. Estoy activo y revisando las carreras.`);
    console.log(`Comando /start recibido de ${userName}. Forzando revisión de carreras.`);
    checkAndScheduleRaces(); // Forzar revisión al recibir /start
});

// --- Función Principal ---

async function main() {
    await setupDatabase();

    // Programar la revisión periódica de carreras
    schedule.scheduleJob(`0 */${CHECK_INTERVAL_HOURS} * * *`, checkAndScheduleRaces);
    console.log(`Revisión periódica de carreras programada para ejecutarse cada ${CHECK_INTERVAL_HOURS} horas.`);

    // Ejecutar una primera revisión al arrancar
    console.log('Realizando primera revisión de carreras al iniciar...');
    await checkAndScheduleRaces();
}

main();
