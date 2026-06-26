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

# Estados de la conversación
MONTO, CATEGORIA, DESCRIPCION, MEDIO = range(4)

FIRST_DATA_ROW = 7  # la hoja GASTOS arranca los datos en la fila 7


def get_sheet():
    import json

    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sh = client.open(SPREADSHEET_NAME)
    return sh.worksheet(SHEET_TAB)


def next_empty_row(ws):
    col_b = ws.col_values(2)  # columna B = Fecha
    row = FIRST_DATA_ROW
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
        "👋 Hola! Soy tu bot de gastos.\n\n"
        "Usá /gasto para cargar un gasto nuevo.\n"
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
        ws = get_sheet()
        row = next_empty_row(ws)
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    logger.info("Bot iniciado. Esperando mensajes...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
