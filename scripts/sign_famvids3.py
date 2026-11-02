#!/usr/bin/env python3
"""Drive tizen.sh package prompts with a state machine: answer each prompt
as soon as its text appears in the pty output."""
import os, pty, select, sys, time

CWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTHOR_PW = os.environ.get('FV_AUTHOR_PW', '')
if not AUTHOR_PW:
    sys.exit('FV_AUTHOR_PW not set — source deploy.local.sh first')
# the SDK default distributor password (public, documented)
DIST_PW = 'tizenpkcs12passfordsigner'

os.chdir(CWD)
pid, fd = pty.fork()
if pid == 0:
    os.execvp(os.path.expanduser('~/tizen-studio/tools/ide/bin/tizen.sh'),
              [os.path.expanduser('~/tizen-studio/tools/ide/bin/tizen.sh'),
               'package', '-t', 'wgt', '-s', 'a2s', '-o', 'out', '--', 'tvapp'])

buf = b''
sent_author = sent_dist = saved = 0
out_lines = []
deadline = time.time() + 120
while time.time() < deadline:
    r, _, _ = select.select([fd], [], [], 0.5)
    if r:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    text = buf.decode('utf-8', 'replace')
    if not sent_author and 'Author password' in text:
        time.sleep(0.4); os.write(fd, (AUTHOR_PW + '\n').encode()); sent_author = 1
        buf = b''
    elif sent_author and saved < 2 and 'Yes: (Y), No: (N)' in text:
        time.sleep(0.4); os.write(fd, b'N\n'); saved += 1
        buf = b''
    elif sent_author and not sent_dist and 'Distributor1 password' in text:
        time.sleep(0.4); os.write(fd, (DIST_PW + '\n').encode()); sent_dist = 1
        buf = b''
    if b'Package File Location' in buf or b'Invaild password' in buf or b'An error has occurred' in buf:
        break

try:
    # drain remaining output briefly
    end = time.time() + 8
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if b'Package File Location' in buf or b'Invaild password' in buf:
            break
finally:
    try:
        os.kill(pid, 9)
    except OSError:
        pass

out = buf.decode('utf-8', 'replace')
print(out[-1800:])
print('\nsent: author=%d dist=%d saved=%d' % (sent_author, sent_dist, saved))
