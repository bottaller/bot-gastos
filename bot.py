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

# Categorías de gasto
CATEGORIAS = [
    "Servicios",
    "Comida",
    "Nafta",
    "Facultad",
    "Ropa",
    "Salidas",
    "Tarjetas",
    "Inversiones/ahorro",
    "Otro",
]

MEDIOS_PAGO = ["Efectivo", "Débito", "Crédito", "Transferencia", "Mercado Pago"]
TARJETAS = ["UALA", "GALICIA"]

ORIGENES_INGRESO = ["Sueldo Mas Melos", "Venta", "Comisión", "Changa", "Ahorro"]

# Estados de la conversación de /gasto
MONTO, CATEGORIA, DESCRIPCION, MEDIO, TARJETA = range(5)

# Estados de la conversación de /ingreso (rango distinto para no pisar los de arriba)
MONTO_ING, DESCRIPCION_ING, ORIGEN_ING, MEDIO_ING = range(10, 14)

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


def necesita_tarjeta(categoria: str, medio: str) -> bool:
    # Un gasto con tarjeta de crédito (no pagado todavía) o el pago de una
    # tarjeta (categoría "Tarjetas") necesitan saber a qué tarjeta corresponden
    # para poder calcular el saldo pendiente por tarjeta en la hoja SALDOS.
    return medio == "Crédito" or categoria == "Tarjetas"


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


# ---------------------------------------------------------------------------
# /gasto
# ---------------------------------------------------------------------------


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
    context.user_data["medio"] = medio

    categoria = context.user_data["categoria"]

    if necesita_tarjeta(categoria, medio):
        await query.edit_message_text(f"💳 Medio de pago: {medio}")
        botones = [
            [InlineKeyboardButton(t, callback_data=f"tarjeta::{i}")]
            for i, t in enumerate(TARJETAS)
        ]
        await query.message.reply_text(
            "💳 ¿Con qué tarjeta?"
            if medio == "Crédito"
            else "💳 ¿Qué tarjeta estás pagando?",
            reply_markup=InlineKeyboardMarkup(botones),
        )
        return TARJETA

    await guardar_gasto(query, context, tarjeta="")
    return ConversationHandler.END


async def recibir_tarjeta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("::")[1])
    tarjeta = TARJETAS[idx]
    await guardar_gasto(query, context, tarjeta=tarjeta)
    return ConversationHandler.END


async def guardar_gasto(query, context: ContextTypes.DEFAULT_TYPE, tarjeta: str):
    monto = context.user_data["monto"]
    categoria = context.user_data["categoria"]
    descripcion = context.user_data["descripcion"]
    medio = context.user_data["medio"]
    hoy = datetime.now().strftime("%d/%m/%Y")

    try:
        ws = get_sheet(SHEET_TAB)
        row = next_empty_row(ws, FIRST_DATA_ROW)
        ws.update(
            f"B{row}:F{row}",
            [[hoy, categoria, descripcion, monto, medio]],
        )
        if tarjeta:
            ws.update_cell(row, 8, tarjeta)  # columna H = Tarjeta

        tarjeta_linea = f"\n💳 Tarjeta: {tarjeta}" if tarjeta else ""
        await query.edit_message_text(
            f"✅ Gasto registrado:\n"
            f"📅 {hoy}\n"
            f"📂 {categoria}\n"
            f"📝 {descripcion or '(sin descripción)'}\n"
            f"💳 {medio}{tarjeta_linea}\n"
            f"💰 ${monto:,.0f}".replace(",", ".")
        )
    except Exception as e:
        logger.exception("Error al escribir en Sheets")
        await query.edit_message_text(
            f"❌ Hubo un error al guardar el gasto: {e}"
        )

    context.user_data.clear()


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Carga cancelada.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /ingreso
# ---------------------------------------------------------------------------


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
    context.user_data["origen_ing"] = origen

    await query.edit_message_text(f"🏷️ Origen: {origen}")
    botones = [
        [InlineKeyboardButton(medio, callback_data=f"medioing::{i}")]
        for i, medio in enumerate(MEDIOS_PAGO)
    ]
    await query.message.reply_text(
        "💳 ¿Con qué medio lo cobraste?",
        reply_markup=InlineKeyboardMarkup(botones),
    )
    return MEDIO_ING


async def recibir_medio_ingreso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("::")[1])
    medio = MEDIOS_PAGO[idx]

    monto = context.user_data["monto_ing"]
    descripcion = context.user_data["descripcion_ing"]
    origen = context.user_data["origen_ing"]
    hoy = datetime.now().strftime("%d/%m/%Y")

    try:
        ws = get_sheet(SHEET_TAB_INGRESOS)
        row = next_empty_row(ws, FIRST_DATA_ROW_INGRESOS)
        ws.update(
            f"B{row}:F{row}",
            [[hoy, descripcion, monto, origen, medio]],
        )
        await query.edit_message_text(
            f"✅ Ingreso registrado:\n"
            f"📅 {hoy}\n"
            f"📝 {descripcion or '(sin descripción)'}\n"
            f"🏷️ {origen}\n"
            f"💳 {medio}\n"
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
            TARJETA: [CallbackQueryHandler(recibir_tarjeta, pattern=r"^tarjeta::")],
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
            MEDIO_ING: [
                CallbackQueryHandler(recibir_medio_ingreso, pattern=r"^medioing::")
            ],
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
