# Importaciones de la biblioteca estándar
from datetime import datetime, timedelta, timezone
import base64
import json
import os

# Importaciones de bibliotecas de terceros
import aiohttp
from aiohttp.client_exceptions import ClientConnectorError
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# Variables de entorno
TOKEN = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_CHANNEL_NAME = os.getenv("TWITCH_CHANNEL_NAME")
GUILD_ID = os.getenv("GUILD_ID")

# Instancia de la clase Intents
intents = discord.Intents.default() # Habilita los intents por defecto
intents.messages = True # Para recibir eventos de mensaje
intents.message_content = True # Para acceder al contenido del mensaje, necesario para los comandos

# Creamos una instancia de la clase Bot con los intents configurados
bot = commands.Bot(intents=intents, command_prefix='!')

# Variable global para almacenar el token de OAuth de Twitch
twitch_oauth_token = None

# Variable para rastrear si ya se ha creado un evento para la transmisión en vivo actual
is_event_active = False

# Variable para rastrear el evento de Discord activo
discord_event_id = None

# Variable para definir el delay del buffer en segundos
buffer_delay_seconds = 5

# Variable para definir el delta en minutos del tiempo inicial de finalización del evento
delta_end_time_minutes = 120

# Intervalo de tiempo en minutos para verificar la transmisión en Twitch
check_interval_minutes = 2

# Intervalo de tiempo en minutos para actualizar la fecha de finalización del evento
update_interval_minutes = 60

# Número de horas para extender la fecha de finalización del evento cada vez
extension_minutes = 120

# Función para obtener el OAuth Token de Twitch
async def get_twitch_oauth_token():
    global twitch_oauth_token
    url = 'https://id.twitch.tv/oauth2/token'
    payload = {
        'client_id': TWITCH_CLIENT_ID,
        'client_secret': TWITCH_CLIENT_SECRET,
        'grant_type': 'client_credentials'
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, data=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    twitch_oauth_token = data['access_token']
                    print('OAuth Token de Twitch obtenido con éxito.')
                else:
                    # Manejo específico para diferentes códigos de estado HTTP
                    error_message = await response.text()
                    print(f"Failed to get OAuth token: {response.status} - {error_message}")
        except ClientConnectorError as e:
            # Manejo de errores de conexión
            print(f"Connection error occurred: {e}")
        except aiohttp.ClientResponseError as e:
            # Manejo de errores de respuesta del cliente (4xx y 5xx fuera de los gestionados arriba)
            print(f"Client response error occurred: {e}")
        except Exception as e:
            # Manejo de cualquier otra excepción
            print(f"An unexpected error occurred: {e}")

# Función auxiliar para convertir una imagen de una URL a data URI
async def convert_image_to_data_uri(url):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    image_data = await response.read()
                    b64_encoded = base64.b64encode(image_data).decode('utf-8')
                    return f'data:{response.content_type};base64,{b64_encoded}'
                else:
                    print(f"Failed to fetch image: HTTP Status {response.status}")
                    return None
        except ClientConnectorError as e:
            print(f"Connection error occurred while fetching image: {e}")
            return None
        except aiohttp.ClientResponseError as e:
            print(f"Client response error occurred while fetching image: {e}")
            return None
        except Exception as e:
            print(f"An unexpected error occurred while fetching image: {e}")
            return None

# Función para verificar el estado actual del evento en Discord
async def check_discord_event_status(guild_id, event_id):
    url = f'https://discord.com/api/v9/guilds/{guild_id}/scheduled-events/{event_id}'
    headers = {
        'Authorization': f'Bot {TOKEN}',
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    event_data = await response.json()
                    # Verifica si el evento aún está activo (por ejemplo, no ha sido cancelado manualmente)
                    return event_data['status'] in [1, 2]  # 1 para SCHEDULED, 2 para ACTIVE
                else:
                    return False
        except Exception as e:
            print(f"An unexpected error occurred while checking Discord event status: {e}")
            return False

# Función para comprobar si el canal de Twitch está en directo
async def check_twitch_stream_online():
    global twitch_oauth_token
    url = f'https://api.twitch.tv/helix/streams?user_login={TWITCH_CHANNEL_NAME}'
    headers = {
        'Client-ID': TWITCH_CLIENT_ID,
        'Authorization': f'Bearer {twitch_oauth_token}'
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data['data']:
                        stream_info = data['data'][0]
                        return True, stream_info['title']  # Devuelve True y el título de la transmisión
                    else:
                        return False, None  # Canal no está en directo
                else:
                    return False, None
    except ClientConnectorError as e:
        print(f"Connection error occurred while checking Twitch stream: {e}")
    except aiohttp.ClientResponseError as e:
        print(f"Client response error occurred while checking Twitch stream: {e}")
    except Exception as e:
        print(f"An unexpected error occurred while checking Twitch stream: {e}")

# Función para crear el evento en Discord
async def create_discord_event(guild_id, name, description, thumbnail_url=None):
    global discord_event_id
    fecha_hora_evento = datetime.now(timezone.utc) + timedelta(seconds=buffer_delay_seconds)  # Hora actual UTC + segundos de buffer
    scheduled_start_time = fecha_hora_evento.isoformat()
    scheduled_end_time = (fecha_hora_evento + timedelta(minutes=delta_end_time_minutes)).isoformat()

    # Convertir thumbnail_url a data URI si se proporciona
    image_data_uri = None
    if thumbnail_url:
        image_data_uri = await convert_image_to_data_uri(thumbnail_url)

    url = f'https://discord.com/api/v9/guilds/{guild_id}/scheduled-events'
    json_payload = {
        'name': name,
        'privacy_level': 2,
        'scheduled_start_time': scheduled_start_time,
        'scheduled_end_time': scheduled_end_time,
        'description': description,
        'image': image_data_uri,
        'entity_type': 3,  # Eventos en línea
        'entity_metadata': {'location': f"https://www.twitch.tv/{TWITCH_CHANNEL_NAME}"}
    }
    headers = {
        'Authorization': f'Bot {TOKEN}',
        'Content-Type': 'application/json'
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=json_payload, headers=headers) as response:
                if response.status in (200, 201):  # 200 OK o 201 Created
                    event_data = await response.json()
                    discord_event_id = event_data['id']  # Guarda el ID del evento creado
                    print(f"Evento '{name}' creado con éxito.")
                    return True
                else:
                    # Manejo específico para diferentes códigos de estado HTTP
                    error_message = await response.text()
                    print(f"Failed to create event: {error_message} (Status code: {response.status})")
                    return False
        except ClientConnectorError as e:
            # Manejo de errores de conexión
            print(f"Connection error occurred while creating Discord event: {e}")
            return False
        except aiohttp.ClientResponseError as e:
            # Manejo de errores de respuesta del cliente (4xx y 5xx fuera de los gestionados arriba)
            print(f"Client response error occurred while creating Discord event: {e}")
            return False
        except aiohttp.ClientPayloadError as e:
            # Manejo de errores relacionados con el procesamiento del payload
            print(f"Payload error occurred while creating Discord event: {e}")
            return False
        except Exception as e:
            # Manejo de cualquier otra excepción
            print(f"An unexpected error occurred while creating Discord event: {e}")
            return False

# Función para modificar un evento en Discord
async def modify_discord_event(guild_id, event_id, changes):
    url = f'https://discord.com/api/v9/guilds/{guild_id}/scheduled-events/{event_id}'
    headers = {
        'Authorization': f'Bot {TOKEN}',
        'Content-Type': 'application/json'
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.patch(url, json=changes, headers=headers) as response:
                if response.status == 200:
                    event_data = await response.json()
                    print(f"Evento '{event_data['name']}' modificado con éxito.")
                    return True
                else:
                    # Manejo específico para diferentes códigos de estado HTTP
                    error_message = await response.text()
                    print(f"Failed to modify event: {error_message} (Status code: {response.status})")
                    return False
        except ClientConnectorError as e:
            # Manejo de errores de conexión
            print(f"Connection error occurred while modifying Discord event: {e}")
            return False
        except aiohttp.ClientResponseError as e:
            # Manejo de errores de respuesta del cliente
            print(f"Client response error occurred while modifying Discord event: {e}")
            return False
        except aiohttp.ClientPayloadError as e:
            # Manejo de errores relacionados con el payload
            print(f"Payload error occurred while modifying Discord event: {e}")
            return False
        except Exception as e:
            # Manejo de cualquier otra excepción
            print(f"An unexpected error occurred while modifying Discord event: {e}")
            return False

# Función para actualizar el estado y la fecha de finalización de un evento
async def update_event_details(guild_id, event_id, new_status=None, new_end_time=None):
    changes = {}
    if new_status is not None:
        changes['status'] = new_status
    if new_end_time is not None:
        changes['scheduled_end_time'] = new_end_time
    
    return await modify_discord_event(guild_id, event_id, changes)

# Tarea en segundo plano para verificar la transmisión en Twitch y crear o cancelar un evento en Discord
@tasks.loop(minutes=check_interval_minutes)
async def check_twitch_and_create_event():
    global is_event_active, discord_event_id
    stream_online, stream_title = await check_twitch_stream_online()
    
    # Verifica primero el estado del evento en Discord si existe uno
    if discord_event_id:
        is_event_still_active = await check_discord_event_status(GUILD_ID, discord_event_id)
        if not is_event_still_active:
            is_event_active = False
            discord_event_id = None

    if stream_online:
        if not is_event_active:
            # El canal está en directo y no se ha creado un evento aún
            event_name = f"{TWITCH_CHANNEL_NAME} está en directo!"

            # Crea el evento
            is_event_created = await create_discord_event(GUILD_ID, event_name, stream_title, "https://media.discordapp.net/attachments/794644359064453122/1171932649955270726/1024.png")
            is_event_active = is_event_created
    else:
        if is_event_active and discord_event_id:
            # El canal ya no está en directo, pero hay un evento de Discord activo
            # Actualiza el evento para marcarlo como finalizado
            changes = {'status': 3}  # COMPLETED
            was_event_cancelled = await modify_discord_event(GUILD_ID, discord_event_id, changes)
            if was_event_cancelled:
                print(f"Evento '{discord_event_id}' eliminado con éxito.")
                discord_event_id = None  # Restablece el ID del evento
                is_event_active = False  # Indica que ya no hay un evento activo

# Tarea en segundo plano para actualizar la fecha de finalización del evento
@tasks.loop(minutes=update_interval_minutes) 
async def update_discord_event_end_time():
    global discord_event_id, is_event_active
    if is_event_active and discord_event_id:
        if await check_twitch_stream_online():
            # Extiende la fecha de finalización del evento desde ahora
            new_end_time = (datetime.now(timezone.utc) + timedelta(minutes=extension_minutes)).isoformat()
            changes = {'scheduled_end_time': new_end_time}
            await modify_discord_event(GUILD_ID, discord_event_id, changes)

@bot.event
async def on_close():
    global http_session
    if http_session:
        await http_session.close()

@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    # Intenta obtener el token de OAuth de Twitch al iniciar el bot
    await get_twitch_oauth_token()
    if twitch_oauth_token:
        # Si el token se obtuvo con éxito, inicia la tarea en segundo plano
        check_twitch_and_create_event.start()
        update_discord_event_end_time.start()
    else:
        print('No se pudo obtener el token de OAuth de Twitch.')


# Ejecutar el bot
if __name__ == "__main__":
    bot.run(TOKEN)
