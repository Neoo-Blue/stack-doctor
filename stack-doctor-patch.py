#!/usr/bin/env python3
import os

WEBUI = "/data/stack-doctor-src/doctor/webui.py"
JANITOR = "/data/stack-doctor-src/doctor/checks/janitor.py"

def patch_webui():
    if not os.path.exists(WEBUI):
        print(f"ERROR: {WEBUI} not found")
        return False
    with open(WEBUI) as f:
        content = f.read()
    orig = content
    content = content.replace('for r in reversed(_warm_recent)', 'for r in reversed(_warmer._warm_recent)')
    content = content.replace('"total": _warm_count[0]', '"total": _warmer._warm_count[0]')
    if content != orig:
        with open(WEBUI, 'w') as f:
            f.write(content)
        print("PATCHED webui.py warmer variables")
    else:
        print("No webui patch needed")
    return True

def patch_janitor():
    if not os.path.exists(JANITOR):
        print(f"ERROR: {JANITOR} not found")
        return False
    with open(JANITOR) as f:
        content = f.read()
    orig = content
    old = 'mm = re.search(r"/__all__/([^/]+)(?:/|$)", tgt)'
    new = 'mm = re.search(r"/(?:__all__|complete)/([^/]+)(?:/|$)", tgt)'
    content = content.replace(old, new)
    if content != orig:
        with open(JANITOR, 'w') as f:
            f.write(content)
        print("PATCHED janitor.py altmount/complete regex")
    else:
        print("No janitor patch needed")
    return True

patch_webui()
patch_janitor()
