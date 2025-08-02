import aiohttp
import asyncio
import concurrent
from dataclasses import dataclass
from http import HTTPStatus
import json
import logging
import socket
import sys
import time
import threading

from .aircon import Device

if sys.version_info < (3, 8):
  TimeoutError = concurrent.futures.TimeoutError
else:
  TimeoutError = asyncio.exceptions.TimeoutError


@dataclass
class _NotifyConfiguration:
  device: Device
  headers: dict
  last_timestamp: int

class Notifier:
  _KEEP_ALIVE_INTERVAL = 10.0
  _TIME_TO_HANDLE_REQUESTS = 100e-3
  _REQUEST_TIMEOUT = 5.0

  def __init__(self, port: int):
    self._configurations = []
    self._condition = asyncio.Condition()

    self._running = False

    local_ip = self._get_local_ip()
    self._json = {'local_reg': {'ip': local_ip, 'notify': 0, 'port': port, 'uri': '/local_lan'}}

  def _get_local_ip(self):
    sock = None
    try:
      sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
      sock.connect(('10.255.255.255', 1))
      return sock.getsockname()[0]
    finally:
      if sock:
        sock.close()

  def register_device(self, device: Device):
    if device not in (conf.device for conf in self._configurations):
      headers = {
          'Accept': 'application/json',
          'Connection': 'keep-alive',
          'Content-Type': 'application/json',
          'Host': device.ip_address,
          'Accept-Encoding': 'gzip'
      }
      self._configurations.append(_NotifyConfiguration(device, headers, 0))

  async def _notify(self):
    async with self._condition:
      self._condition.notify_all()

  def notify(self):
    loop = asyncio.get_event_loop()
    asyncio.run_coroutine_threadsafe(self._notify(), loop)

  async def start(self, session: aiohttp.ClientSession):
    self._running = True
    async with self._condition:
      while self._running:
        queue_sizes = await asyncio.gather(*(self._perform_request(session=session, config=config)
                                             for config in self._configurations))
        if max(queue_sizes) <= 1:
          logging.debug('[KeepAlive] Waiting for notification or timeout')
          try:
            await asyncio.wait_for(self._condition.wait(), timeout=self._KEEP_ALIVE_INTERVAL)
          except TimeoutError:
            pass
        else:
          # give some time to clean up the queues
          await asyncio.sleep(self._TIME_TO_HANDLE_REQUESTS)

  async def stop(self):
    self._running = False
    await self._notify()

  async def _perform_request(self, session: aiohttp.ClientSession,
                             config: _NotifyConfiguration) -> int:
                  
    queue_size = config.device.commands_queue.qsize()
    if (queue_size == 0 or
        not config.device.available) and time.time() - config.last_timestamp < self._KEEP_ALIVE_INTERVAL:
      return 0
    method = 'PUT' if config.device.available else 'POST'
    self._json['local_reg']['notify'] = int(queue_size > 0)
    url = f'http://{config.device.ip_address}/local_reg.json'
    logging.debug(f'[KeepAlive] Sending {method} {url} {json.dumps(self._json)}')
    # try:
    try:
      async with session.request(method, url, json=self._json, headers=config.headers, timeout=self._REQUEST_TIMEOUT) as resp:
        if resp.status != HTTPStatus.ACCEPTED.value:
          resp_data = await resp.text()
          raise ConnectionError(f'{resp.status}, {resp_data}')
    except Exception as e:
      logging.error(f'[KeepAlive] Error: {repr(e)}')
      logging.error(f'[KeepAlive] Failed to connect to {config.device.ip_address}, maybe it is offline?')
      config.last_timestamp = time.time()
      config.device.available = False
      return 0

    config.last_timestamp = time.time()
    config.device.available = True
    return queue_size
