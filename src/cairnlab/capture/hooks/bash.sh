# Cairn bash capture hook. Emitted by `cairn activate`; load it with:
#
#     export CAIRN_USER=<you>; eval "$(cairn activate)"
#
# It is never installed into any startup file. Sourcing this file only defines
# functions; `cairn activate` appends, at top level (see __cairn_activate):
#     __cairn_debug_probe=$(trap -p DEBUG); __cairn_activate
#
# One JSON object per command is appended to
#     $CAIRN_HOME/events/<host>-<YYYY-MM-DD>.jsonl   (UTC date)
#
# Design notes
# - Works on bash 4.2+ (no namerefs, no negative array indices, no ${x@Q}, no PS0).
# - Safe under `set -u`: every expansion of a possibly-unset variable has a default.
# - Fails silently: write errors are discarded, and nothing here can exit the shell.
# - The hook adds one fork per prompt (`history 1`); on bash < 5 it adds two more
#   (`date`) per recorded command.
# - Bash hides the DEBUG trap inside functions and puts the caller's trap back when
#   a function returns, so the old trap is probed at top level, and deactivation
#   installs a one-shot trap that restores the old one from top level.
# - Ordering: __cairn_precmd runs first in PROMPT_COMMAND so it sees the user's $?,
#   and hands that status back so the existing PROMPT_COMMAND sees it too.
#   __cairn_arm runs last, so DEBUG-trap firings caused by the rest of
#   PROMPT_COMMAND are never mistaken for the start of the user's next command.

__cairn_marker='[cairn] '
__cairn_ignore=' ls cd pwd clear history '  # plus "cairn note" and "cairn run"

# JSON-escape $1 into __cairn_j (no fork). Valid UTF-8 passes through unchanged.
__cairn_json_escape() {
  local s=${1-} bs='\' q='"' i o c
  s=${s//"$bs"/"$bs$bs"}
  s=${s//"$q"/"$bs$q"}
  case $s in
    *[[:cntrl:]]*)
      s=${s//$'\n'/"${bs}n"}
      s=${s//$'\t'/"${bs}t"}
      s=${s//$'\r'/"${bs}r"}
      s=${s//$'\f'/"${bs}f"}
      s=${s//$'\b'/"${bs}b"}
      case $s in
        *[[:cntrl:]]*)
          for i in 1 2 3 4 5 6 7 11 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 127; do
            printf -v o '%03o' "$i"
            printf -v c "\\$o"
            case $s in
              *"$c"*)
                printf -v o '%su%04x' "$bs" "$i"
                s=${s//"$c"/"$o"}
                ;;
            esac
          done
          ;;
      esac
      ;;
  esac
  __cairn_j=$s
}

# Convert epoch seconds $1 and a 3-digit millisecond string $2 to
# YYYY-MM-DDTHH:MM:SS.mmmZ in __cairn_ts. Pure arithmetic (civil-from-days), no fork.
__cairn_iso() {
  local t=$1 ms=${2-000}
  local days=$((t / 86400)) sod=$((t % 86400))
  local z=$((days + 719468))
  local era=$(((z >= 0 ? z : z - 146096) / 146097))
  local doe=$((z - era * 146097))
  local yoe=$(((doe - doe / 1460 + doe / 36524 - doe / 146096) / 365))
  local doy=$((doe - (365 * yoe + yoe / 4 - yoe / 100)))
  local mp=$(((5 * doy + 2) / 153))
  local d=$((doy - (153 * mp + 2) / 5 + 1))
  local m=$((mp < 10 ? mp + 3 : mp - 9))
  local y=$((yoe + era * 400 + (m <= 2 ? 1 : 0)))
  printf -v __cairn_ts '%04d-%02d-%02dT%02d:%02d:%02d.%sZ' \
    "$y" "$m" "$d" $((sod / 3600)) $((sod % 3600 / 60)) $((sod % 60)) "$ms"
}

# Current time into __cairn_sec (epoch seconds) and __cairn_ms (3 digits).
__cairn_now() {
  local t frac
  if [ -n "${EPOCHREALTIME-}" ]; then
    t=$EPOCHREALTIME
  else
    t=$(date +%s.%N 2>/dev/null) || t=
  fi
  __cairn_sec=${t%%[.,]*}
  case $t in
    *[.,]*) frac=${t#*[.,]} ;;
    *) frac= ;;
  esac
  case $__cairn_sec in
    '' | *[!0-9]*) __cairn_sec=0 ;;
  esac
  case $frac in
    *[!0-9]*) frac= ;;
  esac
  frac=${frac}000
  __cairn_ms=${frac:0:3}
}

# Return 0 if the command in $1 should not be recorded.
__cairn_ignored() {
  local c=${1-} w
  # Strip leading whitespace, then take the first word.
  c=${c#"${c%%[![:space:]]*}"}
  w=${c%%[[:space:]]*}
  case $__cairn_ignore in
    *" $w "*) ;;
    *)
      case $c in
        # cairn run records itself (with exact outputs), so the hook skips it.
        'cairn note' | 'cairn note'[[:space:]]* | 'cairn run'[[:space:]]*) ;;
        *) return 1 ;;
      esac
      ;;
  esac
  # Command substitution runs something else: always record.
  case $c in
    *'$('* | *'`'*) return 1 ;;
  esac
  # Only a lone simple command is ignored: "cd x && make" is still recorded, but
  # separators inside quotes (cairn note "a; b") do not count.
  ! __cairn_has_separator "$c"
}

# Return 0 if $1 contains ; & | or a newline outside quotes. Only called for the
# handful of ignorable commands, so the character loop costs well under a millisecond.
__cairn_has_separator() {
  local s=${1-} i=0 n ch q=
  n=${#s}
  case $s in
    *[\;\&\|]* | *$'\n'*) ;;
    *) return 1 ;;
  esac
  while [ "$i" -lt "$n" ]; do
    ch=${s:i:1}
    if [ -n "$q" ]; then
      if [ "$ch" = "$q" ]; then
        q=
      elif [ "$q" = '"' ] && [ "$ch" = '\' ]; then
        i=$((i + 1))
      fi
    else
      case $ch in
        "'" | '"') q=$ch ;;
        '\') i=$((i + 1)) ;;
        ';' | '&' | '|' | $'\n') return 0 ;;
      esac
    fi
    i=$((i + 1))
  done
  return 1
}

# Return 0 if the command in $1 looks like it carries a secret (case-insensitive).
__cairn_secret() {
  local u=${1-}
  u=${u^^}
  case $u in
    *KEY*=* | *TOKEN* | *PASSWORD* | *SECRET*) return 0 ;;
  esac
  return 1
}

# DEBUG trap: note the start time and cwd of the first command after each prompt.
__cairn_debug() {
  [ -n "${__cairn_armed-}" ] || return 0
  [ -z "${COMP_LINE-}" ] || return 0
  case ${FUNCNAME[1]-} in
    __cairn_*) return 0 ;;
  esac
  case ${BASH_COMMAND-} in
    __cairn_precmd* | __cairn_arm*) return 0 ;;
  esac
  __cairn_armed=
  __cairn_now
  __cairn_t0_sec=$__cairn_sec
  __cairn_t0_ms=$__cairn_ms
  __cairn_t0_cwd=$PWD
  __cairn_t0_cmd=${BASH_COMMAND-}
  return 0
}

# Write one cmd record if a new history entry appeared. $1 = exit status.
# A new history entry is the signal that a command ran; the DEBUG trap only supplies
# its exact start. Lines that never fire the trap in this shell (a bare "( ... )",
# coproc, a function definition) fall back to the prompt time, flagged ts_start_approx.
__cairn_write() {
  local rc=$1 hist num text start end dir file approx= repeat= cwd
  [ -n "${CAIRN_HOME-}" ] || return 0
  hist=$(HISTTIMEFORMAT= builtin history 1)
  hist=${hist#"${hist%%[![:space:]]*}"}
  num=${hist%%[!0-9]*}
  [ -n "$num" ] || return 0
  text=${hist#"$num"}
  text=${text:2}
  # History unchanged: an empty line, a line hidden by ignorespace, or an identical
  # rerun swallowed by ignoredups. Only the rerun is recorded: the DEBUG trap fired,
  # ignoredups is on, and the first simple command it saw starts the previous entry.
  # (A hidden line whose first command happens to prefix the previous entry would be
  # misread as a rerun; its own text is still never recorded.)
  if [ "$num" = "${__cairn_last_hist-}" ] && [ "$text" = "${__cairn_last_text-}" ]; then
    [ -n "${__cairn_t0_sec-}" ] && [ -n "${__cairn_t0_cmd-}" ] || return 0
    case ${HISTCONTROL-} in
      *ignoredups* | *ignoreboth* | *erasedups*) ;;
      *) return 0 ;;
    esac
    case $text in
      "$__cairn_t0_cmd"*) repeat=',"hist_repeat":true' ;;
      *) return 0 ;;
    esac
  fi
  __cairn_last_hist=$num
  __cairn_last_text=$text
  ! __cairn_ignored "$text" || return 0
  ! __cairn_secret "$text" || text='[redacted]'

  if [ -n "${__cairn_t0_sec-}" ]; then
    __cairn_iso "$__cairn_t0_sec" "$__cairn_t0_ms"
    cwd=$__cairn_t0_cwd
  else
    __cairn_iso "${__cairn_p_sec:-0}" "${__cairn_p_ms:-000}"
    cwd=${__cairn_p_cwd:-$PWD}
    approx=',"ts_start_approx":true'
  fi
  start=$__cairn_ts
  __cairn_now
  __cairn_iso "$__cairn_sec" "$__cairn_ms"
  end=$__cairn_ts

  dir=$CAIRN_HOME/events
  [ -d "$dir" ] || mkdir -p "$dir" || return 0
  file=$dir/$__cairn_host-${end:0:10}.jsonl
  __cairn_seq=$((${__cairn_seq-0} + 1))

  # The environment the command ran in (cheap: two variables).
  local envj= j_env
  if [ -n "${CONDA_DEFAULT_ENV-}" ]; then
    __cairn_json_escape "$CONDA_DEFAULT_ENV"
    envj=",\"conda_env\":\"$__cairn_j\""
  fi
  j_env=${VIRTUAL_ENV:-${CONDA_PREFIX-}}
  if [ -n "$j_env" ]; then
    __cairn_json_escape "$j_env"
    envj="$envj,\"env_prefix\":\"$__cairn_j\""
  fi

  local j_host j_user j_cwd j_cmd j_sess
  __cairn_json_escape "$__cairn_host"; j_host=$__cairn_j
  __cairn_json_escape "${CAIRN_USER-}"; j_user=$__cairn_j
  __cairn_json_escape "$cwd"; j_cwd=$__cairn_j
  __cairn_json_escape "$text"; j_cmd=$__cairn_j
  __cairn_json_escape "${CAIRN_SESSION-}"; j_sess=$__cairn_j
  printf '{"v":1,"type":"cmd","id":"%s-%s","session":"%s","host":"%s","shell":"bash","user":"%s","cwd":"%s","cmd":"%s","ts_start":"%s","ts_end":"%s","exit":%d%s%s%s}\n' \
    "$j_sess" "$__cairn_seq" "$j_sess" "$j_host" "$j_user" "$j_cwd" "$j_cmd" \
    "$start" "$end" "$rc" "$approx" "$repeat" "$envj" >>"$file"
}

# First entry of PROMPT_COMMAND.
__cairn_precmd() {
  local __cairn_rc=$?
  if [ -n "${CAIRN_USER-}" ]; then
    __cairn_write "$__cairn_rc" 2>/dev/null || :
  fi
  __cairn_t0_sec=
  __cairn_t0_cmd=
  return $__cairn_rc
}

# Last entry of PROMPT_COMMAND: arm the DEBUG trap and keep the prompt marker.
__cairn_arm() {
  local rc=$?
  case ${PS1-} in
    *"$__cairn_marker"*) ;;
    *) PS1=$__cairn_marker${PS1-} ;;
  esac
  # Prompt time and cwd, the fallback start for lines the DEBUG trap never sees.
  # EPOCHREALTIME (bash 5) gives milliseconds; printf %(...)T (bash 4.2+) gives whole
  # seconds. Neither forks.
  if [ -n "${EPOCHREALTIME-}" ]; then
    __cairn_now
    __cairn_p_sec=$__cairn_sec
    __cairn_p_ms=$__cairn_ms
  else
    printf -v __cairn_p_sec '%(%s)T' -1
    __cairn_p_ms=000
  fi
  __cairn_p_cwd=$PWD
  __cairn_armed=1
  return $rc
}

# `cairn` is a shell function while active (like conda) so that `cairn deactivate`
# can change this shell. In a subshell, e.g. eval "$(cairn deactivate)", it defers to
# the real command, which prints the deactivation code instead.
__cairn_wrapper() {
  if [ "${1-}" = deactivate ] && [ "${BASH_SUBSHELL-0}" = 0 ]; then
    __cairn_deactivate
  else
    command cairn "$@"
  fi
}

__cairn_unload() {
  unset -f __cairn_json_escape __cairn_iso __cairn_now __cairn_ignored __cairn_secret \
    __cairn_has_separator \
    __cairn_debug __cairn_write __cairn_precmd __cairn_arm __cairn_wrapper \
    __cairn_activate __cairn_deactivate __cairn_grab_trap __cairn_unload
  unset __cairn_marker __cairn_ignore __cairn_j __cairn_ts __cairn_sec __cairn_ms \
    __cairn_armed __cairn_t0_sec __cairn_t0_ms __cairn_t0_cwd __cairn_t0_cmd __cairn_last_hist \
    __cairn_last_text __cairn_seq __cairn_host __cairn_p_sec __cairn_p_ms __cairn_p_cwd \
    __cairn_default_user __cairn_default_host __cairn_session_id __cairn_default_home \
    __cairn_debug_probe
}

# `eval "__cairn_grab_trap ${old#trap }"` turns `trap -- 'cmd' DEBUG` into the bare cmd.
__cairn_grab_trap() {
  __cairn_old_debug=${2-}
}

__cairn_activate() {
  if [ -z "${CAIRN_USER-}" ] && [ -n "${__cairn_default_user-}" ]; then
    export CAIRN_USER=$__cairn_default_user
  fi
  if [ -z "${CAIRN_USER-}" ]; then
    echo 'cairn: CAIRN_USER is not set; run: export CAIRN_USER=<your name>' \
      '(or put CAIRN_USER=<your name> in ~/.config/cairn/env)' >&2
    __cairn_unload
    return 1
  fi
  case $CAIRN_USER in
    *[!A-Za-z0-9._-]*)
      echo "cairn: CAIRN_USER='$CAIRN_USER' may only use letters, digits, '.', '_' and '-'" >&2
      __cairn_unload
      return 1
      ;;
  esac
  if [ -n "${__cairn_active-}" ]; then
    echo "cairn: already active in this shell (session $CAIRN_SESSION)" >&2
    return 0
  fi

  # Remember everything we change, so __cairn_deactivate can put it back exactly.
  __cairn_had_home=${CAIRN_HOME+1}
  __cairn_old_home=${CAIRN_HOME-}
  __cairn_had_session=${CAIRN_SESSION+1}
  __cairn_old_session=${CAIRN_SESSION-}
  __cairn_had_pc=${PROMPT_COMMAND+1}
  __cairn_old_pc=${PROMPT_COMMAND-}
  __cairn_old_ps1=${PS1-}
  __cairn_pc_array=
  case $(declare -p PROMPT_COMMAND 2>/dev/null) in
    'declare -a'*) __cairn_pc_array=1 ;;
  esac
  # Probed at top level by the caller: inside a function, `trap -p DEBUG` is empty.
  __cairn_old_debug_spec=${__cairn_debug_probe-}
  unset __cairn_debug_probe
  __cairn_old_debug=
  if [ -n "$__cairn_old_debug_spec" ]; then
    eval "__cairn_grab_trap ${__cairn_old_debug_spec#trap }"
  fi

  export CAIRN_HOME="${CAIRN_HOME:-${__cairn_default_home:-${HOME-}/.cairn-$CAIRN_USER}}"
  if [ -n "${__cairn_session_id-}" ]; then
    export CAIRN_SESSION=$__cairn_session_id
  else
    export CAIRN_SESSION="s$(date +%s 2>/dev/null)$$$RANDOM"
  fi
  __cairn_host=${CAIRN_HOST:-${__cairn_default_host:-${HOSTNAME%%.*}}}
  __cairn_seq=0
  __cairn_armed=
  __cairn_t0_sec=
  __cairn_p_sec=
  __cairn_p_cwd=
  mkdir -p "$CAIRN_HOME/events" 2>/dev/null

  # Seed the history position so the activation line itself is not recorded.
  local hist
  hist=$(HISTTIMEFORMAT= builtin history 1)
  hist=${hist#"${hist%%[![:space:]]*}"}
  __cairn_last_hist=${hist%%[!0-9]*}
  __cairn_last_text=${hist#"$__cairn_last_hist"}
  __cairn_last_text=${__cairn_last_text:2}

  if [ -n "$__cairn_old_pc" ]; then
    PROMPT_COMMAND="__cairn_precmd
$__cairn_old_pc"
  else
    PROMPT_COMMAND=__cairn_precmd
  fi
  if [ -n "$__cairn_pc_array" ]; then
    PROMPT_COMMAND+=(__cairn_arm)
  else
    PROMPT_COMMAND="$PROMPT_COMMAND
__cairn_arm"
  fi
  __cairn_pc_new=$PROMPT_COMMAND

  if [ -n "$__cairn_old_debug" ]; then
    trap "__cairn_debug
$__cairn_old_debug" DEBUG
  else
    trap __cairn_debug DEBUG
  fi

  PS1=$__cairn_marker${PS1-}
  __cairn_ps1_new=$PS1
  function cairn { __cairn_wrapper "$@"; }
  __cairn_active=1
  echo "cairn: capture on (user $CAIRN_USER, session $CAIRN_SESSION) -> $CAIRN_HOME/events" >&2
  return 0
}

__cairn_deactivate() {
  [ -n "${__cairn_active-}" ] || return 0

  # PROMPT_COMMAND: restore exactly if nobody else touched it; otherwise remove our parts.
  if [ -n "$__cairn_pc_array" ]; then
    local i
    for i in "${!PROMPT_COMMAND[@]}"; do
      [ "${PROMPT_COMMAND[$i]}" != __cairn_arm ] || unset 'PROMPT_COMMAND[i]'
    done
  fi
  if [ "${PROMPT_COMMAND-}" = "$__cairn_pc_new" ] || [ -n "$__cairn_pc_array" ]; then
    if [ -n "$__cairn_had_pc" ]; then
      PROMPT_COMMAND=$__cairn_old_pc
    else
      unset 'PROMPT_COMMAND[0]'
    fi
  else
    local pc=${PROMPT_COMMAND-}
    pc=${pc//"__cairn_precmd"$'\n'/}
    pc=${pc//$'\n'"__cairn_arm"/}
    pc=${pc//"__cairn_precmd"/}
    pc=${pc//"__cairn_arm"/}
    PROMPT_COMMAND=$pc
  fi

  # A trap changed inside a function is reverted on return, so hand the restore to a
  # one-shot trap that acts on its first firing outside any function.
  __cairn_final_trap=${__cairn_old_debug_spec:-trap - DEBUG}
  trap '[ -n "${FUNCNAME-}" ] || { eval "$__cairn_final_trap"; unset __cairn_final_trap; }' DEBUG

  if [ "${PS1-}" = "$__cairn_ps1_new" ]; then
    PS1=$__cairn_old_ps1
  else
    PS1=${PS1/"$__cairn_marker"/}
  fi

  if [ -n "$__cairn_had_home" ]; then
    CAIRN_HOME=$__cairn_old_home
  else
    unset CAIRN_HOME
  fi
  if [ -n "$__cairn_had_session" ]; then
    CAIRN_SESSION=$__cairn_old_session
  else
    unset CAIRN_SESSION
  fi

  unset -f cairn
  unset __cairn_active __cairn_had_home __cairn_old_home __cairn_had_session \
    __cairn_old_session __cairn_had_pc __cairn_old_pc __cairn_old_ps1 __cairn_pc_array \
    __cairn_pc_new __cairn_ps1_new __cairn_old_debug __cairn_old_debug_spec
  __cairn_unload
  echo 'cairn: capture off' >&2
  return 0
}
