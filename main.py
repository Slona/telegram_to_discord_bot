#!/usr/bin/env python3

from telethon import TelegramClient, events
import telethon
import aiohttp
import nextcord
import textwrap
import os
import requests
import json
import random
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("WEBHOOK")
appid = os.environ.get("APPID")
apihash = os.environ.get("APIHASH")
apiname = os.environ.get("APINAME")
dlloc = os.environ.get("DLLOC")
input_channels_entities = os.environ.get("INPUT_CHANNELS")

if input_channels_entities is not None:
  input_channels_entities = list(map(int, input_channels_entities.split(',')))

def start():
    client = TelegramClient(apiname, 
                            appid, 
                            apihash)
    client.start()
    print('Started')
    print(f'Input channels: {input_channels_entities}')
    @client.on(events.NewMessage(chats=input_channels_entities))
    async def handler(event):
        if (type(event.chat)==telethon.tl.types.User):
          return #Ignore Messages from Users or Bots
        msg = event.message.message
        if event.message.media is not None: # If message has media
              path = await event.message.download_media(dlloc)
              await pic(path,msg,event.chat.title)
              os.remove(path)
        else: # No media text message
            await send_to_webhook(msg,event.chat.title)
        
    client.run_until_disconnected()

async def pic(filem,message,username): # Send media to webhook
    async with aiohttp.ClientSession() as session:
      try:
        print('Sending w media')
        webhook = nextcord.Webhook.from_url(url, session=session)
        try: # Try sending to discord
          f = nextcord.File(filem)
          await webhook.send(file=f,username=username)
        except Exception as ee:
            print(f'Error {ee.args}') 
        for line in textwrap.wrap(message, 2000, replace_whitespace=False): # Send message to discord
            await webhook.send(content=line,username=username) 
      except Exception as e:
        print(f'Error {e.args}')

async def send_to_webhook(message,username): # Send message to webhook
    async with aiohttp.ClientSession() as session:
        print('Sending w/o media')
        webhook = nextcord.Webhook.from_url(url, session=session)
        for line in textwrap.wrap(message, 2000, replace_whitespace=False): # Send message to discord
            await webhook.send(content=line,username=username)

if __name__ == "__main__":
    start()
