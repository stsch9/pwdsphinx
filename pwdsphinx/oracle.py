#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2018-2021, Marsiske Stefan
# SPDX-License-Identifier: GPL-3.0-or-later

import socket, sys, ssl, os, datetime, binascii, shutil, os.path, traceback, struct
import pysodium
import equihash
from pwdsphinx import sphinxlib
from pwdsphinx.config import getcfg
cfg = getcfg('sphinx')

verbose = cfg['server'].getboolean('verbose')
address = cfg['server']['address']
port = int(cfg['server']['port'])
timeout = int(cfg['server']['timeout'])
max_kids = int(cfg['server'].get('max_kids',5))
datadir = os.path.expanduser(cfg['server']['datadir'])
ssl_key = os.path.expanduser(cfg['server']['ssl_key'])
ssl_cert = os.path.expanduser(cfg['server']['ssl_cert'])
rl_decay = int(cfg['server'].get('rl_decay',1800))
rl_threshold = int(cfg['server'].get('rl_threshold',1))
rl_gracetime = int(cfg['server'].get('rl_gracetime',10))

if(verbose):
  cfg.write(sys.stdout)

CREATE=0x00
READ=0x33
UNDO=0x55
GET=0x66
COMMIT=0x99
CHANGE=0xaa
WRITE=0xcc
CHALLENGE_CREATE = 0x5a
CHALLENGE_VERIFY = 0xa5
DELETE=0xff

RULE_SIZE=42

Difficulties = [
    # timeouts are based on benchmarking a raspberry pi 1b
    { 'n': 60,  'k': 4, 'timeout': 1 },    # 320KiB, ~0.02
    { 'n': 65,  'k': 4, 'timeout': 2 },    # 640KiB, ~0.04
    { 'n': 70,  'k': 4, 'timeout': 4 },    # 1MiB, ~0.08
    { 'n': 75,  'k': 4, 'timeout': 9 },    # 2MiB, ~0.2
    { 'n': 80,  'k': 4, 'timeout': 16 },   # 5MiB, ~0.5
    { 'n': 85,  'k': 4, 'timeout': 32 },   # 10MiB, ~0.9
    { 'n': 90,  'k': 4, 'timeout': 80 },   # 20MiB, ~2.4
    { 'n': 95,  'k': 4, 'timeout': 160 },  # 40MiB, ~4.6
    # timeouts below are interpolated from above
    { 'n': 100, 'k': 4, 'timeout': 320 },  # 80MiB, ~7.8
    { 'n': 105, 'k': 4, 'timeout': 640 },  # 160MiB, ~25
    { 'n': 110, 'k': 4, 'timeout': 1280 }, # 320MiB, ~57
    { 'n': 115, 'k': 4, 'timeout': 2560 }, # 640MiB, ~70
    { 'n': 120, 'k': 4, 'timeout': 5120 }, # 1GiB, ~109
]
RL_Timeouts = {(e['n'],e['k']): e['timeout'] for e in Difficulties}

normal = "\033[38;5;%sm"
reset = "\033[0m"

def fail(s):
    if verbose:
        traceback.print_stack()
        print('fail')
    s.send(b'\x00\x04fail') # plaintext :/
    s.shutdown(socket.SHUT_RDWR)
    s.close()
    os._exit(0)

def pop(obj, cnt):
  return obj[:cnt], obj[cnt:]

def verify_blob(msg, pk):
  sig = msg[-64:]
  msg = msg[:-64]
  pysodium.crypto_sign_verify_detached(sig, msg, pk)
  return msg

def save_blob(path,fname,blob):
  path = os.path.join(datadir, path, fname)
  with open(path,'wb') as fd:
    os.fchmod(fd.fileno(),0o600)
    fd.write(blob)

def read_pkt(s,size):
    res = []
    read = 0
    while read<size:
      res.append(s.recv(size-read))
      read+=len(res[-1])
    return b''.join(res)

def update_blob(s):
    id = s.recv(32)
    if len(id)!=32:
      fail(s)
    id = binascii.hexlify(id).decode()
    blob = load_blob(id,'blob')
    new = False
    if blob is None:
      new = True
      blob = b'\x00\x00'
    s.send(blob)
    if new:
      pk = s.recv(32)
      prefix = s.recv(2)
      bsize = struct.unpack('!H', prefix)[0]
      signedblob = read_pkt(s, bsize+64)
      blob = pk+prefix+signedblob
      try:
        blob = verify_blob(blob,pk)
      except ValueError:
        print('invalid signature on msg')
        fail(s)
      blob = blob[32:]
      # create directories
      if not os.path.exists(datadir):
        os.mkdir(datadir,0o700)
      tdir = os.path.join(datadir,id)
      if not os.path.exists(tdir):
        os.mkdir(tdir,0o700)
      # save pubkey
      save_blob(id,'pub',pk)
    else:
      prefix = s.recv(2)
      bsize = struct.unpack('!H', prefix)[0]
      signedblob = read_pkt(s, bsize+64)
      blob = prefix+signedblob
      pk = load_blob(id,'pub')
      try:
        blob = verify_blob(blob,pk)
      except ValueError:
        print('invalid signature on msg')
        fail(s)
    save_blob(id,'blob',blob)

# msg format: 0x00|id[32]|alpha[32]
def create(s, msg):
    if len(msg)!=65:
      fail(s)
    print('Data received:',msg.hex())
    op,   msg = pop(msg,1)
    id,   msg = pop(msg,32)
    alpha,msg = pop(msg,32)

    # check if id is unique
    id = binascii.hexlify(id).decode()
    tdir = os.path.join(datadir,id)
    if(os.path.exists(os.path.join(tdir,'rules'))):
      fail(s)

    # 1st step OPRF with a new seed
    # this might be if the user already has stored a blob for this id
    # and now also wants a sphinx rwd
    if(os.path.exists(os.path.join(tdir,'key'))):
      k = load_blob(id,'key',32)
    else:
      k=pysodium.randombytes(32)
    try:
        beta = sphinxlib.respond(alpha, k)
    except:
      fail(s)

    s.send(beta)

    # wait for auth signing pubkey and rules
    msg = s.recv(32+RULE_SIZE+64) # pubkey, rule, signature
    if len(msg)!=32+RULE_SIZE+64:
      fail(s)
    # verify auth sig on packet
    pk = msg[0:32]
    try:
      msg = verify_blob(msg,pk)
    except ValueError:
      fail(s)

    rules = msg[32:]

    if not os.path.exists(datadir):
        os.mkdir(datadir,0o700)
    if not os.path.exists(tdir):
        os.mkdir(tdir,0o700)

    save_blob(id,'key',k)
    save_blob(id,'pub',pk)
    save_blob(id,'rules',rules)

    # 3rd phase
    update_blob(s) # add user to host record
    s.send(b'ok')

def load_blob(path,fname,size=None):
    f = os.path.join(datadir,path,fname)
    if not os.path.exists(f):
        if verbose: print('%s does not exist' % f)
        return
    with open(f,'rb') as fd:
        v = fd.read()
    if size and len(v) != size:
        if verbose: print("wrong size for %s" % f)
        raise ValueError('corrupted blob: %s is not %s bytes' % (f, size))
    return v

# msg format: 0x66|id[32]|alpha[32]
def get(conn, msg):
    _, msg = pop(msg,1)
    id, msg = pop(msg,32)
    alpha, msg = pop(msg,32)
    if msg!=b'':
      if verbose: print('invalid get msg, trailing content %r' % msg)
      fail(conn)

    id = binascii.hexlify(id).decode()
    k = load_blob(id,'key',32)
    if k is None:
      # maybe execute protocol with static but random value to not leak which host ids exist?
      fail(conn)

    rules = load_blob(id,'rules', RULE_SIZE)
    if rules is None:
        fail(conn)

    try:
        beta = sphinxlib.respond(alpha, k)
    except:
      fail(conn)

    conn.send(beta+rules)

def auth(s,id,alpha):
  pk = load_blob(id,'pub',32)
  if pk is None:
    print('no pubkey found in %s' % id)
    fail(s)
  nonce=pysodium.randombytes(32)
  k = load_blob(id,'key')
  if k is not None:
    try:
       beta = sphinxlib.respond(alpha, k)
    except:
       fail(s)
  else:
    beta = b''
  s.send(b''.join([beta,nonce]))
  sig = s.recv(64)
  try:
    pysodium.crypto_sign_verify_detached(sig, nonce, pk)
  except:
    print('bad sig')
    fail(s)

def change(conn, msg):
  op,   msg = pop(msg,1)
  id,   msg = pop(msg,32)
  alpha,msg = pop(msg,32)
  if msg!=b'':
    if verbose: print('invalid get msg, trailing content %r' % msg)
    fail(conn)

  id = binascii.hexlify(id).decode()
  tdir = os.path.join(datadir,id)
  if not os.path.exists(tdir):
    if verbose: print("%s doesn't exist" % tdir)
    fail(conn)

  auth(conn, id, alpha)

  k=pysodium.randombytes(32)

  try:
      beta = sphinxlib.respond(alpha, k)
  except:
    fail(conn)

  #print("beta=",beta.hex())
  rules = load_blob(id,'rules', RULE_SIZE)
  if rules is None:
      fail(conn)

  save_blob(id,'new',k)
  conn.send(beta+rules)

def delete(conn, msg):
  op,   msg = pop(msg,1)
  id,   msg = pop(msg,32)
  alpha,msg = pop(msg,32)
  if msg!=b'':
    if verbose: print('invalid get msg, trailing content %r' % msg)
    fail(conn)

  id = binascii.hexlify(id).decode()
  tdir = os.path.join(datadir,id)
  if not os.path.exists(tdir):
    if verbose: print("%s doesn't exist" % tdir)
    fail(conn)

  auth(conn, id, alpha)

  update_blob(conn)

  shutil.rmtree(tdir)
  conn.send(b'ok')

def commit_undo(conn, msg, new, old):
  op,   msg = pop(msg,1)
  id,   msg = pop(msg,32)
  alpha,msg = pop(msg,32)
  if msg!=b'':
    if verbose: print('invalid get msg, trailing content %r' % msg)
    fail(conn)

  id = binascii.hexlify(id).decode()
  tdir = os.path.join(datadir,id)
  if not os.path.exists(tdir):
    if verbose: print("%s doesn't exist" % tdir)
    fail(conn)

  auth(conn, id, alpha)

  k = load_blob(id,new, 32)
  if k is None:
      fail(conn)

  key = load_blob(id,'key', 32)
  if key is None:
      fail(conn)

  try:
      beta = sphinxlib.respond(alpha, k)
  except:
    fail(conn)

  rules = load_blob(id,'rules', RULE_SIZE)
  if rules is None:
      fail(conn)

  conn.send(beta+rules)

  blob = conn.recv(32+RULE_SIZE+64)
  if len(blob)!=32+RULE_SIZE+64:
    fail(conn)

  pk = blob[0:32]
  try:
    blob = verify_blob(blob,pk)
  except ValueError:
    fail(conn)
  rules = blob[32:]

  save_blob(id,old,key)
  save_blob(id,'key',k)
  save_blob(id,'pub',pk)
  save_blob(id,'rules',rules)
  os.unlink(os.path.join(tdir,new))
  conn.send(b'ok')

def read(conn, msg):
  op,   msg = pop(msg,1)
  id,   msg = pop(msg,32)
  alpha,msg = pop(msg,32)
  id = binascii.hexlify(id).decode()
  auth(conn, id, alpha)

  blob = load_blob(id,'blob')
  if blob is None:
    blob = b''
  conn.send(blob)

def handler(conn, data):
   if verbose:
     print('Data received:',data.hex())

   if data[0] == GET:
     get(conn, data)
   elif data[0] == CHANGE:
     change(conn, data)
   elif data[0] == DELETE:
     delete(conn, data)
   elif data[0] == COMMIT:
     commit_undo(conn, data, 'new', 'old')
   elif data[0] == UNDO:
     commit_undo(conn, data, 'old', 'new')
   elif data[0] == READ:
     read(conn, data)
   elif verbose:
     print("unknown op: 0x%02x" % data[0])

   conn.close()
   os._exit(0)

def create_challenge(conn):
  req = conn.read(65)
  if req[0] == READ:
    if len(req)!=33:
      fail(conn)
  elif len(req)!=65:
    fail(conn)
  now = datetime.datetime.now().timestamp()
  id = binascii.hexlify(req[1:33]).decode()
  diff = load_blob(id,'difficulty',9) # ts: u32, level: u8, count:u32
  if not diff: # no diff yet, use easiest hardness
    n = Difficulties[0]['n']
    k = Difficulties[0]['k']
    level = 0
    count = 0
  else:
    level = struct.unpack("B", diff[0:1])[0]
    count = struct.unpack("I", diff[1:5])[0]
    ts = struct.unpack("I", diff[5:])[0]
    if level >= len(Difficulties):
      print("invalid level in rl_ctx:", level)
      level = len(Difficulties) - 1
      count = 0
    elif ((now - rl_decay) > ts and level > 0): # cooldown, decay difficulty
      periods = int((now - ts) // rl_decay)
      if level >= periods:
        level -= periods
      else:
        level = 0
      count = 0
    else: # increase hardness
      if count >= rl_threshold and (level < len(Difficulties) - 1):
        count = 0
        level+=1
      else:
        count+=1
    n = Difficulties[level]['n']
    k = Difficulties[level]['k']

  if (level == len(Difficulties) - 1) and count>rl_threshold*2:
    print(f"{normal}alert{normal}: someones trying (%d) really hard at: %s" %
          (196, 253, count, id))

  rl_ctx = b''.join([
    struct.pack("B", level),   # level
    struct.pack("I", count),   # count
    struct.pack('I', int(now)) # ts
  ])
  if(verbose): print("rl difficulty", {"level": level, "count": count, "ts": int(now)})
  try:
    save_blob(id, 'difficulty', rl_ctx)
  except FileNotFoundError:
    if diff: raise

  challenge = b''.join([bytes([n, k]), struct.pack('Q', int(now))])

  key = load_blob('', "key", 32)
  if not key:
    key=pysodium.randombytes(32)
    save_blob('','key',key)

  state = pysodium.crypto_generichash_init(32, key)
  pysodium.crypto_generichash_update(state,req)
  pysodium.crypto_generichash_update(state,challenge)
  sig = pysodium.crypto_generichash_final(state,32)

  resp = b''.join([challenge, sig])
  conn.send(resp)

def verify_challenge(conn):
  # read challenge
  challenge = conn.read(1+1+8+32) # n,k,ts,sig
  if(len(challenge)!=42):
    fail(conn)
  n, tmp = pop(challenge,1)
  n = n[0]
  k, tmp = pop(tmp,1)
  k = k[0]
  ts, tmp = pop(tmp,8)
  ts = struct.unpack("Q", ts)[0]
  sig, tmp = pop(tmp,32)

  # read request
  req = conn.read(65)
  if req[0] == READ:
    if len(req)!=33:
      fail(conn)
  elif len(req)!=65:
    fail(conn)
  # read mac key
  key = load_blob('', "key", 32)
  if not key:
    fail(conn)

  tosign = challenge[:10]

  state = pysodium.crypto_generichash_init(32, key)
  pysodium.crypto_generichash_update(state,req)
  pysodium.crypto_generichash_update(state,tosign)
  mac = pysodium.crypto_generichash_final(state,32)
  # poor mans const time comparison
  if(sum(m^i for (m, i) in zip(mac,sig))):
    fail(conn)

  now = datetime.datetime.now().timestamp()
  if now - (RL_Timeouts[(n,k)]+rl_gracetime) > ts:
    # solution is too old
    fail(conn)

  solsize = equihash.solsize(n,k)
  solution = conn.read(solsize)
  if len(solution)!= solsize:
    fail(conn)

  seed = b''.join([challenge,req])
  if not equihash.verify(n,k, seed, solution):
    fail(conn)

  handler(conn, req)

def ratelimit(conn):
   op = conn.recv(1)
   if op[0] == CREATE:
     data = b'0'+conn.recv(64)
     create(conn, data)
   elif op[0] == CHALLENGE_CREATE:
     create_challenge(conn)
   elif op[0] == CHALLENGE_VERIFY:
     verify_challenge(conn)

def main():
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=ssl_cert, keyfile=ssl_key)

    socket.setdefaulttimeout(timeout)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((address, port))
    except socket.error as msg:
        print('Bind failed. Error Code : %s Message: %s' % (str(msg[0]), msg[1]))
        sys.exit()
    #Start listening on socket
    s.listen()
    kids = []
    try:
        # main loop
        while 1:
            #wait to accept a connection - blocking call
            try:
              conn, addr = s.accept()
            except socket.timeout:
              try:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid != 0:
                  print("remove pid", pid)
                  kids.remove(pid)
                continue
              except ChildProcessError:
                continue
            except:
              raise

            if verbose:
                print('{} Connection from {}:{}'.format(datetime.datetime.now(), addr[0], addr[1]))
            conn = ctx.wrap_socket(conn, server_side=True)

            while(len(kids)>max_kids):
                pid, status = os.waitpid(0,0)
                kids.remove(pid)

            pid=os.fork()
            if pid==0:
              ssl.RAND_add(os.urandom(16),0.0)
              try:
                ratelimit(conn)
              except:
                print("fail")
                raise
              finally:
                try: conn.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                conn.close()
              sys.exit(0)
            else:
                kids.append(pid)

            try:
              pid, status = os.waitpid(-1,os.WNOHANG)
              if pid!=0:
                 kids.remove(pid)
            except ChildProcessError: pass

    except KeyboardInterrupt:
        pass
    s.close()

if __name__ == '__main__':
  main()
