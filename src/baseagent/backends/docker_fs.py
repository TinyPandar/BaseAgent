"""The POSIX sh helper that performs DockerBackend file operations inside the container.

Run as ``docker exec CONTAINER sh -c FS_SCRIPT baseagent-fs ROOT OP ARG...``. ROOT is the
mount point (``/workspace``); tests run the same script on a temporary host directory.
Only ``cat``, ``sha256sum``, ``cut``, ``wc``, ``tr``, ``tar``, ``find``, ``mktemp``, ``ln``,
``mv``, ``rm``, ``mkdir``, ``dirname``, ``readlink -f``, ``stat``, ``chmod``, ``chown`` and
``grep`` are used, so coreutils (Debian/Ubuntu images) and busybox (Alpine) both work.

Operations (paths are workspace-relative POSIX paths already checked on the host):

``probe LIMIT ENTRY...``  ENTRY is ``r:PATH`` (read), ``i:PATH`` (read, but no symbolic link
    anywhere on the path: AGENTS.md) or ``s:PATH`` (status only). For each entry one
    ``@@ STATUS [SIZE]`` line goes to stderr, in order; STATUS is ok, missing, dir, notfile,
    escape, link or large. Readable files then go to stdout as one tar stream (links that
    stay inside the workspace are followed, like LocalBackend reads).
``stat PATH...``           one stdout line per path: ``ok SHA256 MODE`` or the status.
``put PATH EXPECTED MODE`` stdin becomes the file. EXPECTED is a digest, ``missing`` or
    ``force``; the hash is compared again right before the rename. MODE is octal or ``-``
    (keep the old mode). Prints ``ok SHA256``.
``del PATH EXPECTED``      remove a regular file if it still has EXPECTED (or ``force``).
``list PATH``              files, an empty record, then directories, NUL separated.
``after MARKER UID GID``   kill processes carrying ``BASEAGENT_EXEC=MARKER`` (one command's
    leftovers) and give files not owned by UID:GID back to the host user (``-`` skips).

Exit codes: 3 conflict, 4 created by another writer, 5 symbolic link, 6 not a regular file,
7 I/O error, 44 search path missing, 70 workspace mount missing.
"""

FS_SCRIPT = r"""
set -u
root=$1; op=$2; shift 2
cd "$root" 2>/dev/null || { echo "baseagent-fs: workspace mount is missing" >&2; exit 70; }
fail() { echo "baseagent-fs: $2" >&2; exit "$1"; }
sha() { sha256sum < "$1" | cut -c1-64; }
size() { wc -c < "$1" | tr -d ' '; }
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
classify() {
  p=$1
  if [ ! -e "$p" ] && [ ! -L "$p" ]; then st=missing; return; fi
  r=$(readlink -f "$p" 2>/dev/null) || { st=escape; return; }
  case "$r" in "$root"/*) ;; *) st=escape; return;; esac
  if [ ! -e "$p" ]; then st=missing; return; fi
  if [ -d "$p" ]; then st=dir; return; fi
  if [ ! -f "$p" ]; then st=notfile; return; fi
  st=ok
}
nolinks() {
  q=.
  set -f; saved=$IFS; IFS=/
  for c in $1; do
    [ -z "$c" ] && continue
    [ "$c" = . ] && continue
    q=$q/$c
    if [ -L "$q" ]; then IFS=$saved; set +f; return 1; fi
  done
  IFS=$saved; set +f
  return 0
}
case "$op" in
probe)
  limit=$1; shift
  n=$#; i=0
  while [ "$i" -lt "$n" ]; do
    e=$1; shift; i=$((i + 1))
    kind=${e%%:*}; p=./${e#?:}
    classify "$p"
    if [ "$st" = ok ] && [ "$kind" = i ] && ! nolinks "$p"; then st=link; fi
    if [ "$st" = ok ]; then
      s=$(size "$p")
      if [ "$kind" != s ] && [ "$s" -gt "$limit" ]; then
        echo "@@ large $s" >&2
      else
        echo "@@ ok $s" >&2
        [ "$kind" != s ] && set -- "$@" "$p"
      fi
    else
      echo "@@ $st" >&2
    fi
  done
  [ "$#" -eq 0 ] && exit 0
  exec tar -chf - "$@"
  ;;
stat)
  for rel in "$@"; do
    p=./$rel
    classify "$p"
    if [ "$st" = ok ]; then echo "ok $(sha "$p") $(mode_of "$p")"; else echo "$st"; fi
  done
  ;;
put)
  p=./$1; want=$2; mode=$3
  nolinks "$p" || fail 5 "write paths cannot use symlinks"
  if [ -e "$p" ] && [ ! -f "$p" ]; then fail 6 "write target must be a file"; fi
  d=$(dirname "$p")
  mkdir -p "$d" 2>/dev/null || fail 7 "cannot create the parent directory"
  nolinks "$p" || fail 5 "write paths cannot use symlinks"
  cur=missing; [ -f "$p" ] && cur=$(sha "$p")
  if [ "$want" != force ] && [ "$cur" != "$want" ]; then fail 3 "file version changed; read the current file before editing"; fi
  t=$(mktemp "$d/.baseagent-edit-XXXXXX") || fail 7 "cannot create a temporary file"
  if ! cat > "$t"; then rm -f "$t"; fail 7 "write failed"; fi
  if [ "$mode" != - ]; then chmod "$mode" "$t"; elif [ -f "$p" ]; then chmod "$(mode_of "$p")" "$t"; fi
  now=missing; [ -f "$p" ] && now=$(sha "$p")
  if [ "$want" != force ] && [ "$now" != "$cur" ]; then rm -f "$t"; fail 3 "file changed while preparing the edit"; fi
  if [ "$now" = missing ] && [ "$want" != force ]; then
    if ! ln "$t" "$p" 2>/dev/null; then
      if [ -e "$p" ] || [ -L "$p" ]; then rm -f "$t"; fail 4 "file was created by another writer"; fi
      mv -f "$t" "$p" || { rm -f "$t"; fail 7 "write failed"; }
    fi
    rm -f "$t"
  else
    mv -f "$t" "$p" || { rm -f "$t"; fail 7 "write failed"; }
  fi
  echo "ok $(sha "$p")"
  ;;
del)
  p=./$1; want=$2
  nolinks "$p" || fail 5 "write paths cannot use symlinks"
  if [ ! -e "$p" ] && [ ! -L "$p" ]; then
    [ "$want" = force ] && { echo ok; exit 0; }
    fail 3 "file does not exist; read the current workspace state"
  fi
  [ -f "$p" ] || fail 6 "write target must be a file"
  if [ "$want" != force ] && [ "$(sha "$p")" != "$want" ]; then fail 3 "file version changed; read the current file before editing"; fi
  rm -f "$p" || fail 7 "delete failed"
  echo ok
  ;;
list)
  if [ "$1" = . ]; then p=.; else p=./$1; fi
  [ -e "$p" ] || fail 44 "search path does not exist"
  find "$p" \( -iname .git -o -iname .venv -o -iname node_modules -o -iname __pycache__ -o -iname .baseagent \) -prune -o -type f -print0
  printf '\0'
  find "$p" \( -iname .git -o -iname .venv -o -iname node_modules -o -iname __pycache__ -o -iname .baseagent \) -prune -o -type d -print0
  ;;
after)
  marker=$1; uid=$2; gid=$3
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    [ "$pid" = "$$" ] && continue
    if tr '\0' '\n' < "$d/environ" 2>/dev/null | grep -qx "BASEAGENT_EXEC=$marker"; then kill -9 "$pid" 2>/dev/null; fi
  done
  if [ "$uid" != - ]; then
    find . \( -path ./.git -o -path ./.baseagent \) -prune -o \( ! -user "$uid" -o ! -group "$gid" \) -exec chown -h "$uid:$gid" {} + 2>/dev/null
  fi
  exit 0
  ;;
*)
  fail 2 "unknown operation"
  ;;
esac
"""

# Exit status -> (error code name, message) for file operations.
EXIT_CODES = {
    3: ("CONFLICT", None),
    4: ("CONFLICT", "file was created by another writer"),
    5: ("PERMISSION_DENIED", "write paths cannot use symlinks or junctions"),
    6: ("PERMISSION_DENIED", "write target must be a file"),
    7: ("EXECUTION_FAILED", None),
    44: ("NOT_FOUND", "search path does not exist"),
}
