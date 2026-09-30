"""
Bot de Feedback - Gerador TRS
==============================
Coleta feedback de interfaces por ciclos via Telegram.
Áudios são salvos no Google Drive e transcritos automaticamente em português.
Tudo é organizado no Google Drive por ciclo.

Estrutura no Drive:
/FeedbackTRS/
    /Ciclo-01/
        /imagens/
        /audios/
    /Ciclo-01_feedback.txt   (textos + referências aos áudios)

Setup:
------
1. pip install -r requirements.txt
2. Copie service_account.json do bot anterior (ou gere um novo)
3. export TELEGRAM_BOT_TOKEN="seu_token"
4. python bot.py
"""

import os
import json
import logging
import tempfile
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Update, BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

RECIFE_TZ = timezone(timedelta(hours=-3))


def now() -> datetime:
    return datetime.now(tz=RECIFE_TZ)


# ─────────────────────────────────────────────
# Configuração
# ─────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TOKEN_HERE")
ALLOWED_USER_IDS = []
DRIVE_ROOT_FOLDER = "FeedbackTRS"
CYCLE_STATE_FILE = Path(__file__).parent / "ciclo_atual.json"

# No VPS atual (1,9 GiB RAM), large-v3-turbo oferece a melhor qualidade
# viável sem arriscar OOM. Em máquina com mais memória, use large-v3.
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "auto")
_WHISPER_MODEL = None
_WHISPER_MODEL_CONFIG = None
# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


# ─────────────────────────────────────────────
# Gerenciamento de ciclo
# ─────────────────────────────────────────────

def load_cycle() -> int:
    if CYCLE_STATE_FILE.exists():
        return json.loads(CYCLE_STATE_FILE.read_text()).get("ciclo", 1)
    return 1


def save_cycle(n: int):
    CYCLE_STATE_FILE.write_text(json.dumps({"ciclo": n, "atualizado": now().isoformat()}))


def cycle_folder_name(n: int) -> str:
    return f"Ciclo-{n:02d}"


def telegram_folder_name(n: int) -> str:
    return f"Ciclo-{n:02d}-Telegram"


def get_telegram_folder_id(service, root_id: str, cycle: int) -> str:
    cycle_id = get_or_create_folder(service, cycle_folder_name(cycle), root_id)
    return get_or_create_folder(service, telegram_folder_name(cycle), cycle_id)


# ─────────────────────────────────────────────
# Google Drive API
# ─────────────────────────────────────────────

def build_drive_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = Path(__file__).parent / "token.json"
    if not token_path.exists():
        raise FileNotFoundError("token.json não encontrado.")

    creds = Credentials.from_authorized_user_file(
        str(token_path), ["https://www.googleapis.com/auth/drive"]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


LIST_KWARGS = dict(includeItemsFromAllDrives=True, supportsAllDrives=True)
FILE_KWARGS = dict(supportsAllDrives=True)


def get_root_folder_id(service) -> str:
    """Encontra a pasta FeedbackTRS compartilhada com a service account."""
    query = (
        f"name='{DRIVE_ROOT_FOLDER}' "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id)", **LIST_KWARGS).execute()
    files = results.get("files", [])
    if not files:
        raise RuntimeError(
            f"Pasta '{DRIVE_ROOT_FOLDER}' não encontrada. "
            "Compartilhe-a com tr-bot-feedback@bot-feedback-trs.iam.gserviceaccount.com (Editor)."
        )
    return files[0]["id"]


def get_or_create_folder(service, name: str, parent_id: str) -> str:
    query = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id)", **LIST_KWARGS).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = service.files().create(body=meta, fields="id", **FILE_KWARGS).execute()
    log.info(f"[Drive] Pasta criada: '{name}'")
    return folder["id"]


def upload_file(service, local_path: str, filename: str, folder_id: str, mime: str = None) -> str:
    from googleapiclient.http import MediaFileUpload
    meta = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
    result = service.files().create(body=meta, media_body=media, fields="id,webViewLink", **FILE_KWARGS).execute()
    url = result.get("webViewLink", "")
    log.info(f"[Drive] Upload '{filename}' → {url}")
    return url


def append_to_feedback_file(service, root_id: str, cycle: int, entry: str) -> str:
    filename = "_chat.txt"
    tg_folder_id = get_telegram_folder_id(service, root_id, cycle)
    query = f"name='{filename}' and '{tg_folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id,webViewLink)", **LIST_KWARGS).execute()
    existing = results.get("files", [])

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp_path = tmp.name

    try:
        if existing:
            file_id = existing[0]["id"]
            url = existing[0].get("webViewLink", "")
            content = service.files().get_media(fileId=file_id).execute()
            with open(tmp_path, "wb") as f:
                f.write(content)
            with open(tmp_path, "a", encoding="utf-8") as f:
                f.write(entry)
            from googleapiclient.http import MediaFileUpload
            service.files().update(fileId=file_id, media_body=MediaFileUpload(tmp_path, mimetype="text/plain"), **FILE_KWARGS).execute()
            return url
        else:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(entry)
            return upload_file(service, tmp_path, filename, tg_folder_id, "text/plain")
    finally:
        os.unlink(tmp_path)


def get_feedback_content(service, root_id: str, cycle: int) -> str:
    tg_folder_id = get_telegram_folder_id(service, root_id, cycle)
    query = f"name='_chat.txt' and '{tg_folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id)", **LIST_KWARGS).execute()
    files = results.get("files", [])
    if not files:
        return ""
    content = service.files().get_media(fileId=files[0]["id"]).execute()
    return content.decode("utf-8") if isinstance(content, bytes) else content


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_sender_name(user) -> str:
    return user.first_name or user.username or str(user.id)


def make_entry(user, tipo: str, conteudo: str) -> str:
    ts = now().strftime("%Y-%m-%d %H:%M:%S")
    name = get_sender_name(user)
    return f"[{ts}] {name} [{tipo}]: {conteudo}\n"


def slugify(text: str) -> str:
    import re
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s_]+", "-", text)


def transcribe_audio(local_path: str) -> str:
    """Transcreve um arquivo usando faster-whisper e idioma português."""
    global _WHISPER_MODEL, _WHISPER_MODEL_CONFIG
    from faster_whisper import WhisperModel

    requested = (WHISPER_MODEL_NAME, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
    if _WHISPER_MODEL is None or _WHISPER_MODEL_CONFIG != requested:
        try:
            _WHISPER_MODEL = WhisperModel(
                WHISPER_MODEL_NAME,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
            )
        except Exception as first_error:
            if WHISPER_DEVICE == "cpu":
                raise
            log.warning(
                "Whisper não carregou em %s/%s (%s); usando CPU/int8.",
                WHISPER_DEVICE,
                WHISPER_COMPUTE_TYPE,
                first_error,
            )
            _WHISPER_MODEL = WhisperModel(
                WHISPER_MODEL_NAME,
                device="cpu",
                compute_type="int8",
            )
        _WHISPER_MODEL_CONFIG = requested
        log.info(
            "Whisper carregado: modelo=%s dispositivo=%s computação=%s idioma=pt",
            WHISPER_MODEL_NAME,
            WHISPER_DEVICE,
            WHISPER_COMPUTE_TYPE,
        )

    segments, info = _WHISPER_MODEL.transcribe(
        local_path,
        language="pt",
        beam_size=5,
        vad_filter=True,
    )
    text = " ".join(segment.text.strip() for segment in segments).strip()
    log.info(
        "Áudio transcrito: modelo=%s idioma_detectado=%s duração=%.1fs caracteres=%d",
        WHISPER_MODEL_NAME,
        getattr(info, "language", "pt"),
        getattr(info, "duration", 0.0),
        len(text),
    )
    return text or "[Nenhuma fala detectada]"


def write_transcript_file(text: str) -> str:
    """Cria um arquivo temporário UTF-8 com a transcrição."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(text + "\n")
        return tmp.name


async def send_long_text(message, text: str) -> None:
    """Envia a transcrição em partes dentro do limite do Telegram."""
    limit = 3900
    chunks = [text[i : i + limit] for i in range(0, len(text), limit)] or [""]
    await message.reply_text("Transcrição:\n\n" + chunks[0])
    for chunk in chunks[1:]:
        await message.reply_text(chunk)


async def process_audio_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tmp_path: str,
    cycle: int,
    user,
    caption: str,
    suffix: str,
    mime: str,
    original_stem: str,
) -> None:
    """Salva áudio, transcreve, salva .txt no Drive e responde no Telegram."""
    message = update.effective_message
    service = build_drive_service()
    root_id = get_root_folder_id(service)
    tg_folder_id = get_telegram_folder_id(service, root_id, cycle)

    ts = now().strftime("%Y-%m-%d_%H-%M-%S")
    sender = slugify(get_sender_name(user))
    audio_filename = f"{ts}_{sender}_{original_stem}{suffix}"
    upload_file(service, tmp_path, audio_filename, tg_folder_id, mime)

    await context.bot.send_chat_action(
        chat_id=message.chat_id,
        action="typing",
    )
    try:
        transcript = await asyncio.to_thread(transcribe_audio, tmp_path)
        transcription_status = "transcrição concluída"
    except Exception as exc:
        log.exception("[Bot] Erro ao transcrever áudio")
        transcript = f"[Falha na transcrição: {exc}]"
        transcription_status = "falha na transcrição"

    transcript_path = write_transcript_file(transcript)
    try:
        transcript_filename = f"{ts}_{sender}_{original_stem}.txt"
        upload_file(
            service,
            transcript_path,
            transcript_filename,
            tg_folder_id,
            "text/plain; charset=utf-8",
        )
    finally:
        Path(transcript_path).unlink(missing_ok=True)

    caption_suffix = f" — legenda: {caption}" if caption else ""
    entry = make_entry(
        user,
        "audio_transcrito",
        f"áudio={audio_filename}; transcrição={transcript_filename}; "
        f"status={transcription_status}{caption_suffix}",
    )
    append_to_feedback_file(service, root_id, cycle, entry)

    await message.reply_text(
        f"Áudio e transcrição salvos no {cycle_folder_name(cycle)}.\n"
        f"Status: {transcription_status}."
    )
    await send_long_text(message, transcript)


# ─────────────────────────────────────────────
# Handlers de comando
# ─────────────────────────────────────────────

async def cmd_ciclo_atual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = load_cycle()
    await update.effective_message.reply_text(f"Ciclo atual: {n} ({cycle_folder_name(n)})")


async def cmd_novo_ciclo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = load_cycle() + 1
    save_cycle(n)
    log.info(f"[Bot] Novo ciclo iniciado: {n}")
    await update.effective_message.reply_text(
        f"Ciclo {n} iniciado. Todos os feedbacks agora vão para {cycle_folder_name(n)}."
    )


async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cycle = load_cycle()
    await update.effective_message.reply_text(f"Buscando feedback do {cycle_folder_name(cycle)}...")
    try:
        service = build_drive_service()
        root_id = get_root_folder_id(service)
        content = get_feedback_content(service, root_id, cycle)
        if not content:
            await update.effective_message.reply_text(f"Nenhum feedback registrado no {cycle_folder_name(cycle)} ainda.")
            return
        lines = content.strip().splitlines()
        summary = f"Resumo {cycle_folder_name(cycle)} ({len(lines)} entradas):\n\n" + "\n".join(lines[-20:])
        if len(lines) > 20:
            summary = f"[Mostrando últimas 20 de {len(lines)} entradas]\n\n" + summary
        await update.effective_message.reply_text(summary[:4000])
    except Exception as e:
        log.error(f"[Bot] Erro no resumo: {e}")
        await update.effective_message.reply_text(f"Erro ao buscar resumo: {e}")


# ─────────────────────────────────────────────
# Handlers de mensagem
# ─────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    text = message.text or ""
    cycle = load_cycle()

    tipo = "feedback"
    text_lower = text.lower()
    if any(w in text_lower for w in ["bug", "erro", "quebrado", "não funciona", "nao funciona"]):
        tipo = "bug"
    elif any(w in text_lower for w in ["sugestão", "sugestao", "seria legal", "poderia", "deveria"]):
        tipo = "sugestao"
    elif any(w in text_lower for w in ["bom", "ótimo", "otimo", "gostei", "funciona bem"]):
        tipo = "elogio"

    log.info(f"[Bot] Texto [{tipo}] de {user.username or user.id}: '{text[:60]}'")

    try:
        service = build_drive_service()
        root_id = get_root_folder_id(service)
        entry = make_entry(user, tipo, text)
        append_to_feedback_file(service, root_id, cycle, entry)
        await message.reply_text(f"Feedback registrado no {cycle_folder_name(cycle)}.")
    except Exception as e:
        log.error(f"[Bot] Erro ao salvar texto: {e}")
        await message.reply_text(f"Erro ao salvar: {e}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    voice = message.voice
    caption = (message.caption or "").strip()
    cycle = load_cycle()

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(tmp_path)
        await process_audio_file(
            update,
            context,
            tmp_path,
            cycle,
            user,
            caption,
            suffix=".ogg",
            mime="audio/ogg",
            original_stem="voz",
        )
    except Exception as e:
        log.error(f"[Bot] Erro ao processar áudio de voz: {e}")
        await message.reply_text(f"Erro ao processar áudio: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    photo = message.photo[-1]
    caption = (message.caption or "").strip()
    cycle = load_cycle()

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(tmp_path)

        service = build_drive_service()
        root_id = get_root_folder_id(service)
        tg_folder_id = get_telegram_folder_id(service, root_id, cycle)

        ts = now().strftime("%Y-%m-%d_%H-%M-%S")
        sender = slugify(get_sender_name(user))
        img_filename = f"{ts}_{sender}.jpg"
        url = upload_file(service, tmp_path, img_filename, tg_folder_id, "image/jpeg")

        label = img_filename + (f" — {caption}" if caption else "")
        entry = make_entry(user, "imagem", label)
        append_to_feedback_file(service, root_id, cycle, entry)
        await message.reply_text(f"Imagem salva no {cycle_folder_name(cycle)}.")
    except Exception as e:
        log.error(f"[Bot] Erro ao salvar imagem: {e}")
        await message.reply_text(f"Erro ao salvar imagem: {e}")
    finally:
        os.unlink(tmp_path)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    doc = message.document
    caption = (message.caption or "").strip()
    cycle = load_cycle()
    original = doc.file_name or "documento"
    ext = Path(original).suffix.lstrip(".") or "bin"

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(tmp_path)

        service = build_drive_service()
        root_id = get_root_folder_id(service)
        tg_folder_id = get_telegram_folder_id(service, root_id, cycle)

        ts = now().strftime("%Y-%m-%d_%H-%M-%S")
        sender = slugify(get_sender_name(user))
        filename = f"{ts}_{sender}_{slugify(Path(original).stem)}.{ext}"
        url = upload_file(service, tmp_path, filename, tg_folder_id, doc.mime_type)

        label = filename + (f" — {caption}" if caption else "")
        entry = make_entry(user, "documento", label)
        append_to_feedback_file(service, root_id, cycle, entry)
        await message.reply_text(f"Documento salvo no {cycle_folder_name(cycle)}.")
    except Exception as e:
        log.error(f"[Bot] Erro ao salvar documento: {e}")
        await message.reply_text(f"Erro: {e}")
    finally:
        os.unlink(tmp_path)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    audio = message.audio
    caption = (message.caption or "").strip()
    cycle = load_cycle()
    original = audio.file_name or "audio"
    ext = Path(original).suffix.lstrip(".") or "m4a"

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        tg_file = await context.bot.get_file(audio.file_id)
        await tg_file.download_to_drive(tmp_path)
        await process_audio_file(
            update,
            context,
            tmp_path,
            cycle,
            user,
            caption,
            suffix=f".{ext}",
            mime=audio.mime_type or "audio/mp4",
            original_stem=slugify(Path(original).stem),
        )
    except Exception as e:
        log.error(f"[Bot] Erro ao processar arquivo de áudio: {e}")
        await message.reply_text(f"Erro ao processar áudio: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_TOKEN_HERE":
        log.error("TELEGRAM_BOT_TOKEN não configurado.")
        return

    log.info("Iniciando Bot de Feedback TRS...")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    user_filter = filters.User(user_ids=ALLOWED_USER_IDS) if ALLOWED_USER_IDS else filters.ALL

    app.add_handler(CommandHandler("cicloatual", cmd_ciclo_atual))
    app.add_handler(CommandHandler("novociclo",  cmd_novo_ciclo))
    app.add_handler(CommandHandler("resumo",     cmd_resumo))
    app.add_handler(MessageHandler(filters.TEXT    & ~filters.COMMAND & user_filter, handle_text))
    app.add_handler(MessageHandler(filters.VOICE   & user_filter, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO   & user_filter, handle_audio))
    app.add_handler(MessageHandler(filters.PHOTO   & user_filter, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL & user_filter, handle_document))

    async def set_commands(app):
        commands = [
            BotCommand("cicloatual", "Ver o ciclo atual de feedback"),
            BotCommand("novociclo",  "Iniciar um novo ciclo"),
            BotCommand("resumo",     "Ver entradas do ciclo atual"),
        ]
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())

    app.post_init = set_commands

    log.info("Bot rodando. Ctrl+C para parar.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
