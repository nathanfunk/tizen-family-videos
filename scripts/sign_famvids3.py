#!/usr/bin/env python3
"""Drive tizen.sh package prompts with a state machine: answer each prompt
as soon as its text appears in the pty output."""
import os, pty, select, shutil, sys, tempfile, time

CWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIZEN = os.path.expanduser(os.environ.get(
    'FV_TIZEN', '~/tizen-studio/tools/ide/bin/tizen.sh'))
AUTHOR_PW = os.environ.get('FV_AUTHOR_PW', '')
# the SDK default distributor password (public, documented)
DIST_PW = os.environ.get('FV_DIST_PW', 'tizenpkcs12passfordsigner')


def main():
    if not AUTHOR_PW:
        sys.exit('FV_AUTHOR_PW not set — source deploy.local.sh first')
    os.chdir(CWD)

    # Stage the app tree into a temp dir and package from there, so a personal
    # launcher icon (FV_ICON, e.g. a family photo — never committed to git) can
    # replace the default icon.png at package time only.
    icon = os.environ.get('FV_ICON', '')
    if icon and not os.path.isfile(icon):
        sys.exit('FV_ICON set but file missing: ' + icon)

    stage = tempfile.mkdtemp(prefix='fvstage-')
    pid = None
    fd = None
    buf = b''
    try:
        shutil.copytree(os.path.join(CWD, 'tvapp'), stage, dirs_exist_ok=True)
        if icon:
            shutil.copy(icon, os.path.join(stage, 'icon.png'))
            print('personal icon applied:', icon, file=sys.stderr)

        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execvp(TIZEN, [TIZEN, 'package', '-t', 'wgt', '-s', 'a2s',
                                  '-o', 'out', '--', stage])
            except OSError as e:
                os.write(2, ('exec failed: %r\n' % e).encode())
            os._exit(127)

        sent_author = sent_dist = saved = 0
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
            if (b'Package File Location' in buf or b'Invaild password' in buf
                    or b'Invalid password' in buf or b'An error has occurred' in buf):
                break

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

        out = buf.decode('utf-8', 'replace')
        print(out[-1800:])
        print('\nsent: author=%d dist=%d saved=%d' % (sent_author, sent_dist, saved))
    finally:
        if pid:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == '__main__':
    main()
