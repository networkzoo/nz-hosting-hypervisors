#!/usr/bin/env python3
"""Add / check / list clusters in config.yaml without hand-editing YAML.

  python3 deploy/cluster_config.py check
  python3 deploy/cluster_config.py list
  python3 deploy/cluster_config.py set-secret Aviary [--apply]   # replace a wrong/rotated secret
  python3 deploy/cluster_config.py rename OLD NEW [--apply]      # use `rename -- "--odd" NEW` if OLD starts with -
  python3 deploy/cluster_config.py remove NAME [--drop-secret] [--apply]
  python3 deploy/cluster_config.py add proxmox Aviary https://172.16.124.30:8006 [--apply]
  python3 deploy/cluster_config.py add scale   CDAC   https://10.4.33.10 --user monitor [--apply]

`add` asks for the secret (hidden input) and stores it in .env, so nothing sensitive is typed on
the command line. --apply recreates the dashboard container afterwards. A backup of config.yaml
is kept as config.yaml.bak. Defaults: Proxmox token id monitor@pve!dashboard, TLS verify off.
Run from anywhere; files are found relative to this script's repo root.
"""
import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is missing. Install it with:  sudo apt install python3-yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.yaml")
ENVFILE = os.path.join(ROOT, ".env")


def die(msg):
    sys.exit(f"ERROR: {msg}")


def load_yaml(text, label="config.yaml"):
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        die(f"{label} is not valid YAML{where}: {getattr(e, 'problem', e)}\n"
            "       Check that every '- name:' starts at the same indentation.")


def read_env():
    vals = {}
    if os.path.exists(ENVFILE):
        for line in open(ENVFILE, encoding="utf-8"):
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.-]*)=(.*)$", line.rstrip("\n"))
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                vals[m.group(1)] = v
    return vals


def write_env_var(var, secret):
    if "\n" in secret or "'" in secret:
        die("secret can't contain a newline or a single quote (') - choose another or edit .env by hand")
    line = f"{var}='{secret}'\n"
    lines = open(ENVFILE, encoding="utf-8").read().splitlines(True) if os.path.exists(ENVFILE) else []
    out, done = [], False
    for l in lines:
        if re.match(rf"\s*{re.escape(var)}=", l):
            out.append(line)
            done = True
        else:
            out.append(l)
    if not done:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(line)
    with open(ENVFILE, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.chmod(ENVFILE, 0o600)  # holds secrets: owner-only


def check(quiet=False):
    if not os.path.exists(CONFIG):
        die(f"{CONFIG} not found")
    cfg = load_yaml(open(CONFIG, encoding="utf-8").read())
    env = read_env()
    clusters = cfg.get("clusters") or []
    problems, seen = 0, set()
    for i, c in enumerate(clusters, 1):
        name = c.get("name", f"#{i}")
        errs = []
        for k in ("name", "type", "url"):
            if not c.get(k):
                errs.append(f"missing '{k}'")
        if name in seen:
            errs.append("duplicate name")
        seen.add(name)
        if c.get("type") == "proxmox":
            need = [("token_id", None), ("token_secret_env", "token_secret_env")]
        elif c.get("type") == "scale":
            need = [("username", None), ("password_env", "password_env")]
        else:
            need = []
            errs.append(f"type must be proxmox or scale (got {c.get('type')!r})")
        for key, envkey in need:
            if not c.get(key):
                errs.append(f"missing '{key}'")
            elif envkey and not env.get(c[key]):
                errs.append(f"{c[key]} is not set in .env")
        status = "OK " if not errs else "BAD"
        if not quiet or errs:
            print(f"  [{status}] {name:<20} {c.get('type', '?'):<8} {c.get('url', '')}"
                  + (f"   <- {'; '.join(errs)}" if errs else ""))
        problems += bool(errs)
    if not quiet:
        print(f"{len(clusters)} cluster(s), {problems} with problems")
    return problems


def add(args):
    if not os.path.exists(CONFIG):
        die(f"{CONFIG} not found - copy config.example.yaml to config.yaml first")
    raw = open(CONFIG, encoding="utf-8").read()
    cfg = load_yaml(raw)
    names = [c.get("name") for c in (cfg.get("clusters") or [])]
    if args.name.startswith("-"):
        die("a cluster name can't start with '-' (it looks like an option)")
    if args.name in names:
        die(f"a cluster named {args.name!r} already exists")
    if not re.match(r"^https?://\S+$", args.url):
        die("url must start with https:// (or http://) and contain no spaces")

    slug = re.sub(r"[^A-Za-z0-9]+", "_", args.name).strip("_").upper()
    if args.type == "proxmox":
        envvar = args.env or f"PVE_{slug}_TOKEN"
        fields = [("type", "proxmox"), ("url", args.url), ("token_id", args.token_id),
                  ("token_secret_env", envvar)]
        prompt = f"API token secret for {args.name} (stored in .env as {envvar})"
    else:
        envvar = args.env or f"SCALE_{slug}_PASSWORD"
        fields = [("type", "scale"), ("url", args.url), ("username", args.user),
                  ("password_env", envvar)]
        prompt = f"Password for {args.user} on {args.name} (stored in .env as {envvar})"
    fields.append(("verify_tls", "true" if args.verify_tls else "false"))

    # Work out the indentation already used by the list, so the new entry lines up.
    text = raw
    if re.search(r"(?m)^clusters:\s*\[\s*\]\s*$", text):
        text = re.sub(r"(?m)^clusters:\s*\[\s*\]\s*$", "clusters:", text)
    lines = text.splitlines()
    try:
        ci = next(i for i, l in enumerate(lines) if re.match(r"^clusters:\s*(#.*)?$", l))
    except StopIteration:
        die("no 'clusters:' section found in config.yaml - add a line 'clusters:' at the end first")
    for l in lines[ci + 1:]:
        if l.strip() and not l.startswith((" ", "-", "#")):
            die("'clusters:' is not the last section of config.yaml, so I can't append safely.\n"
                "       Move it to the end of the file, or edit by hand.")
    dash = 2
    for l in lines[ci + 1:]:
        m = re.match(r"^(\s*)- name:", l)
        if m:
            dash = len(m.group(1))
            break
    q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    quoted = {"url", "token_id"}
    block = [" " * dash + f"- name: {q(args.name)}"]
    for k, v in fields:
        block.append(" " * (dash + 2) + f"{k}: {q(v) if k in quoted else v}")
    new_text = "\n".join(lines).rstrip("\n") + "\n" + "\n".join(block) + "\n"

    # Prove the result is valid and contains our entry before touching anything.
    parsed = load_yaml(new_text, "the new config")
    got = [c for c in parsed.get("clusters", []) if c.get("name") == args.name]
    if len(got) != 1:
        die("internal check failed: new entry did not parse as expected; nothing was changed")

    secret = os.environ.get("CLUSTER_SECRET")
    if secret is None:
        secret = getpass.getpass(prompt + " (Enter to skip): ")
    secret = secret.strip()     # a pasted secret often carries a trailing space/newline
    shutil.copyfile(CONFIG, CONFIG + ".bak")
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(new_text)
    if secret:
        write_env_var(envvar, secret)
    print(f"Added {args.name!r}. Backup: {CONFIG}.bak")
    problems = check(quiet=True)
    if problems:
        print("Some clusters still have problems (see above); run 'check' for details.")
    if args.apply:
        print("Restarting dashboard...")
        subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "dashboard"], cwd=ROOT, check=True)
        print("Done. Give it a minute, then refresh the page.")
    else:
        print("Apply with:  docker compose up -d --force-recreate dashboard   (or re-run with --apply)")


def _entry_span(lines, name):
    """(start, end) line indexes of the list entry whose name is `name`, inside clusters:."""
    try:
        ci = next(i for i, l in enumerate(lines) if re.match(r"^clusters:\s*(#.*)?$", l))
    except StopIteration:
        die("no 'clusters:' section found")
    start = dash = None
    for i in range(ci + 1, len(lines)):
        m = re.match(r"^(\s*)-\s+name:\s*(.*?)\s*(#.*)?$", lines[i])
        if m:
            val = m.group(2).strip().strip("\"'")
            if start is not None:
                return start, i
            if val == name:
                start, dash = i, len(m.group(1))
        elif start is not None and lines[i].strip() and not lines[i].startswith("#"):
            if len(lines[i]) - len(lines[i].lstrip()) <= dash:   # dedent: the list ended
                return start, i
    if start is None:
        die(f"no cluster named {name!r}. Known: " + ", ".join(
            str(c.get("name")) for c in load_yaml("\n".join(lines)).get("clusters") or []))
    return start, len(lines)


def _save_checked(lines, expect_names, label):
    new_text = "\n".join(lines).rstrip("\n") + "\n"
    parsed = load_yaml(new_text, "the new config")
    got = [c.get("name") for c in parsed.get("clusters") or []]
    if sorted(map(str, got)) != sorted(map(str, expect_names)):
        die(f"internal check failed after {label}: expected {expect_names}, got {got}. Nothing was changed.")
    shutil.copyfile(CONFIG, CONFIG + ".bak")
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"{label}. Backup: {CONFIG}.bak")


def _apply(args):
    if getattr(args, "apply", False):
        print("Restarting dashboard...")
        subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "dashboard"], cwd=ROOT, check=True)
        print("Done. Give it a minute, then refresh the page.")
    else:
        print("Apply with:  docker compose up -d --force-recreate dashboard   (or re-run with --apply)")


def remove(args):
    raw = open(CONFIG, encoding="utf-8").read()
    cfg = load_yaml(raw)
    clusters = cfg.get("clusters") or []
    target = [c for c in clusters if str(c.get("name")) == args.name]
    if not target:
        die(f"no cluster named {args.name!r}. Known: " + ", ".join(str(c.get("name")) for c in clusters))
    lines = raw.splitlines()
    a, b = _entry_span(lines, args.name)
    del lines[a:b]
    keep = [c.get("name") for c in clusters if str(c.get("name")) != args.name]
    _save_checked(lines, keep, f"Removed {args.name!r}")
    if args.drop_secret:
        var = target[0].get("token_secret_env") or target[0].get("password_env")
        used = {c.get("token_secret_env") or c.get("password_env") for c in clusters if c is not target[0]}
        if var and var not in used and os.path.exists(ENVFILE):
            kept = [l for l in open(ENVFILE, encoding="utf-8") if not re.match(rf"\s*{re.escape(var)}=", l)]
            open(ENVFILE, "w", encoding="utf-8").writelines(kept)
            os.chmod(ENVFILE, 0o600)
            print(f"Removed {var} from .env")
        elif var in used:
            print(f"Kept {var} in .env: another cluster still uses it")
    _apply(args)


def rename(args):
    if args.new.startswith("-"):
        die("a cluster name can't start with '-'")
    raw = open(CONFIG, encoding="utf-8").read()
    cfg = load_yaml(raw)
    names = [str(c.get("name")) for c in cfg.get("clusters") or []]
    if args.old not in names:
        die(f"no cluster named {args.old!r}. Known: " + ", ".join(names))
    if args.new in names:
        die(f"a cluster named {args.new!r} already exists (remove it first if it's a broken duplicate)")
    lines = raw.splitlines()
    a, _ = _entry_span(lines, args.old)
    m = re.match(r"^(\s*-\s+name:\s*)", lines[a])
    q = '"' + args.new.replace("\\", "\\\\").replace('"', '\\"') + '"'
    lines[a] = m.group(1) + q
    _save_checked(lines, [args.new if n == args.old else n for n in names], f"Renamed {args.old!r} to {args.new!r}")
    print("(The secret's variable name in .env is unchanged; that's fine, it's only a label.)")
    _apply(args)


def set_secret(args):
    cfg = load_yaml(open(CONFIG, encoding="utf-8").read())
    match = [c for c in (cfg.get("clusters") or []) if c.get("name") == args.name]
    if not match:
        die(f"no cluster named {args.name!r}. Known: " + ", ".join(str(c.get("name")) for c in cfg.get("clusters") or []))
    c = match[0]
    envvar = c.get("token_secret_env") if c.get("type") == "proxmox" else c.get("password_env")
    if not envvar:
        die(f"{args.name!r} has no secret variable set in config.yaml (token_secret_env / password_env)")
    what = "API token secret" if c.get("type") == "proxmox" else "password"
    secret = os.environ.get("CLUSTER_SECRET")
    if secret is None:
        secret = getpass.getpass(f"New {what} for {args.name} (stored in .env as {envvar}): ")
    secret = secret.strip()
    if not secret:
        die("empty secret; nothing changed")
    write_env_var(envvar, secret)
    print(f"Updated {envvar} in .env ({len(secret)} characters).")
    if c.get("type") == "proxmox":
        print(f"The token id in config.yaml is {c.get('token_id')!r} - it must match the token the secret belongs to.")
    if args.apply:
        print("Restarting dashboard...")
        subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "dashboard"], cwd=ROOT, check=True)
        print("Done. Give it a minute, then refresh the page.")
    else:
        print("Apply with:  docker compose up -d --force-recreate dashboard   (or re-run with --apply)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate config.yaml and .env")
    sub.add_parser("list", help="list configured clusters")
    rm = sub.add_parser("remove", help="remove a cluster")
    rm.add_argument("name")
    rm.add_argument("--drop-secret", action="store_true", help="also delete its secret from .env (if unused elsewhere)")
    rm.add_argument("--apply", action="store_true")
    rn = sub.add_parser("rename", help="rename a cluster")
    rn.add_argument("old"); rn.add_argument("new")
    rn.add_argument("--apply", action="store_true")
    ss = sub.add_parser("set-secret", help="replace the stored secret for an existing cluster")
    ss.add_argument("name")
    ss.add_argument("--apply", action="store_true", help="recreate the dashboard container afterwards")
    a = sub.add_parser("add", help="add a cluster")
    a.add_argument("type", choices=["proxmox", "scale"])
    a.add_argument("name")
    a.add_argument("url")
    a.add_argument("--token-id", default="monitor@pve!dashboard", help="Proxmox only")
    a.add_argument("--user", default="monitor", help="Scale only")
    a.add_argument("--env", help="name of the .env variable for the secret (default derived from the name)")
    a.add_argument("--verify-tls", action="store_true", help="verify the HTTPS certificate (default: off)")
    a.add_argument("--apply", action="store_true", help="recreate the dashboard container afterwards")
    # Flags written after a bare "--" would be read as names; move the known ones in front of it.
    argv = sys.argv[1:]
    if "--" in argv:
        i = argv.index("--")
        flags = [a for a in argv[i + 1:] if a in ("--apply", "--drop-secret", "--verify-tls")]
        argv = argv[:i] + flags + ["--"] + [a for a in argv[i + 1:] if a not in flags]
    args = p.parse_args(argv)
    if args.cmd == "check":
        sys.exit(1 if check() else 0)
    elif args.cmd == "list":
        check()
    elif args.cmd == "set-secret":
        set_secret(args)
    elif args.cmd == "remove":
        remove(args)
    elif args.cmd == "rename":
        rename(args)
    else:
        add(args)


if __name__ == "__main__":
    main()
