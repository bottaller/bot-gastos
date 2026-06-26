import os
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Config desde variables de entorno ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Gastos Mensuales")
SHEET_TAB = os.environ.get("SHEET_TAB", "GASTOS")
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]  # contenido completo del JSON, como texto
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")  # opcional: "123456,789012"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Categorías, igual a las de CONFIG en el Excel
CATEGORIAS = [
    "Vivienda (alquiler/expensas)",
    "Servicios (luz, gas, agua, internet)",
    "Supermercado / comida",
    "Transporte / nafta",
    "Salud",
    "Educación / facultad",
    "Indumentaria",
    "Ocio / salidas",
    "Deudas / tarjetas",
    "Ahorro",
    "Otros",
]

MEDIOS_PAGO = ["Efectivo", "Débito", "Crédito", "Transferencia", "Mercado Pago"]

ORIGENES_INGRESO = ["Sueldo extra", "GS GROUP", "Changa", "Venta", "Otro"]

# Estados de la conversación de /gasto
MONTO, CATEGORIA, DESCRIPCION, MEDIO = range(4)

# Estados de la conversación de /ingreso (rango distinto para no pisar los de arriba)
MONTO_ING, DESCRIPCION_ING, ORIGEN_ING = range(4, 7)

FIRST_DATA_ROW = 7  # la hoja GASTOS arranca los datos en la fila 7
FIRST_DATA_ROW_INGRESOS = 7  # la hoja INGRESOS también arranca en la fila 7

SHEET_TAB_INGRESOS = os.environ.get("SHEET_TAB_INGRESOS", "INGRESOS")


def get_sheet(tab_name=None):
    import json

    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sh = client.open(SPREADSHEET_NAME)
    return sh.worksheet(tab_name or SHEET_TAB)


def next_empty_row(ws, first_row=FIRST_DATA_ROW):
    col_b = ws.col_values(2)  # columna B = Fecha en ambas hojas
    row = first_row
    while row <= len(col_b) + 1:
        if row > len(col_b) or not col_b[row - 1]:
            return row
        row += 1
    return row


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS.strip():
        return True
    allowed = {int(x.strip()) for x in ALLOWED_USER_IDS.split(",") if x.strip()}
    return user_id in allowed


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola! Soy tu bot de gastos e ingresos.\n\n"
        "Usá /gasto para cargar un gasto nuevo.\n"
        "Usá /ingreso para cargar un ingreso nuevo.\n"
        "Usá /cancelar para abortar una carga en curso."
    )


async def gasto_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("No tenés permiso para usar este bot.")
        return ConversationHandler.END
    await update.message.reply_text("💵 ¿Cuánto gastaste? (solo el número, ej: 4500)")
    return MONTO


async def recibir_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().replace(",", ".").replace("$", "")
    try:
        monto = float(texto)
        if monto <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Ese monto no parece válido. Mandá solo un número, ej: 4500"
        )
        return MONTO

    context.user_data["monto"] = monto

    botones = [
        [InlineKeyboardButton(cat, callback_data=f"cat::{i}")]
        for i, cat in enumerate(CATEGORIAS)
    ]
    await update.message.reply_text(
        "📂 ¿En qué categoría entra?",
        reply_markup=InlineKeyboardMarkup(botones),
    )
    return CATEGORIA


async def recibir_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("::")[1])
    categoria = CATEGORIAS[idx]
    context.user_data["categoria"] = categoria

    await query.edit_message_text(f"📂 Categoría: {categoria}")
    await query.message.reply_text(
        "📝 ¿Descripción del gasto? (mandá - si no querés poner ninguna)"
    )
    return DESCRIPCION


async def recibir_descripcion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if desc == "-":
        desc = ""
    context.user_data["descripcion"] = desc

    botones = [
        [InlineKeyboardButton(medio, callback_data=f"medio::{i}")]
        for i, medio in enumerate(MEDIOS_PAGO)
    ]
    await update.message.reply_text(
        "💳 ¿Con qué medio de pago?",
        reply_markup=InlineKeyboardMarkup(botones),
    )
    return MEDIO


async def recibir_medio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("::")[1])
    medio = MEDIOS_PAGO[idx]

    monto = context.user_data["monto"]
    categoria = context.user_data["categoria"]
    descripcion = context.user_data["descripcion"]
    hoy = datetime.now().strftime("%d/%m/%Y")

    try:
        ws = get_sheet(SHEET_TAB)
        row = next_empty_row(ws, FIRST_DATA_ROW)
        ws.update(
            f"B{row}:F{row}",
            [[hoy, categoria, descripcion, monto, medio]],
        )
        await query.edit_message_text(
            f"✅ Gasto registrado:\n"
            f"📅 {hoy}\n"
            f"📂 {categoria}\n"
            f"📝 {descripcion or '(sin descripción)'}\n"
            f"💳 {medio}\n"
            f"💰 ${monto:,.0f}".replace(",", ".")
        )
    except Exception as e:
        logger.exception("Error al escribir en Sheets")
        await query.edit_message_text(
            f"❌ Hubo un error al guardar el gasto: {e}"
        )

    context.user_data.clear()
    return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Carga cancelada.")
    return ConversationHandler.END


async def ingreso_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("No tenés permiso para usar este bot.")
        return ConversationHandler.END
    await update.message.reply_text("💵 ¿Cuánto ingresaste? (solo el número, ej: 50000)")
    return MONTO_ING


async def recibir_monto_ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().replace(",", ".").replace("$", "")
    try:
        monto = float(texto)
        if monto <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Ese monto no parece válido. Mandá solo un número, ej: 50000"
        )
        return MONTO_ING

    context.user_data["monto_ing"] = monto
    await update.message.reply_text(
        "📝 ¿Descripción del ingreso? (mandá - si no querés poner ninguna)"
    )
    return DESCRIPCION_ING


async def recibir_descripcion_ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if desc == "-":
        desc = ""
    context.user_data["descripcion_ing"] = desc

    botones = [
        [InlineKeyboardButton(origen, callback_data=f"origen::{i}")]
        for i, origen in enumerate(ORIGENES_INGRESO)
    ]
    await update.message.reply_text(
        "🏷️ ¿De dónde viene este ingreso?",
        reply_markup=InlineKeyboardMarkup(botones),
    )
    return ORIGEN_ING


async def recibir_origen_ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("::")[1])
    origen = ORIGENES_INGRESO[idx]

    monto = context.user_data["monto_ing"]
    descripcion = context.user_data["descripcion_ing"]
    hoy = datetime.now().strftime("%d/%m/%Y")

    try:
        ws = get_sheet(SHEET_TAB_INGRESOS)
        row = next_empty_row(ws, FIRST_DATA_ROW_INGRESOS)
        ws.update(
            f"B{row}:E{row}",
            [[hoy, descripcion, monto, origen]],
        )
        await query.edit_message_text(
            f"✅ Ingreso registrado:\n"
            f"📅 {hoy}\n"
            f"📝 {descripcion or '(sin descripción)'}\n"
            f"🏷️ {origen}\n"
            f"💰 ${monto:,.0f}".replace(",", ".")
        )
    except Exception as e:
        logger.exception("Error al escribir en Sheets")
        await query.edit_message_text(
            f"❌ Hubo un error al guardar el ingreso: {e}"
        )

    context.user_data.clear()
    return ConversationHandler.END


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("gasto", gasto_start)],
        states={
            MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_monto)],
            CATEGORIA: [CallbackQueryHandler(recibir_categoria, pattern=r"^cat::")],
            DESCRIPCION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_descripcion)
            ],
            MEDIO: [CallbackQueryHandler(recibir_medio, pattern=r"^medio::")],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    conv_handler_ingreso = ConversationHandler(
        entry_points=[CommandHandler("ingreso", ingreso_start)],
        states={
            MONTO_ING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_monto_ingreso)
            ],
            DESCRIPCION_ING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_descripcion_ingreso)
            ],
            ORIGEN_ING: [CallbackQueryHandler(recibir_origen_ingreso, pattern=r"^origen::")],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(conv_handler_ingreso)

    logger.info("Bot iniciado. Esperando mensajes...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
