import os
import logging
import re
import httpx
import asyncio
from datetime import datetime
from typing import Dict, Optional, Union
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, InlineQueryHandler,
    filters, ContextTypes, Application
)
from cachetools import TTLCache
from uuid import uuid4
from dotenv import load_dotenv

# Carregar variáveis de ambiente
load_dotenv()

# Configurações
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_CONSULTA_TOKEN = os.getenv("API_CONSULTA_TOKEN")
API_CONSULTA_URL = os.getenv("API_CONSULTA_URL")

# URLs de APIs alternativas (adicione suas próprias)
API_BACKUP_URLS = [
    os.getenv("API_BACKUP_1"),
    os.getenv("API_BACKUP_2"),
]

# Configuração de logging otimizada
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Regex melhorada para placas (Mercosul e antigas)
PLATE_PATTERNS = {
    'mercosul': r'^[A-Z]{3}[0-9][A-Z][0-9]{2}$',  # ABC1D23
    'antiga': r'^[A-Z]{3}[0-9]{4}$',               # ABC1234
    'moto_mercosul': r'^[A-Z]{3}[0-9]{2}[A-Z][0-9]$',  # ABC12D3
}

# Cache otimizado com diferentes TTLs
cache_principal = TTLCache(maxsize=500, ttl=1800)  # 30 minutos
cache_erros = TTLCache(maxsize=100, ttl=300)       # 5 minutos para erros
cache_inline = TTLCache(maxsize=200, ttl=600)      # 10 minutos para inline

# Cliente HTTP reutilizável
http_client: Optional[httpx.AsyncClient] = None

class PlateValidator:
    """Classe para validação de placas"""

    @staticmethod
    def validate_plate(plate: str) -> tuple[bool, str]:
        """Valida formato da placa e retorna tipo"""
        plate = plate.upper().strip()

        for plate_type, pattern in PLATE_PATTERNS.items():
            if re.match(pattern, plate):
                return True, plate_type

        return False, 'invalid'

    @staticmethod
    def normalize_plate(plate: str) -> str:
        """Normaliza a placa removendo caracteres especiais"""
        return re.sub(r'[^A-Z0-9]', '', plate.upper().strip())

class APIConsultor:
    """Classe para consultas à API com fallback"""

    def __init__(self):
        self.timeout = httpx.Timeout(15.0, connect=5.0)
        self.limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)

    async def get_http_client(self) -> httpx.AsyncClient:
        """Retorna cliente HTTP reutilizável"""
        global http_client
        if http_client is None or http_client.is_closed:
            http_client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=self.limits,
                headers={
                    'User-Agent': 'TelegramBot/1.0',
                    'Accept': 'application/json',
                    'Connection': 'keep-alive'
                }
            )
        return http_client

    async def consultar_api_principal(self, placa: str) -> Dict:
        """Consulta API principal"""
        url = f"{API_CONSULTA_URL}/{placa}/{API_CONSULTA_TOKEN}"
        client = await self.get_http_client()

        try:
            response = await client.get(url)

            if response.status_code == 402:
                return {
                    "erro": "payment_required",
                    "message": "⚠️ Serviço temporariamente indisponível - Créditos esgotados"
                }
            elif response.status_code == 404:
                return {
                    "erro": "not_found",
                    "message": "🔍 Placa não encontrada na base de dados"
                }
            elif response.status_code == 429:
                return {
                    "erro": "rate_limit",
                    "message": "⏱️ Muitas consultas. Aguarde alguns segundos"
                }

            response.raise_for_status()
            return response.json()

        except httpx.TimeoutException:
            return {"erro": "timeout", "message": "⏱️ Timeout na consulta"}
        except httpx.HTTPStatusError as e:
            return {"erro": "http_error", "message": f"❌ Erro HTTP: {e.response.status_code}"}
        except Exception as e:
            logger.error(f"Erro na API principal: {e}")
            return {"erro": "api_error", "message": "❌ Erro na consulta"}

    async def consultar_apis_backup(self, placa: str) -> Dict:
        """Consulta APIs de backup"""
        for backup_url in API_BACKUP_URLS:
            if not backup_url:
                continue

            try:
                client = await self.get_http_client()
                url = f"{backup_url}/{placa}" # Assumindo que o backup não precisa de token, ou já está no URL
                response = await client.get(url)

                if response.status_code == 200:
                    return response.json()

            except Exception as e:
                logger.warning(f"Erro na API backup {backup_url}: {e}")
                continue

        return {"erro": "all_apis_failed", "message": "❌ Todas as APIs de backup falharam"}

    async def consultar_placa(self, placa: str) -> Dict:
        """Consulta principal com fallback"""
        cache_key = f"placa_{placa}"
        if cache_key in cache_principal:
            logger.info(f"Cache hit para placa {placa}")
            return cache_principal[cache_key]

        error_key = f"error_{placa}"
        if error_key in cache_erros:
            logger.info(f"Erro em cache para placa {placa}")
            return cache_erros[error_key]

        resultado = await self.consultar_api_principal(placa)

        if "erro" in resultado and resultado["erro"] in ["payment_required", "api_error", "timeout", "http_error"]: # Adicionado http_error para fallback
            logger.info(f"API principal falhou para {placa} (erro: {resultado['erro']}), tentando backups...")
            resultado_backup = await self.consultar_apis_backup(placa)

            if "erro" not in resultado_backup:
                cache_principal[cache_key] = resultado_backup # Cache do resultado do backup
                return resultado_backup
            else:
                # Se o backup também falhou, retornamos o erro original da API principal
                # ou o erro do backup se for mais informativo, mas geralmente o da principal é o primeiro a ser mostrado.
                # Cacheamos o erro da API principal para evitar tentativas repetidas.
                cache_erros[error_key] = resultado
                return resultado


        if "erro" in resultado:
            cache_erros[error_key] = resultado
        else:
            cache_principal[cache_key] = resultado

        return resultado

api_consultor = APIConsultor()

from typing import Union # Certifique-se de ter este import se ainda não tiver

def escape_markdown_v2(text: Union[str, int, float, None]) -> str:
    """Escapa caracteres especiais para MarkdownV2."""
    if text is None:
        return "" # Retorna string vazia se a entrada for None
    if not isinstance(text, str):
        text = str(text) # Converte para string se não for

    # 1. Primeiro, escape a própria barra invertida.
    # Esta é a correção crucial para a linha 199.
    # Python string: '\' representa um único caractere de barra invertida.
    # Python string: '\' representa dois caracteres de barra invertida.
    # Objetivo: Substituir cada '\' por '\' no texto final do Markdown.
    text = text.replace('\\', '\\')

    # 2. Depois, escape os outros caracteres especiais do MarkdownV2.
    # Lista de caracteres que precisam ser prefixados com '\' no MarkdownV2.
    # A ordem aqui geralmente não importa APÓS o escape da barra invertida.
    escape_chars = r'_*[]()~`>#+-=|{}.!' # Adicionamos o ponto e a exclamação que estavam no seu dict original

    for char_to_escape in escape_chars:
        # No Python, para colocar uma barra invertida antes do caractere,
        # usamos f'{char_to_escape}'
        text = text.replace(char_to_escape, f'{char_to_escape}')

    return text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /start melhorado"""
    # As mensagens aqui já estão formatadas para MarkdownV2.
    # A função escape_markdown_v2 é para dados dinâmicos.
    welcome_message = """
 *Bem-vindo ao Bot de Consulta de Placas!*

📋 *Formatos aceitos:*
• Placas antigas: `ABC1234`
• Placas Mercosul: `ABC1D23`
• Motos Mercosul: `ABC12D3`

🔍 *Como usar:*
• Envie a placa diretamente no chat
• Use inline: `@seu_bot_aqui ABC1234`

⚡ *Recursos:*
• Consulta rápida com cache
• Múltiplas APIs para maior disponibilidade
• Suporte a todos os formatos de placa

Digite uma placa para começar!
    """
    # Nota: Substitua @seu_bot_aqui pelo nome de usuário real do seu bot para o modo inline.
    # O caractere '-' em 'Bem-vindo' e '!' no final precisam ser escapados.
    await update.message.reply_text(welcome_message, parse_mode="MarkdownV2")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler otimizado para mensagens"""
    if not update.message or not update.message.text:
        return
        
    user_input = update.message.text.strip()
    placa = PlateValidator.normalize_plate(user_input)

    if not (7 <= len(user_input) <= 8): # Permite um hífen opcional, que normalize_plate remove
        # Nota: A mensagem usa `$$antiga$$` e `$$Mercosul$$`.
        # Se `$$` não for uma formatação especial que você pretende,
        # considere removê-los ou usar outra forma de destaque como `(antiga)`.
        # Para MarkdownV2, parênteses literais devem ser escapados: `$$antiga$$`.
        await update.message.reply_text(
            "🔎 *Formato inválido!*\n\n"
            "Envie uma placa com 7 caracteres $$sem espaços ou hífens extras$$:\n"
            "• `ABC1234` $$antiga$$\n"
            "• `ABC1D23` $$Mercosul$$",
            parse_mode="MarkdownV2"
        )
        return

    is_valid, plate_type = PlateValidator.validate_plate(placa)
    if not is_valid:
        await update.message.reply_text(
            "❌ *Formato de placa inválido*\n\n"
            "Formatos aceitos:\n"
            "• `ABC1234`  Placa antiga\n"
            "• `ABC1D23`  Placa Mercosul\n"
            "• `ABC12D3`  Moto Mercosul",
            parse_mode="MarkdownV2"
        )
        return

    plate_type_emoji = {
        'mercosul': '🆕',
        'antiga': '🔢',
        'moto_mercosul': '🏍️'
    }

    loading_text = f"Consultando placa {plate_type_emoji.get(plate_type,)} `{escape_markdown_v2(placa)}`"
    loading_msg = await update.message.reply_text(loading_text, parse_mode="MarkdownV2")

    try:
        resultado = await api_consultor.consultar_placa(placa)

        await loading_msg.delete()

        if "erro" in resultado:
            await update.message.reply_text(
                f"❌ {escape_markdown_v2(resultado.get('message', 'Erro desconhecido'))}",
                parse_mode="MarkdownV2"
            )
        else:
            mensagem = formatar_mensagem_otimizada(resultado, plate_type)
            await update.message.reply_text(mensagem, parse_mode="MarkdownV2")

    except Exception as e:
        logger.error(f"Erro no handle_message: {e}", exc_info=True)
        try:
            await loading_msg.delete()
        except Exception: # pylint: disable=broad-except
            pass
        await update.message.reply_text(
            "❌ Erro interno do bot Tente novamente",
            parse_mode="MarkdownV2"
        )

def formatar_mensagem_otimizada(resultado: dict, plate_type: str) -> str:
    """Formatação otimizada da mensagem"""
    try:
        extra = resultado.get("extra", {}) or {}
        # Tratamento para dados_fipe ser lista ou dicionário
        fipe_data = resultado.get("fipe", {}).get("dados", {})
        texto_valor = "N/A"
        if isinstance(fipe_data, list) and fipe_data:
            texto_valor = str(fipe_data[0].get("texto_valor", "N/A"))
        elif isinstance(fipe_data, dict):
            texto_valor = str(fipe_data.get("texto_valor", "N/A"))


        plate_emoji = {
            'mercosul': '🆕',
            'antiga': '',
            'moto_mercosul': '🏍️'
        }.get(plate_type, '🚗')

        hora_consulta = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        def fmt(value, default="N/A"):
            return escape_markdown_v2(str(value) if value and str(value).strip() else default)

        # Monta a mensagem com campos formatados e escapados
        # Garante que todos os valores dinâmicos sejam escapados
        mensagem = f"""
    {plate_emoji} *Consulta de Placa  {fmt(resultado.get('placa', 'N/A'))}*

    ⏰ *Consultado em:* `{escape_markdown_v2(hora_consulta)}`
    🔧 *Tipo de Placa:* `{escape_markdown_v2(plate_type.replace('_', ' ').title())}`

    📋 *DADOS DO VEÍCULO*

    🚗 *Modelo:* `{fmt(resultado.get('modelo'))}`
    🏭 *Marca:* `{fmt(resultado.get('marca'))}`
    🎨 *Cor:* `{fmt(resultado.get('cor'))}`
    📅 *Ano Fab/Mod:* `{fmt(resultado.get('ano'))} / {fmt(resultado.get('anoModelo'))}`
    ⛽ *Combustível:* `{fmt(resultado.get('combustivel'))}`  
    ⛽ *Combustível:* `{fmt(extra.get('combustivel'))}`  
    
    🚫 *Restrição:* `{fmt(resultado.get('situacao'))}`

    🆔 *Chassi:* `{fmt(resultado.get('chassi'))}`
    🆔 *Chassi Completo:* `{fmt(extra.get('chassi'))}`

    📍 *Município:* `{fmt(resultado.get('municipio'))} - {fmt(resultado.get('uf'))}`
    🏳️ *UF Faturado:* `{fmt(extra.get('uf_faturado'))}`

    💵 *Valor:* `{fmt(texto_valor)}`
        """
        # O caractere '-' em "Consulta de Placa - PLACA" e "Município - UF" foi escapado.
        return mensagem.strip()

    except Exception as e:
        logger.error(f"Erro na formatação: {e}", exc_info=True)
        return f"❌ Erro ao formatar dados da placa `{escape_markdown_v2(str(resultado.get('placa', 'N/A')))}`"

async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler otimizado para consultas inline"""
    if not update.inline_query:
        return
        
    query = update.inline_query.query.strip()

    if not query:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="🔍 Digite uma placa para consultar",
                description="Formatos: ABC1234 (antiga) ou ABC1D23 (Mercosul)",
                input_message_content=InputTextMessageContent(
                    "Digite uma placa para consultar\n\n"
                    "Formatos aceitos:\n"
                    "• `ABC1234`  Placa Antiga\n"
                    "• `ABC1D23`  Placa Mercosul",
                    parse_mode="MarkdownV2"
                ),
            )
        ]
        await update.inline_query.answer(results, cache_time=300)
        return

    placa = PlateValidator.normalize_plate(query)

    if len(placa) != 7: # A placa normalizada deve ter 7 caracteres
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="Placa deve ter 7 caracteres",
                description=f"Você digitou: {query} ({len(placa)} após normalização)",
                input_message_content=InputTextMessageContent(
                    "Placa deve ter exatamente 7 caracteres após remover espaços/hífens"
                ), # parse_mode não especificado, então é texto plano. Se quiser MarkdownV2, adicione e escape.
            )
        ]
        await update.inline_query.answer(results, cache_time=60)
        return

    is_valid, plate_type = PlateValidator.validate_plate(placa)
    if not is_valid:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="❌ Formato de placa inválido",
                description=f"'{escape_markdown_v2(query)}' não é um formato válido",
                input_message_content=InputTextMessageContent(
                    f"❌ Formato inválido: `{escape_markdown_v2(query)}`",
                    parse_mode="MarkdownV2"
                ),
            )
        ]
        await update.inline_query.answer(results, cache_time=60)
        return

    inline_cache_key = f"inline_{placa}"
    if inline_cache_key in cache_inline:
        resultado = cache_inline[inline_cache_key]
        logger.info(f"Cache inline hit para placa {placa}")
    else:
        try:
            resultado = await api_consultor.consultar_placa(placa)
            if "erro" not in resultado : # Cache apenas resultados de sucesso para inline
                 cache_inline[inline_cache_key] = resultado
        except Exception as e:
            logger.error(f"Erro na consulta inline para {placa}: {e}", exc_info=True)
            results = [
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title="❌ Erro na consulta",
                    description="Tente novamente em alguns segundos",
                    input_message_content=InputTextMessageContent(
                        "❌ Erro temporário Tente novamente",
                        parse_mode="MarkdownV2"
                    ),
                )
            ]
            await update.inline_query.answer(results, cache_time=30)
            return

    if "erro" in resultado:
        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=f"❌ {escape_markdown_v2(resultado.get('message', 'Erro na consulta'))}",
                description=f"Placa: {escape_markdown_v2(placa)}",
                input_message_content=InputTextMessageContent(
                    f"❌ {escape_markdown_v2(resultado.get('message', 'Erro na consulta'))}\n"
                    f"Placa: `{escape_markdown_v2(placa)}`",
                    parse_mode="MarkdownV2"
                ),
            )
        ]
    else:
        modelo = escape_markdown_v2(resultado.get('modelo', 'N/A'))
        marca = escape_markdown_v2(resultado.get('marca', 'N/A'))
        cor = escape_markdown_v2(resultado.get('cor', 'N/A'))

        results = [
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=f"🚗 {escape_markdown_v2(placa)} - {marca} {modelo}",
                description=f"Cor: {cor} | Clique para ver detalhes completos",
                input_message_content=InputTextMessageContent(
                    formatar_mensagem_otimizada(resultado, plate_type),
                    parse_mode="MarkdownV2"
                ),
            )
        ]

    await update.inline_query.answer(results, cache_time=180) # Reduzido de 300 para erros, pode manter mais alto para sucessos

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler global de erros"""
    logger.error(f"Update {update} causou erro: {context.error}", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Ocorreu um erro inesperado ao processar sua solicitação. Por favor, tente novamente!",
                parse_mode="MarkdownV2"
            )
        except Exception as e: # pylint: disable=broad-except
            logger.error(f"Erro ao enviar mensagem de erro para o usuário: {e}")


async def cleanup_resources():
    """Limpa recursos ao encerrar"""
    global http_client
    if http_client and not http_client.is_closed:
        await http_client.aclose()
        logger.info("Cliente HTTP fechado.")

def main():
    """Função principal otimizada"""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN não encontrado! O bot não pode iniciar.")
        return

    if not API_CONSULTA_TOKEN or not API_CONSULTA_URL:
        logger.critical("Configurações da API principal (API_CONSULTA_TOKEN ou API_CONSULTA_URL) não encontradas! Algumas funcionalidades podem não operar.")
        # Poderia optar por não iniciar o bot, dependendo da criticidade.
        # return

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_error_handler(error_handler)

    logger.info("🚀 Bot iniciado com sucesso! Aguardando polling...")

    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            # close_loop=False # Em versões mais recentes de PTB, o loop é gerenciado internamente.
                              # Se for True, pode fechar o loop antes do cleanup.
                              # Se False, você pode precisar gerenciar o fechamento do loop se fizer mais operações async após run_polling.
                              # Para este caso, a limpeza com asyncio.run() deve funcionar bem.
        )
    except KeyboardInterrupt:
        logger.info("Bot interrompido pelo usuário (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Erro fatal durante o polling: {e}", exc_info=True)
    finally:
        logger.info("Iniciando limpeza de recursos...")
        # Para executar a corrotina de limpeza, especialmente se o loop do run_polling já terminou.
        # Se o loop ainda estiver rodando e for diferente do que asyncio.run() usa, pode haver problemas.
        # PTB v20+ gerencia seu próprio loop.
        try:
            # Tenta obter o loop existente se ainda estiver rodando
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(cleanup_resources()) # Adiciona como tarefa se o loop ainda estiver ativo
            else:
                asyncio.run(cleanup_resources()) # Executa em um novo loop se o anterior foi fechado
        except RuntimeError: # Caso não haja loop de eventos atual
             asyncio.run(cleanup_resources())
        logger.info("Bot encerrado.")


if __name__ == "__main__":
    main()