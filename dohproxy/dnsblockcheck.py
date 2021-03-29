#!/usr/bin/env python3

import socket
#import asyncio
import os, os.path
import signal
import sys
import time
import argparse
import logging
import json
import threading
from functools import partial
from multidict import CIMultiDict

blocklist = CIMultiDict()
fileslist = CIMultiDict()


def configure_logger(name='', level='DEBUG'):
  log_format = '%(name)s - %(funcName)s - %(threadName)s - %(levelname)s - %(message)s'
  if sys.stdout.isatty():
    log_format = '%(asctime)s - ' + log_format
  logging.basicConfig(format=log_format)
  logger = logging.getLogger(name)  
  level_name = level.upper()
  level = getattr(logging, level_name, None)
  if not isinstance(level, int):
    raise Exception('Invalid log level name : %s' % level_name)
  logger.setLevel(level)
  return logger
  #handler = logging.FileHandler('/tmp/{}.log'.format(name))
  #logger.addHandler(handler)


class socketserver():
  def __init__(self, args, logger):
    self.args = args
    self.logger = logger
    self.server = None
    if os.path.exists(args.socket):
      os.remove(args.socket)
    #loop = asyncio.get_event_loop()
    
  def is_blocked(self, data):
    if 'user' in data and 'domain' in data:
      self.logger.info('Checking whether domain name {} blocked for {}'.format(data['domain'], data['user']))
      if data['user'] in blocklist:
        if data['domain'] in blocklist[data['user']]:
          return True
      else:
        return False 
    else:
      return False

  def handle_req(self, client, address):
    while True:
      try:
        data = client.recv(2048)
        if data:
          data = json.loads(data.decode())
          if self.is_blocked(data):
            client.send(bytes([True]))
          else:
            client.send(bytes([False]))
        else:
          raise error('Client disconnected')
      except:
        client.close()
        return False

  def start_server(self):
    self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    self.server.bind(self.args.socket)
    self.listen()
  
  def listen(self):
    try:
      while True:
        time.sleep(0.2)
        self.server.listen(5)
        client, address = self.server.accept()
        client.settimeout(60)
        threading.Thread(target=self.handle_req, args=(client,address)).start()
    except KeyboardInterrupt as k:
      self.logger.info("Shutting down the server.")
      os.remove(self.args.socket)
      self.server.close()

def read_files(path, logger):
  fileslist['path'] = path
  logger.info('Listing files from: {}'.format(path))
  for dirpath, dirnames, filenames in os.walk(path):
    for file in [os.path.join(dirpath, f) for f in filenames]:
      user = file.split('/')[-1].split('.')[0]
      fileslist[file] = os.stat(file).st_mtime 
      try:
        with open(file,'r') as fin:
          blocklist[user]=[line.strip() for line in fin]
      except Exception as e:
        logger.error('Error: {}'.format(e))
  logger.info('read all files.')

def read_on_signal(logger, signum, stack):
  logger.info('Received signal: {}'.format(signum))
  path = fileslist['path']
  newfileslist = CIMultiDict()
  for dirpath, dirnames, filenames in os.walk(path):
    for file in [os.path.join(dirpath, f) for f in filenames]:
      user = file.split('/')[-1].split('.')[0]
      newfileslist[file] = os.stat(file).st_mtime
      if (file in fileslist) and (newfileslist[file] == fileslist[file]):
        continue
      try:
        with open(file,'r') as fin:
          blocklist[user]=[line.strip() for line in fin]
      except Exception as e:
        logger.error('Error: {}'.format(e))
  logger.info('read all files.')

def main():
  parser = argparse.ArgumentParser(description='Run command.')
  parser.add_argument( '-s', '--socket', default='/tmp/dnsblockcheck.sock',
    help='The socket file on which it should listen. Default: [%(default)s]'
  )
  parser.add_argument( '-f', '--filespath', default='./user-blocklist',
    help='A directory path where the user files are located. Default: [%(default)s]'
  )
  args = parser.parse_args()
  logger = configure_logger("DNSBLOCKLIST")
  read_files(args.filespath, logger)
  signal.signal(signal.SIGUSR1, partial(read_on_signal, logger))

  server = socketserver(args, logger)
  server.start_server()


if __name__ == "__main__":
    main()


